"""One reference PDF page -> editable PageElements that reproduce it.

Live text becomes text frames set in the page's own fonts (see webfonts.py), placed by baseline. Placed photographs
are extracted at full resolution as movable image frames. Everything else (vector artwork, gradients, tints,
outlined type) is rendered into a background picture from which the text and extracted photographs have been
removed, so nothing is drawn twice and nothing is lost.
"""

from __future__ import annotations

import io
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from statistics import median
from typing import Callable

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageStat

from app.services.reference_analysis.fonts import classify_font, clean_font_name, preview_font_for
from app.services.reference_import.webfonts import DEFAULT_BASELINE_K, FaceInfo, face_key, style_name

log = logging.getLogger("pressflow.reference_import.pages")

BACKGROUND_PPI = 200
MAX_IMAGE_PPI = 400
TARGET_IMAGE_PPI = 300

# save_asset(data, content_type, width_px, height_px, kind, name) -> asset id
SaveAsset = Callable[[bytes, str, int, int, str, str], str]


@dataclass
class Piece:
    """A run of one style on one line."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    baseline: float
    size: float
    color: str
    font: str
    angle: float = 0.0
    tracking: float = 0.0  # thousandths of an em, when measured (outlined type)

    @property
    def key(self) -> tuple:
        return (clean_font_name(self.font), round(self.size, 1), self.color)


@dataclass
class Frame:
    pieces: list[Piece] = field(default_factory=list)

    @property
    def first(self) -> Piece:
        return self.pieces[0]


def _hex(color: int) -> str:
    return f"#{color:06X}"


def _page_frame(page: pymupdf.Page) -> tuple[float, float, float, float]:
    from app.services.reference_analysis.extract import _page_frame as frame

    return frame(page)


# ------------------------------------------------------------------ text


def _pieces(page: pymupdf.Page, ox: float, oy: float) -> tuple[list[list[Piece]], list[pymupdf.Rect]]:
    """Text runs grouped by PDF block, plus every visible line's rectangle (for removal from the background)."""
    d = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    blocks: list[list[Piece]] = []
    rects: list[pymupdf.Rect] = []
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        out: list[Piece] = []
        for ln in block.get("lines", []):
            spans = [s for s in ln.get("spans", []) if s.get("text") and s.get("alpha", 255) > 0]
            if not "".join(s["text"] for s in spans).strip():
                continue
            rects.append(pymupdf.Rect(ln["bbox"]))
            dx, dy = ln.get("dir", (1, 0))
            angle = round(math.degrees(math.atan2(dy, dx)), 1)
            # Runs of one size and colour stay together; weight changes inside a run take the dominant face.
            runs: list[list[dict]] = []
            for s in spans:
                k = (round(s["size"], 1), s["color"])
                if runs and ((round(runs[-1][0]["size"], 1), runs[-1][0]["color"]) == k or not s["text"].strip()):
                    runs[-1].append(s)
                else:
                    runs.append([s])
            for run in runs:
                text = "".join(s["text"] for s in run)
                if not text.strip():
                    continue
                weights: Counter = Counter()
                for s in run:
                    weights[s["font"]] += len(s["text"].strip())
                font = weights.most_common(1)[0][0]
                main = next(s for s in run if s["font"] == font)
                x0 = min(s["bbox"][0] for s in run) - ox
                y0 = min(s["bbox"][1] for s in run) - oy
                x1 = max(s["bbox"][2] for s in run) - ox
                y1 = max(s["bbox"][3] for s in run) - oy
                out.append(Piece(text=text.strip(), x0=x0, y0=y0, x1=x1, y1=y1,
                                 baseline=main["origin"][1] - oy, size=float(main["size"]), color=_hex(main["color"]),
                                 font=font, angle=angle))
        if out:
            blocks.append(merge_words(out))
    return blocks, rects


def merge_words(pieces: list[Piece]) -> list[Piece]:
    """Widely spaced justified lines come out of the PDF word by word: runs of one style on one baseline are one line."""
    out: list[Piece] = []
    for p in pieces:
        last = out[-1] if out else None
        if (last is not None and p.angle == last.angle == 0 and p.key == last.key
                and abs(p.baseline - last.baseline) <= 0.3 * p.size and 0 <= p.x0 - last.x1 <= 1.6 * p.size):
            last.text = f"{last.text} {p.text}"
            last.x1, last.y0, last.y1 = p.x1, min(last.y0, p.y0), max(last.y1, p.y1)
        else:
            out.append(p)
    return out


