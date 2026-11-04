"""Web fonts from a reference PDF: Google Fonts where the family exists there, otherwise the embedded font program
rebuilt into a font a browser accepts.

Fonts embedded in a PDF are subsets with PDF-specific character mappings (WinAnsi codes, Identity-H CIDs, symbol
cmaps), and usually lack tables browsers require. Each one is rebuilt with a Unicode cmap taken from the PDF's own
ToUnicode / encoding data, consistent vertical metrics and the minimum OpenType tables, so the browser draws the
same glyphs the PDF did. Several subsets of one face are declared under one family with `unicode-range`, so the
browser composes them; characters no subset contains fall back to a similar Google font.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field

import httpx
import pymupdf
from fontTools import agl
from fontTools.fontBuilder import FontBuilder
from fontTools.misc.psCharStrings import T2WidthExtractor
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

from app.services.reference_analysis.fonts import classify_font, clean_font_name, is_italic, weight_of

log = logging.getLogger("pressflow.reference_import.fonts")

DROP_TABLES = ("fpgm", "prep", "cvt ", "hdmx", "VDMX", "LTSH", "DSIG", "kern", "GPOS", "GSUB", "GDEF", "vhea", "vmtx")
DEFAULT_BASELINE_K = 0.35  # (ascent - descent) / 2 in em: where the baseline sits in a CSS line box
FALLBACK_GOOGLE = {"serif": "Source Serif 4", "sans": "Inter", "display": "Playfair Display", "mono": "JetBrains Mono"}


def _winansi(code: int) -> str | None:
    try:
        return bytes([code]).decode("cp1252")
    except UnicodeDecodeError:
        return None


@dataclass
class FaceInfo:
    """One typeface (family + weight + italic) as the editor uses it."""

    family: str  # CSS family name, e.g. "Roboto", "Good Pro"
    weight: int
    italic: bool
    classification: str
    google: bool = False
    baseline_k: float = DEFAULT_BASELINE_K
    style_name: str = ""  # the font's own style name ("SemiBold", "Heavy", "Bold Italic"), which InDesign matches on
    files: list[dict] = field(default_factory=list)  # {"id", "bytes", "unicodeRange"} rebuilt subsets
    # Every rebuilt subset of the face, for measuring text set in it: each holds only the letters the PDF used
    # somewhere, so a letter is measured in whichever subset has it (their glyphs are the same face's).
    metrics: list[tuple[bytes, set[int]]] = field(default_factory=list)
    _fonts: list = field(default_factory=list, repr=False)

    def text_width(self, text: str, size: float, tracking: float = 0.0) -> float | None:
        """Natural width of `text` in this face at `size` pt (tracking in thousandths of an em), or None."""
        fonts = self.metrics_fonts()
        if not fonts or not text:
            return None
        known = []
        for ch in text:
            font = next((f for f, cov in fonts if ord(ch) in cov), None)
            if font is not None:
                known.append(font.getlength(ch))
        if len(known) < 0.6 * len(text):
            return None
        avg = sum(known) / len(known)
        em = (sum(known) + avg * (len(text) - len(known))) / 100
        return (em + tracking / 1000 * (len(text) - 1)) * size

    def metrics_fonts(self) -> list:
        """(PIL font at 100 pt, code points) for each subset that loads."""
        if not self._fonts and self.metrics:
            from PIL import ImageFont

            for data, cov in self.metrics:
                try:
                    self._fonts.append((ImageFont.truetype(io.BytesIO(data), 100), cov))
                except OSError:
                    continue
        return self._fonts


STYLE_SUFFIX = re.compile(r"(?<=.)(Thin|Hairline|ExtraLight|UltraLight|Light|Book|Regular|Medium|SemiBold|Semibold|"
                          r"DemiBold|Demi|ExtraBold|UltraBold|Heavy|Black|Bold|Italic|Oblique)$")


def family_name(base_font: str) -> str:
    """'OEEXHW+Roboto-Black' -> 'Roboto'; 'SFProDisplay-Bold' -> 'SF Pro Display'; 'TimesNewRomanPSMT' -> 'Times New Roman'."""
    n = clean_font_name(base_font)
    n = re.split(r"[-,]", n, maxsplit=1)[0]
    n = re.sub(r"(PSMT|PS|MT)$", "", n) or n
    while STYLE_SUFFIX.search(n):
        n = STYLE_SUFFIX.sub("", n)
    n = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", n)
    return n.strip() or clean_font_name(base_font)


def face_key(base_font: str) -> tuple[str, int, bool]:
    return family_name(base_font), weight_of(base_font), is_italic(base_font)


def style_name(base_font: str) -> str:
    """'Poppins-SemiBold' -> 'SemiBold'; 'SFProDisplay-SemiboldItalic' -> 'Semibold Italic'; no suffix -> ''."""
    n = clean_font_name(base_font)
    if "-" not in n and "," not in n:
        return ""
    style = re.split(r"[-,]", n, maxsplit=1)[1].replace("MT", "").strip()
    return re.sub(r"(?<=[a-z])(Italic|Oblique)$", r" \1", style) or ""


# ------------------------------------------------------------------ ToUnicode / encodings


def _hex_str(h: str) -> str:
    raw = bytes.fromhex(h)
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return ""


def parse_tounicode(data: bytes) -> dict[int, str]:
    """bfchar / bfrange entries of a ToUnicode CMap: character code -> text."""
    text = data.decode("latin-1")
    out: dict[int, str] = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            out[int(src, 16)] = _hex_str(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, rest in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]*>)", block):
            lo_i, hi_i = int(lo, 16), int(hi, 16)
            if hi_i - lo_i > 0xFFFF:
                continue
            if rest.startswith("["):
                for i, dst in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", rest)):
                    out[lo_i + i] = _hex_str(dst)
            else:
                start = _hex_str(rest[1:-1])
                if not start:
                    continue
                for i in range(hi_i - lo_i + 1):
                    out[lo_i + i] = start[:-1] + chr(ord(start[-1]) + i)
    return {k: v for k, v in out.items() if v}


def _ref_xref(doc: pymupdf.Document, xref: int, key: str) -> int | None:
    kind, val = doc.xref_get_key(xref, key)
    if kind == "xref":
        return int(val.split()[0])
    if kind == "array" and key == "DescendantFonts":
        m = re.search(r"(\d+)\s+0\s+R", val)
        return int(m.group(1)) if m else None
    return None


def _tounicode(doc: pymupdf.Document, xref: int) -> dict[int, str]:
    tx = _ref_xref(doc, xref, "ToUnicode")
    if not tx:
        return {}
    try:
        return parse_tounicode(doc.xref_stream(tx) or b"")
    except Exception:  # noqa: BLE001 - malformed CMap: fall back to the encoding
        return {}


def _differences(doc: pymupdf.Document, xref: int) -> dict[int, str]:
    """code -> glyph name from an /Encoding dictionary's /Differences."""
    kind, val = doc.xref_get_key(xref, "Encoding")
    if kind == "xref":
        kind, val = doc.xref_get_key(int(val.split()[0]), "Differences")
    elif kind == "dict":
        m = re.search(r"/Differences\s*\[(.*?)\]", val, re.S)
        kind, val = ("array", f"[{m.group(1)}]") if m else ("null", "")
    else:
        return {}
    if kind != "array":
        return {}
    out, code = {}, 0
    for tok in re.findall(r"/[^\s/\[\]]+|\d+", val):
        if tok.startswith("/"):
            out[code] = tok[1:]
            code += 1
        else:
            code = int(tok)
    return out