def _frames(blocks: list[list[Piece]]) -> list[Frame]:
    """Consecutive runs of one style stacked at a steady leading form one frame; everything else stands alone."""
    frames: list[Frame] = []
    for block in blocks:
        cur: Frame | None = None
        for p in block:
            if cur is not None and p.angle == 0 and cur.first.angle == 0 and p.key == cur.first.key:
                last = cur.pieces[-1]
                step = p.baseline - last.baseline
                leading = cur.pieces[1].baseline - cur.pieces[0].baseline if len(cur.pieces) > 1 else None
                stacked = 0.8 * p.size <= step <= 2.6 * p.size and p.x0 < max(q.x1 for q in cur.pieces) and \
                    p.x1 > min(q.x0 for q in cur.pieces)
                if stacked and (leading is None or abs(step - leading) <= 0.15 * leading):
                    cur.pieces.append(p)
                    continue
            cur = Frame([p])
            frames.append(cur)
    return frames


def _alignment(lines: list[Piece]) -> str:
    if len(lines) < 2:
        return "left"
    tol = max(1.5, 0.12 * lines[0].size)
    x0s, x1s = [p.x0 for p in lines], [p.x1 for p in lines]
    left = max(x0s) - min(x0s) <= tol
    right_all = max(x1s) - min(x1s) <= tol
    # Justified: most lines but the last reach the right edge (one ending at a hyphen can fall a little short).
    body = x1s[:-1]
    right_body = len(lines) >= 3 and sum(max(body) - x <= tol for x in body) >= max(2, 0.7 * len(body))
    centre = max(a + b for a, b in zip(x0s, x1s)) / 2 - min(a + b for a, b in zip(x0s, x1s)) / 2 <= tol
    if left and (right_body and not right_all or right_all and len(lines) >= 3):
        return "justify"  # every line full: a paragraph that runs on to the next column
    if left:
        return "left"
    if right_all:
        return "right"
    return "center" if centre else "left"


def _role(size: float, body: float) -> str:
    if size >= 1.8 * body:
        return "headline"
    if size >= 1.25 * body:
        return "deck"
    if size <= 0.85 * body:
        return "caption"
    return "body"


def text_element(frame: Frame, faces: dict[str, FaceInfo], body_size: float, z: int, idx: int) -> dict:
    lines = frame.pieces
    p0 = lines[0]
    face = faces.get(clean_font_name(p0.font))
    if face is None:
        fam, w, it = face_key(p0.font)
        face = FaceInfo(family=fam, weight=w, italic=it, classification=classify_font(p0.font),
                        style_name=style_name(p0.font))
    size = p0.size
    leading = median(b.baseline - a.baseline for a, b in zip(lines, lines[1:])) if len(lines) > 1 else round(size * 1.2, 2)
    align = _alignment(lines)
    x0, x1 = min(p.x0 for p in lines), max(p.x1 for p in lines)
    # Lines keep the PDF's breaks (the browser would break a paragraph differently from InDesign's composer).
    text = "\n".join(p.text for p in lines)
    tracking = median(p.tracking for p in lines)
    if align == "justify":
        # InDesign squeezed word spaces to justify, CSS can only stretch them: letter-spacing tightened just enough
        # for the tightest line keeps the frame to the original measure without any line wrapping.
        squeeze = [((x1 - x0) / 1.004 - w) / (len(p.text) - 1) / size * 1000
                   for p in lines if len(p.text) > 1 and (w := face.text_width(p.text, size, tracking))]
        tracking += max(-40.0, min([0.0, *squeeze]))
        slack = 0.0
    else:
        # As wide as the widest line set naturally in its font, so no line wraps by accident.
        natural = max((face.text_width(p.text, size, tracking) or 0) for p in lines)
        slack = max(0.0, natural * 1.004 - (x1 - x0)) + 0.01 * (x1 - x0) + 1
    if align == "right":
        x0 -= slack
    elif align == "center":
        x0, x1 = x0 - slack / 2, x1 + slack / 2
    else:
        x1 += slack
    k = face.baseline_k or DEFAULT_BASELINE_K
    h = len(lines) * leading + 0.25 * leading
    el = {
        "id": f"el_imp_{idx}", "kind": "text", "role": _role(size, body_size), "name": _name(text), "z": z,
        "styleRole": _role(size, body_size), "text": text, "literal": True,
        "style": {"fontFamily": face.family, **({"fontStyle": face.style_name} if face.style_name else {}),
                  "previewFont": preview_font_for(face.classification),
                  "size": round(size, 2), "leading": round(leading, 2), "weight": face.weight, "italic": face.italic,
                  "color": p0.color, "align": align, "tracking": round(tracking, 1),
                  "uppercase": False},
    }
    if p0.angle:
        # A rotated line: the frame is laid out unrotated around the centre of the rotated text's bounds.
        cx, cy = (p0.x0 + p0.x1) / 2, (p0.y0 + p0.y1) / 2
        rad = math.radians(p0.angle)
        bw, bh = p0.x1 - p0.x0, p0.y1 - p0.y0
        length = bh if abs(math.sin(rad)) > 0.9 else bw if abs(math.cos(rad)) > 0.9 else math.hypot(bw, bh) * 0.85
        w = length * 1.05 + 1.5
        el.update(x=round(cx - w / 2, 2), y=round(cy - h / 2, 2), w=round(w, 2), h=round(h, 2), rotation=p0.angle)
        el["style"]["align"] = "center"
    else:
        top = p0.baseline - (leading / 2 + k * size)
        el.update(x=round(x0, 2), y=round(top, 2), w=round(x1 - x0, 2), h=round(h, 2))
    return el


def _name(text: str) -> str:
    words = re.sub(r"\s+", " ", text).strip().split(" ")
    return " ".join(words[:5]) + ("…" if len(words) > 5 else "") or "Text"


# ------------------------------------------------------------------ images


def _image_bytes(doc: pymupdf.Document, xref: int) -> Image.Image | None:
    try:
        pix = pymupdf.Pixmap(doc, xref)
        info = doc.extract_image(xref) or {}
        smask = info.get("smask") or 0
        if pix.colorspace is None:  # stencil mask: drawn with the fill colour, stays in the background
            return None
        if pix.colorspace.n not in (1, 3):
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        if smask:
            mask = pymupdf.Pixmap(doc, smask)
            if mask.width == pix.width and mask.height == pix.height:
                pix = pymupdf.Pixmap(pix, mask)
        mode = {1: "L", 3: "RGB"}[pix.colorspace.n]
        if pix.alpha:
            img = Image.frombytes(mode + "A" if mode == "L" else "RGBA", (pix.width, pix.height), pix.samples)
        else:
            img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        return img.convert("RGBA") if img.mode in ("LA", "RGBA") else img.convert("RGB")
    except Exception as e:  # noqa: BLE001 - anything unusual stays in the background picture
        log.info("image %s not extracted: %s", xref, e)
        return None


def _encode(img: Image.Image) -> tuple[bytes, str]:
    buf = io.BytesIO()
    if img.mode == "RGBA" and img.getextrema()[3][0] < 250:
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue(), "image/png"
    img.convert("RGB").save(buf, "JPEG", quality=90, optimize=True)
    return buf.getvalue(), "image/jpeg"


def _shows_as_is(page: pymupdf.Page, img: Image.Image, rect: tuple[float, float, float, float]) -> bool:
    """Whether the picture appears on the page as extracted. A fade or mask from the page's graphics state, a blend
    mode, or artwork laid over it all change what shows; such a picture stays in the background, where it looks
    exactly as printed."""
    pix = page.get_pixmap(matrix=pymupdf.Matrix(50 / 72, 50 / 72), clip=pymupdf.Rect(rect), alpha=False)
    if pix.width < 4 or pix.height < 4:
        return True
    shown = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    mine = img.convert("RGBA").resize(shown.size, Image.Resampling.BILINEAR)
    mine = Image.alpha_composite(shown.convert("RGBA"), mine).convert("RGB")
    diff = np.abs(np.asarray(shown, dtype=np.int16) - np.asarray(mine, dtype=np.int16)).sum(axis=2)
    return float((diff > 90).mean()) <= 0.15


@dataclass
class ImageCache:
    by_key: dict[tuple, str] = field(default_factory=dict)