def _descriptor_metrics(doc: pymupdf.Document, xref: int) -> tuple[float, float] | None:
    """(ascent, descent) in 1/1000 em from the FontDescriptor (descent negative)."""
    fd = _ref_xref(doc, xref, "FontDescriptor")
    if fd is None:
        d = _ref_xref(doc, xref, "DescendantFonts")
        fd = _ref_xref(doc, d, "FontDescriptor") if d else None
    if fd is None:
        return None
    try:
        a = float(doc.xref_get_key(fd, "Ascent")[1])
        d = float(doc.xref_get_key(fd, "Descent")[1])
    except (TypeError, ValueError):
        return None
    return (a, d) if a > 0 else None


# ------------------------------------------------------------------ rebuilding


def _set_metrics_and_names(font: TTFont, family: str, weight: int, italic: bool, upm: int,
                           ascent: int, descent: int) -> None:
    """Consistent vertical metrics on every platform (hhea = typo = win) plus the tables browsers require."""
    style = ("Bold " if weight >= 700 else "") + ("Italic" if italic else "") or "Regular"
    fb = FontBuilder(font=font)
    fb.setupNameTable({"familyName": family, "styleName": style.strip(), "psName": re.sub(r"\W", "", family) + "-PF"})
    if "post" not in font:
        fb.setupPost(keepGlyphNames=False)
    else:
        font["post"].formatType = 3.0
        font["post"].mapping = {}
    os2 = font.get("OS/2")
    if os2 is None or getattr(os2, "version", 0) < 1:
        fb.setupOS2(usWeightClass=weight, fsType=0)
        os2 = font["OS/2"]
    os2.usWeightClass = weight
    os2.sTypoAscender, os2.sTypoDescender, os2.sTypoLineGap = ascent, descent, 0
    os2.usWinAscent, os2.usWinDescent = max(0, ascent), max(0, -descent)
    os2.fsType = 0
    sel = (os2.fsSelection | (1 << 7)) & ~((1 << 0) | (1 << 5) | (1 << 6))  # USE_TYPO_METRICS; style bits below
    os2.fsSelection = sel | (1 if italic else 0) | (1 << 5 if weight >= 700 else 0) |         (1 << 6 if weight < 700 and not italic else 0)
    if os2.version < 4:
        os2.version = 4
        for attr, default in (("sxHeight", 0), ("sCapHeight", 0), ("usDefaultChar", 0), ("usBreakChar", 32),
                              ("usMaxContext", 0), ("ulCodePageRange1", 1), ("ulCodePageRange2", 0)):
            if not hasattr(os2, attr):
                setattr(os2, attr, default)
    hhea = font["hhea"]
    hhea.ascent, hhea.descent, hhea.lineGap = ascent, descent, 0
    font["head"].unitsPerEm = upm
    font["head"].macStyle = (1 if weight >= 700 else 0) | (2 if italic else 0)


def _set_cmap(font: TTFont, uni_to_glyph: dict[int, str]) -> None:
    table = newTable("cmap")
    table.tableVersion = 0
    sub = CmapSubtable.newSubtable(4)
    sub.platformID, sub.platEncID, sub.language = 3, 1, 0
    sub.cmap = {u: g for u, g in uni_to_glyph.items() if u <= 0xFFFF}
    table.tables = [sub]
    if any(u > 0xFFFF for u in uni_to_glyph):
        sub12 = CmapSubtable.newSubtable(12)
        sub12.platformID, sub12.platEncID, sub12.language = 3, 10, 0
        sub12.cmap = dict(uni_to_glyph)
        table.tables.append(sub12)
    font["cmap"] = table


def _from_truetype(content: bytes, code_map: dict[int, str], cid_keyed: bool, gid_map: dict[int, int] | None):
    font = TTFont(io.BytesIO(content), recalcBBoxes=False, recalcTimestamp=False)
    upm = font["head"].unitsPerEm or 1000
    font.native_k = (font["hhea"].ascent + font["hhea"].descent) / 2 / upm  # as the original font sets type
    order = font.getGlyphOrder()
    uni: dict[int, str] = {}
    existing = font["cmap"].tables if "cmap" in font else []
    for t in existing:  # a real Unicode cmap survives in some subsets
        if t.platformID in (0, 3) and t.platEncID in (1, 10) or t.platformID == 0:
            for cp, g in (t.cmap or {}).items():
                if cp >= 0x20:
                    uni.setdefault(cp, g)
    if cid_keyed:
        for cid, text in code_map.items():
            gid = gid_map.get(cid, cid) if gid_map else cid
            if len(text) == 1 and 0 < gid < len(order):
                uni[ord(text)] = order[gid]
    else:
        by_code: dict[int, str] = {}
        for t in existing:
            if (t.platformID, t.platEncID) == (3, 0):
                for c, g in (t.cmap or {}).items():
                    by_code.setdefault(c & 0xFF if c >= 0xF000 else c, g)
            elif (t.platformID, t.platEncID) == (1, 0):
                for c, g in (t.cmap or {}).items():
                    by_code.setdefault(c, g)
        for code, g in by_code.items():
            text = code_map.get(code) or _winansi(code)
            if text and len(text) == 1:
                uni[ord(text)] = g
    uni = _drawn_only(font, uni)
    for tag in DROP_TABLES:
        if tag in font:
            del font[tag]
    return font, uni