def image_elements(doc: pymupdf.Document, page: pymupdf.Page, ox: float, oy: float, W: float, H: float, bleed: float,
                   save: SaveAsset, cache: ImageCache, z0: int) -> tuple[list[dict], set[int]]:
    """Placed photographs as image frames (cropped to what shows on the page). Returns elements and their xrefs."""
    elements, xrefs = [], set()
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:  # noqa: BLE001
        return [], set()
    skipped: set[int] = set()
    placed: list[tuple[int, dict]] = []
    for info in infos:
        xref = info.get("xref") or 0
        a, b, c, d, _e, _f = info["transform"]
        if not xref or abs(b) > 1e-3 or abs(c) > 1e-3:
            skipped.add(xref)  # inline/form images and rotated placements stay in the background
            continue
        placed.append((xref, info))
    for xref, info in placed:
        if xref in skipped:
            continue  # the same picture is also placed in a way we keep in the background: keep every use there
        x0, y0, x1, y1 = info["bbox"]
        x0, y0, x1, y1 = x0 - ox, y0 - oy, x1 - ox, y1 - oy
        vx0, vy0, vx1, vy1 = max(x0, -bleed), max(y0, -bleed), min(x1, W + bleed), min(y1, H + bleed)
        if vx1 - vx0 < 6 or vy1 - vy0 < 6:
            continue
        a, _b, _c, d, _e, _f = info["transform"]
        crop = (round((vx0 - x0) / (x1 - x0), 4), round((vy0 - y0) / (y1 - y0), 4), round((vx1 - x0) / (x1 - x0), 4),
                round((vy1 - y0) / (y1 - y0), 4))
        key = (xref, crop, a < 0, d < 0)
        aid = cache.by_key.get(key)
        if aid is None:
            img = _image_bytes(doc, xref)
            if img is None:
                skipped.add(xref)
                continue
            if a < 0:
                img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if d < 0:
                img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            iw, ih = img.size
            img = img.crop((round(crop[0] * iw), round(crop[1] * ih), max(round(crop[0] * iw) + 1, round(crop[2] * iw)),
                            max(round(crop[1] * ih) + 1, round(crop[3] * ih))))
            if not _shows_as_is(page, img, (vx0 + ox, vy0 + oy, vx1 + ox, vy1 + oy)):
                skipped.add(xref)
                continue
            ppi = img.width / ((vx1 - vx0) / 72)
            if ppi > MAX_IMAGE_PPI:
                f = TARGET_IMAGE_PPI / ppi
                img = img.resize((max(1, round(img.width * f)), max(1, round(img.height * f))), Image.Resampling.LANCZOS)
            data, ctype = _encode(img)
            cover = (vx1 - vx0) * (vy1 - vy0) >= 0.85 * W * H
            aid = save(data, ctype, img.width, img.height, "photo", "Background photo" if cover else "Photo")
            cache.by_key[key] = aid
        cover = (vx1 - vx0) * (vy1 - vy0) >= 0.85 * W * H
        elements.append({"id": f"el_img_{len(elements) + 1}", "kind": "image", "role": "background" if cover else "image",
                         "name": "Background photo" if cover else f"Photo {len(elements) + 1}", "x": round(vx0, 2),
                         "y": round(vy0, 2), "w": round(vx1 - vx0, 2), "h": round(vy1 - vy0, 2), "z": z0 + len(elements),
                         "assetId": aid, "crop": {"scale": 1, "offsetX": 0, "offsetY": 0}})
        xrefs.add(xref)
    return elements, xrefs - skipped


# ------------------------------------------------------------------ background