def _drawn_only(font: TTFont, uni: dict[int, str]) -> dict[int, str]:
    """Characters whose glyph really has an outline. A subset often keeps the whole font's character map but only the
    shapes the PDF used: unicode-range would then claim letters that draw as blanks, instead of falling back."""
    if "glyf" not in font:
        return uni
    glyf = font["glyf"]

    def drawn(name: str, depth: int = 0) -> bool:
        g = glyf[name]
        if g.numberOfContours == 0:
            return False
        if g.isComposite():
            return depth < 4 and all(drawn(c.glyphName, depth + 1) for c in g.components)
        return True

    return {u: g for u, g in uni.items() if u >= 0x20 and (chr(u).isspace() or drawn(g))}


def _from_cff(content: bytes, code_map: dict[int, str], differences: dict[int, str]):
    from fontTools.cffLib import CFFFontSet

    cff = CFFFontSet()
    cff.decompile(io.BytesIO(content), None)
    top = cff[cff.fontNames[0]]
    if hasattr(top, "ROS"):
        raise ValueError("CID-keyed CFF")
    names = list(top.charset)
    upm = round(1 / top.FontMatrix[0]) if top.FontMatrix and top.FontMatrix[0] else 1000
    priv = top.Private
    widths = {}
    for n in names:
        cs = top.CharStrings[n]
        ext = T2WidthExtractor(getattr(priv, "Subrs", []), cs.globalSubrs,
                               getattr(priv, "nominalWidthX", 0), getattr(priv, "defaultWidthX", 0))
        try:
            ext.execute(cs)
            widths[n] = int(round(ext.width))
        except Exception:  # noqa: BLE001
            widths[n] = upm // 2
    present = set(names)
    uni: dict[int, str] = {}
    for n in names:
        text = agl.toUnicode(n)
        if text and len(text) == 1:
            uni.setdefault(ord(text), n)
    for code, text in code_map.items():  # ToUnicode by character code, through the PDF encoding
        name = differences.get(code) or agl.UV2AGL.get(ord(_winansi(code) or "\0"))
        if name in present and len(text) == 1:
            uni[ord(text)] = name
    fb = FontBuilder(unitsPerEm=upm, isTTF=False)
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap(uni)
    fb.setupHorizontalMetrics({n: (widths[n], 0) for n in names})
    fb.setupHorizontalHeader(ascent=int(upm * 0.8), descent=-int(upm * 0.2))
    fb.setupNameTable({"familyName": "PF", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost(keepGlyphNames=False)
    fb.setupMaxp()
    table = newTable("CFF ")
    table.cff = cff
    fb.font["CFF "] = table
    fb.font.sfntVersion = "OTTO"  # FontBuilder only sets this in setupCFF, which rebuilds charstrings we already have
    fb.font["maxp"].tableVersion = 0x00005000
    bbox = list(getattr(top, "FontBBox", [0, -200, upm, 800]))
    head = fb.font["head"]
    head.xMin, head.yMin, head.xMax, head.yMax = (int(v) for v in bbox)
    return fb.font, uni


def build_web_font(doc: pymupdf.Document, xref: int, family: str, weight: int, italic: bool) -> dict | None:
    """Rebuild one embedded font as a browser-loadable OpenType font. None when it can't be done safely."""
    try:
        _name, ext, ftype, content = doc.extract_font(xref)
    except Exception:  # noqa: BLE001
        return None
    if not content or ext not in ("ttf", "cff"):
        return None
    code_map = _tounicode(doc, xref)
    try:
        if ext == "ttf":
            gid_map = None
            cid_keyed = ftype == "Type0"
            if cid_keyed:
                desc = _ref_xref(doc, xref, "DescendantFonts")
                cmap_ref = _ref_xref(doc, desc, "CIDToGIDMap") if desc else None
                if cmap_ref:
                    raw = doc.xref_stream(cmap_ref) or b""
                    gid_map = {i: int.from_bytes(raw[i * 2:i * 2 + 2], "big") for i in range(len(raw) // 2)}
            font, uni = _from_truetype(content, code_map, cid_keyed, gid_map)
        else:
            font, uni = _from_cff(content, code_map, _differences(doc, xref))
        if not uni:
            return None
        _set_cmap(font, uni)
        upm = font["head"].unitsPerEm or 1000
        native_k = getattr(font, "native_k", None)
        desc = _descriptor_metrics(doc, xref)
        if desc:
            ascent, descent = int(desc[0] * upm / 1000), int(desc[1] * upm / 1000)
        else:
            ascent, descent = font["hhea"].ascent or int(upm * 0.8), font["hhea"].descent or -int(upm * 0.2)
        _set_metrics_and_names(font, family, weight, italic, upm, ascent, descent)
        buf = io.BytesIO()
        font.flavor = "woff"
        font.save(buf)
    except Exception as e:  # noqa: BLE001 - any font we can't rebuild falls back to a similar web font
        log.info("font %s (%s) not rebuilt: %s", xref, ext, e)
        return None
    k = round((ascent + descent) / 2 / upm, 4)
    return {"bytes": buf.getvalue(), "codepoints": sorted(uni), "baselineK": k,
            "nativeK": round(native_k, 4) if native_k else k}


def unicode_range(codepoints: list[int]) -> str:
    ranges: list[list[int]] = []
    for cp in codepoints:
        if ranges and cp == ranges[-1][1] + 1:
            ranges[-1][1] = cp
        else:
            ranges.append([cp, cp])
    return ", ".join(f"U+{a:X}" if a == b else f"U+{a:X}-{b:X}" for a, b in ranges)


# ------------------------------------------------------------------ Google Fonts


class GoogleFonts:
    """Which families (and weights) Google Fonts serves; answers are cached for the import."""

    CSS = "https://fonts.googleapis.com/css2"

    def __init__(self) -> None:
        self._family: dict[str, bool] = {}
        self._client = httpx.Client(timeout=10, headers={"User-Agent": "Mozilla/5.0 PressFlow"})

    def has(self, family: str) -> bool:
        if family not in self._family:
            try:
                r = self._client.get(self.CSS, params={"family": family})
                self._family[family] = r.status_code == 200 and "fonts.gstatic.com" in r.text
            except httpx.HTTPError:
                self._family[family] = False
        return self._family[family]

    def has_face(self, family: str, weight: int, italic: bool) -> bool:
        try:
            r = self._client.get(self.CSS, params={"family": f"{family}:ital,wght@{int(italic)},{weight}"})
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def close(self) -> None:
        self._client.close()


def google_css_import(faces: list[FaceInfo]) -> str | None:
    by_family: dict[str, set[tuple[int, int]]] = {}
    for f in faces:
        if f.google:
            by_family.setdefault(f.family, set()).add((int(f.italic), f.weight))
    for fam in FALLBACK_GOOGLE.values():
        by_family.setdefault(fam, set()).update({(0, 400), (0, 700)})
    if not by_family:
        return None
    parts = []
    for fam, axes in sorted(by_family.items()):
        spec = ";".join(f"{i},{w}" for i, w in sorted(axes))
        parts.append(f"family={fam.replace(' ', '+')}:ital,wght@{spec}")
    return f"{GoogleFonts.CSS}?{'&'.join(parts)}&display=swap"


def fallback_family(classification: str) -> str:
    return FALLBACK_GOOGLE.get(classification, "Inter")


def collect_faces(doc: pymupdf.Document, google: GoogleFonts | None, progress=None) -> dict[str, FaceInfo]:
    """Every font the document uses, keyed by base font name (subset prefix stripped)."""
    faces: dict[tuple[str, int, bool], FaceInfo] = {}
    by_base: dict[str, FaceInfo] = {}
    seen_xref: set[int] = set()
    for pi, page in enumerate(doc):
        for xref, _ext, _ftype, base, *_ in page.get_fonts(full=True):
            if xref in seen_xref or not base:
                continue
            seen_xref.add(xref)
            fam, weight, italic = face_key(base)
            key = (fam, weight, italic)
            face = faces.get(key)
            if face is None:
                face = FaceInfo(family=fam, weight=weight, italic=italic, classification=classify_font(base),
                                style_name=style_name(base))
                if google is not None and google.has(fam):
                    face.google = google.has_face(fam, weight, italic)
                faces[key] = face
            by_base[clean_font_name(base)] = face
            built = build_web_font(doc, xref, fam, weight, italic)
            if built:
                # Google serves the original font, so its own metrics apply; a rebuilt subset uses the ones set here.
                face.baseline_k = built["nativeK"] if face.google else built["baselineK"]
                face.metrics.append((built["bytes"], set(built["codepoints"])))
                if not face.google:
                    face.files.append({"id": f"f{xref}", "bytes": built["bytes"],
                                       "unicodeRange": unicode_range(built["codepoints"])})
        if progress and pi % 10 == 0:
            progress(pi / max(1, doc.page_count), f"Reading fonts (page {pi + 1})")
    return by_base