def render_background(work: pymupdf.Document, index: int, ox: float, oy: float, W: float, H: float, bleed: float,
                      remove_xrefs: set[int], text_rects: list[pymupdf.Rect], ppi: int = BACKGROUND_PPI,
                      glyph_rects: list[pymupdf.Rect] | None = None) -> Image.Image:
    """The page with its live text, extracted photographs and rebuilt outlined type removed, over trim + bleed."""
    page = work[index]
    for xref in remove_xrefs:
        try:
            page.delete_image(xref)
        except Exception:  # noqa: BLE001
            pass
    if text_rects:
        for r in text_rects:
            page.add_redact_annot(r, fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    if glyph_rects:
        # Outlined type rebuilt as live text: the glyph paths wholly inside each line's box go.
        for r in glyph_rects:
            page.add_redact_annot(r, fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=pymupdf.PDF_REDACT_TEXT_NONE)
    if ox > 0 and oy > 0:
        # Crop marks sit wholly in the corners outside the trim; artwork bleeding into a corner only touches it.
        m = page.rect
        e = 1.5  # marks are drawn up to the trim line itself
        for r in ((m.x0, m.y0, ox + e, oy + e), (ox + W - e, m.y0, m.x1, oy + e), (m.x0, oy + H - e, ox + e, m.y1),
                  (ox + W - e, oy + H - e, m.x1, m.y1)):
            page.add_redact_annot(pymupdf.Rect(r), fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=pymupdf.PDF_REDACT_TEXT_NONE)
    clip = pymupdf.Rect(ox - bleed, oy - bleed, ox + W + bleed, oy + H + bleed) & page.rect
    pix = page.get_pixmap(matrix=pymupdf.Matrix(ppi / 72, ppi / 72), clip=clip, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def is_blank(img: Image.Image, paper: str) -> bool:
    small = img.convert("RGB").resize((96, max(1, round(96 * img.height / img.width))))
    stat = ImageStat.Stat(small)
    pr, pg, pb = (int(paper.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    return max(stat.stddev) < 2.0 and all(abs(m - p) < 4 for m, p in zip(stat.mean, (pr, pg, pb)))


def background_element(img: Image.Image, save: SaveAsset, W: float, H: float, bleed: float, page_number: int) -> dict:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    aid = save(buf.getvalue(), "image/png", img.width, img.height, "photo", f"Page {page_number} background")
    return {"id": "el_bg", "kind": "image", "role": "background", "name": "Background", "x": -bleed, "y": -bleed,
            "w": round(W + 2 * bleed, 2), "h": round(H + 2 * bleed, 2), "z": 0, "assetId": aid,
            "crop": {"scale": 1, "offsetX": 0, "offsetY": 0}}


# ------------------------------------------------------------------ live-text pages


def build_live_page(src: pymupdf.Document, work: pymupdf.Document, index: int, faces: dict[str, FaceInfo],
                    profile: dict, save: SaveAsset, cache: ImageCache) -> list[dict]:
    page = src[index]
    ox, oy, W, H = _page_frame(page)
    bleed = float(profile["page"].get("bleedPt") or 0)
    body = float((profile.get("typography") or {}).get("body", {}).get("size") or 10)
    blocks, rects = _pieces(page, ox, oy)
    images, xrefs = image_elements(src, page, ox, oy, W, H, bleed, save, cache, z0=1)
    bg = render_background(work, index, ox, oy, W, H, bleed, xrefs, rects)
    elements: list[dict] = []
    if not is_blank(bg, profile["colors"].get("paper") or "#FFFFFF"):
        elements.append(background_element(bg, save, W, H, bleed, index + 1))
    elements += images
    z = 1 + len(images)
    for i, frame in enumerate(_frames(blocks)):
        elements.append(text_element(frame, faces, body, z + i, i + 1))
    return elements


def has_live_text(page: pymupdf.Page) -> bool:
    try:
        trace = page.get_texttrace()
    except Exception:  # noqa: BLE001
        return bool(page.get_text().strip())
    return any(t.get("type") != 3 and t.get("opacity", 1) > 0 and any(ch[0] > 32 for ch in t.get("chars", ()))
               for t in trace)


# ------------------------------------------------------------------ pages without live text


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Share of `a` covered by `b` (rectangles as x0, y0, x1, y1)."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    area = (a[2] - a[0]) * (a[3] - a[1])
    return max(0.0, w) * max(0.0, h) / area if area > 0 else 0.0


@dataclass
class Row:
    """One line of outlined type: the union of its glyph shapes and the baseline most glyphs sit on."""

    x0: float
    y0: float
    x1: float
    y1: float
    baseline: float


def ink_shapes(page: pymupdf.Page, ox: float, oy: float, W: float, H: float) -> list[tuple[float, float, float, float]]:
    """Bounds of the small filled shapes and clipping paths on a page: outlined type is made of them."""
    try:
        drawings = page.get_drawings(extended=True)
    except Exception:  # noqa: BLE001
        drawings = page.get_drawings()
    out = []
    for d in drawings[:30000]:
        if d.get("type") not in ("f", "fs", "clip"):
            continue
        r = d.get("scissor") if d.get("type") == "clip" else d.get("rect")
        if r is None:
            continue
        x0, y0, x1, y1 = r.x0 - ox, r.y0 - oy, r.x1 - ox, r.y1 - oy
        if 0.3 < x1 - x0 < 0.95 * W and 0.3 < y1 - y0 < 0.2 * H:
            out.append((x0, y0, x1, y1))
    return out


def claim_shapes(boxes: list[tuple[float, float, float, float]], shapes: list[tuple]) -> list[list[tuple]]:
    """Each shape goes to the nearest approximate box whose surroundings contain its centre (none if it's too big)."""
    claims: list[list[tuple]] = [[] for _ in boxes]
    for s in shapes:
        cx, cy = (s[0] + s[2]) / 2, (s[1] + s[3]) / 2
        best: tuple[float, int] | None = None
        for i, (x0, y0, x1, y1) in enumerate(boxes):
            bw, bh = x1 - x0, y1 - y0
            mx, my = 0.06 * bw + 0.3 * bh, 0.35 * bh
            if not (x0 - mx <= cx <= x1 + mx and y0 - my <= cy <= y1 + my):
                continue
            if s[3] - s[1] > 1.6 * bh or s[2] - s[0] > 1.3 * bw:
                continue
            d = math.hypot(max(x0 - cx, 0, cx - x1), max(y0 - cy, 0, cy - y1))
            if best is None or d < best[0]:
                best = (d, i)
        if best is not None:
            claims[best[1]].append(s)
    return claims


def rows_of(shapes: list[tuple]) -> list[Row]:
    rows: list[list[tuple]] = []
    for s in sorted(shapes, key=lambda r: (r[1] + r[3]) / 2):
        if rows:
            ry0, ry1 = min(r[1] for r in rows[-1]), max(r[3] for r in rows[-1])
            overlap = min(ry1, s[3]) - max(ry0, s[1])
            if overlap >= 0.3 * min(ry1 - ry0, s[3] - s[1]):
                rows[-1].append(s)
                continue
        rows.append([s])
    out = []
    for r in rows:
        h = max(s[3] for s in r) - min(s[1] for s in r)
        letters = [s[3] for s in r if s[3] - s[1] >= 0.45 * h] or [s[3] for s in r]
        out.append(Row(min(s[0] for s in r), min(s[1] for s in r), max(s[2] for s in r), max(s[3] for s in r),
                       median(letters)))
    return out


def vision_elements(page: pymupdf.Page, blocks: list[dict], ox: float, oy: float, W: float, H: float,
                    faces: dict[str, FaceInfo], profile: dict, images: list[dict], save: SaveAsset,
                    z0: int) -> list[tuple[dict, tuple]]:
    """Text and logos a vision model located on the rendered page, as (frame, box it covers) pairs.

    The model reads the words; where they are comes from the page itself: its boxes are only approximate, so each is
    snapped to the outlined glyphs it covers, which also give the lines, baselines and the type size. Block
    coordinates are fractions of the whole PDF page; frames are placed relative to the trim. Logos that are already
    extracted photographs are left to those frames."""
    MW, MH = page.rect.width, page.rect.height
    typo = profile.get("typography") or {}
    photos = [(e["x"], e["y"], e["x"] + e["w"], e["y"] + e["h"]) for e in images]
    wanted = []
    for b in blocks:
        x, y, w, h = b["x"] * MW - ox, b["y"] * MH - oy, b["w"] * MW, b["h"] * MH
        rect = (x, y, x + w, y + h)
        if b.get("kind") == "image" and b.get("coverRole") in ("logo", "masthead"):
            if not any(_overlaps(rect, p) > 0.6 for p in photos):
                wanted.append((b, rect))
        elif b.get("kind") == "text" and (b.get("sample") or "").strip():
            wanted.append((b, rect))
    claims = claim_shapes([r for _b, r in wanted], ink_shapes(page, ox, oy, W, H))
    pairs: list[tuple[dict, tuple]] = []
    for i, ((b, rect), shapes) in enumerate(zip(wanted, claims)):
        rows = rows_of(shapes) if len(shapes) >= 2 else []
        if rows:
            rect = (min(r.x0 for r in rows), min(r.y0 for r in rows), max(r.x1 for r in rows), max(r.y1 for r in rows))
        x, y, x1, y1 = rect
        w, h = x1 - x, y1 - y
        pad = max(1.5, 0.12 * (h / max(1, len(rows))))
        box = (x - pad, y - pad, x1 + pad, y1 + pad)
        if b.get("kind") == "image":
            clip = pymupdf.Rect(x + ox, y + oy, x1 + ox, y1 + oy)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(300 / 72, 300 / 72), clip=clip, alpha=False)
            aid = save(pix.tobytes("png"), "image/png", pix.width, pix.height, "logo",
                       (b.get("description") or "Logo")[:60])
            pairs.append(({"id": f"el_logo_{i + 1}", "kind": "image", "role": "image", "name": "Logo",
                           "x": round(x, 2), "y": round(y, 2), "w": round(w, 2), "h": round(h, 2), "z": z0 + i,
                           "assetId": aid, "crop": {"scale": 1, "offsetX": 0, "offsetY": 0}}, box))
            continue
        text = b["sample"].strip()
        lines = text.split("\n")
        role = {"headline": "headline", "deck": "deck"}.get(b.get("role"), "kicker")
        detected = (typo.get(role) or typo.get("headline") or {}).get("detectedFont") or ""
        face = faces.get(clean_font_name(detected))
        if rows and len(rows) == len(lines):
            fits = [(r.x1 - r.x0) / a for r, ln in zip(rows, lines) if (a := (face.text_width(ln, 1) if face else None))]
            size = median(fits) if fits else (rows[0].y1 - rows[0].y0) / 0.9
            leading = median(r2.baseline - r1.baseline for r1, r2 in zip(rows, rows[1:])) if len(rows) > 1 else size * 1.2
        else:
            leading = h / max(1, len(rows) or len(lines))
            size = float(b.get("fontSize") or leading / 1.15)
            longest = max(lines, key=len)
            if rows and (a := (face.text_width(longest, 1) if face else None)):
                size = min(size, max(r.x1 - r.x0 for r in rows) / a)
        size = round(min(size, leading / 0.95), 2)
        k = face.baseline_k if face else DEFAULT_BASELINE_K
        top = rows[0].baseline - (leading / 2 + k * size) if rows else y
        classification = face.classification if face else (typo.get(role) or {}).get("classification", "sans")
        style = {"previewFont": preview_font_for(classification), "size": size, "leading": round(leading, 2),
                 "weight": int(b.get("weight") or (face.weight if face else 400)), "italic": False,
                 "color": (b.get("color") or "#000000").upper(), "align": b.get("align") or "left", "tracking": 0,
                 "uppercase": False}
        if face:
            style["fontFamily"] = face.family
            if face.style_name and face.weight == style["weight"]:
                style["fontStyle"] = face.style_name
        slack = w * 0.08 + 2
        fx = x - slack if style["align"] == "right" else x - slack / 2 if style["align"] == "center" else x
        n = max(len(rows), len(lines))
        pairs.append(({"id": f"el_vtx_{i + 1}", "kind": "text", "role": role, "styleRole": role, "name": _name(text),
                       "text": text, "literal": True, "x": round(fx, 2), "y": round(top, 2), "w": round(w + slack, 2),
                       "h": round(n * leading + 0.25 * leading, 2), "z": z0 + i, "style": style}, box))
    return pairs


def stack_frames(pieces: list[Piece]) -> list[Frame]:
    """Lines of one style stacked at a steady leading join one frame, wherever they sit in reading order."""
    frames: list[Frame] = []
    for p in sorted(pieces, key=lambda q: (q.baseline, q.x0)):
        home = None
        for f in frames:
            last = f.pieces[-1]
            step = p.baseline - last.baseline
            if last.key != p.key or not 0.8 * p.size <= step <= 2.6 * p.size:
                continue
            if not (p.x0 < max(q.x1 for q in f.pieces) and p.x1 > min(q.x0 for q in f.pieces)):
                continue
            lead = f.pieces[1].baseline - f.pieces[0].baseline if len(f.pieces) > 1 else None
            if lead is None or abs(step - lead) <= 0.15 * lead:
                home = f
                break
        if home:
            home.pieces.append(p)
        else:
            frames.append(Frame([p]))
    return frames


def _logo_element(page: pymupdf.Page, rect: tuple, ox: float, oy: float, save: SaveAsset, z: int, idx: int,
                  name: str = "Logo") -> dict:
    x0, y0, x1, y1 = rect
    pad = 1.0
    clip = pymupdf.Rect(x0 + ox - pad, y0 + oy - pad, x1 + ox + pad, y1 + oy + pad)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(300 / 72, 300 / 72), clip=clip, alpha=False)
    aid = save(pix.tobytes("png"), "image/png", pix.width, pix.height, "logo", name[:60])
    return {"id": f"el_logo_{idx}", "kind": "image", "role": "image", "name": name, "x": round(x0 - pad, 2),
            "y": round(y0 - pad, 2), "w": round(x1 - x0 + 2 * pad, 2), "h": round(y1 - y0 + 2 * pad, 2), "z": z,
            "assetId": aid, "crop": {"scale": 1, "offsetX": 0, "offsetY": 0}}


def _to_px(boxes: list[tuple], bleed: float, img: Image.Image) -> list[tuple[int, int, int, int]]:
    """Trim-space boxes -> pixels of a background picture that starts at -bleed."""
    s = BACKGROUND_PPI / 72
    px = [(max(0, round((x0 + bleed) * s)), max(0, round((y0 + bleed) * s)), min(img.width, round((x1 + bleed) * s)),
           min(img.height, round((y1 + bleed) * s))) for x0, y0, x1, y1 in boxes]
    return [b for b in px if b[2] > b[0] and b[3] > b[1]]


def build_flat_page(src: pymupdf.Document, work: pymupdf.Document, index: int, faces: dict[str, FaceInfo],
                    profile: dict, save: SaveAsset, cache: ImageCache, llm,
                    vision_blocks: Callable[[], list[dict]]) -> list[dict] | None:
    """A page without live text. Outlined type is found from its glyph shapes and read by the vision model
    (outlined.py); a flattened picture falls back to the model's own (approximate) boxes. Photographs are extracted
    as on any page. Text and logos are rebuilt only where they can be cleared from the background without a trace;
    the rest stays as artwork. None when nothing was found."""
    from app.services.reference_import.outlined import family_for, outlined_text, surroundings

    page = src[index]
    ox, oy, W, H = _page_frame(page)
    bleed = float(profile["page"].get("bleedPt") or 0)
    body = float((profile.get("typography") or {}).get("body", {}).get("size") or 10)
    images, xrefs = image_elements(src, page, ox, oy, W, H, bleed, save, cache, z0=1)
    photos = [(e["x"], e["y"], e["x"] + e["w"], e["y"] + e["h"]) for e in images]
    found = outlined_text(page, ox, oy, W, H, faces, family_for(profile, faces), llm)
    z = 1 + len(images)
    if found and (found.pieces or found.logos):
        logos = [r for r in found.logos if not any(_overlaps(r, p) > 0.6 for p in photos)]
        frames = [_logo_element(page, r, ox, oy, save, z + i, i + 1) for i, r in enumerate(logos)]
        z += len(frames)
        frames += [text_element(f, faces, body, z + i, i + 1) for i, f in enumerate(stack_frames(found.pieces))]
        bg = render_background(work, index, ox, oy, W, H, bleed, xrefs, [], glyph_rects=found.redact)
        draw = ImageDraw.Draw(bg)
        for box, rgb in found.cover:
            for px in _to_px([box], bleed, bg):
                draw.rectangle(px, fill=rgb)
    else:
        blocks = vision_blocks()
        if not blocks:
            return None
        pairs = vision_elements(page, blocks, ox, oy, W, H, faces, profile, images, save, z0=z)
        bg = render_background(work, index, ox, oy, W, H, bleed, xrefs, [])
        draw = ImageDraw.Draw(bg)
        frames = []
        for el, box in pairs:
            px = _to_px([box], bleed, bg)
            paper, plain = surroundings(bg, px[0]) if px else ((255, 255, 255), False)
            if plain:  # only on plain paper can the picture's own type be covered without a trace
                draw.rectangle(px[0], fill=paper)
                frames.append(el)
    elements = [] if is_blank(bg, profile["colors"].get("paper") or "#FFFFFF") else \
        [background_element(bg, save, W, H, bleed, index + 1)]
    return elements + images + frames
