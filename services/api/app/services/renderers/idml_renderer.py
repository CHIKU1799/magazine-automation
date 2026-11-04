"""IDML package from a layout contract: open it in Adobe InDesign and save it as .indd.

IDML is InDesign's open, documented interchange format (a zip of XML). Native .indd can only be written by InDesign
itself, so this is how PressFlow hands InDesign a fully editable document without an InDesign server: every frame
becomes a native text frame, picture frame, rectangle or line, at its exact position and rotation, with its font,
size, leading, tracking, colour and alignment set as local formatting. Pictures are linked from a `Links` folder
next to the .idml; the download is one zip holding both, plus a list of the fonts to install.

Text keeps its frame's line breaks as forced line breaks, so lines copied from a reference break where they did.
The first baseline sits where the editor puts it (half the leading plus 0.35 em below the frame's top).
"""

from __future__ import annotations

import io
import math
import zipfile
from dataclasses import dataclass, field
from typing import Callable
from xml.sax.saxutils import escape, quoteattr

from PIL import Image

MIMETYPE = "application/vnd.adobe.indesign-idml-package"
DOM = "8.0"  # CS6 DOM: opens in every later InDesign
PKG = 'xmlns:idPkg="http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging"'
HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
JUSTIFY = {"left": "LeftAlign", "center": "CenterAlign", "right": "RightAlign", "justify": "LeftJustified"}
LINK_FORMAT = {".jpg": "$ID/JPEG", ".jpeg": "$ID/JPEG", ".png": "$ID/Portable Network Graphics (PNG)",
               ".tif": "$ID/TIFF", ".tiff": "$ID/TIFF"}
BASELINE_K = 0.35


@dataclass
class IdmlResult:
    data: bytes
    page_count: int
    warnings: list[str] = field(default_factory=list)


def _n(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def _rgb(hexv: str) -> str:
    h = hexv.lstrip("#")
    return " ".join(_n(int(h[i:i + 2], 16)) for i in (0, 2, 4))


def _matrix(cx: float, cy: float, degrees: float) -> str:
    """Frame transform: rotate (clockwise, y down) about the frame's centre, which sits at (cx, cy) on the spread."""
    r = math.radians(degrees or 0)
    c, s = math.cos(r), math.sin(r)
    return " ".join(_n(round(v, 6)) for v in (c, s, -s, c, cx, cy))


def _path(w: float, h: float) -> str:
    pts = [(-w / 2, -h / 2), (-w / 2, h / 2), (w / 2, h / 2), (w / 2, -h / 2)]
    anchors = "".join(f'<PathPointType Anchor="{_n(x)} {_n(y)}" LeftDirection="{_n(x)} {_n(y)}" '
                      f'RightDirection="{_n(x)} {_n(y)}"/>' for x, y in pts)
    return (f'<Properties><PathGeometry><GeometryPathType PathOpen="false"><PathPointArray>{anchors}'
            f'</PathPointArray></GeometryPathType></PathGeometry></Properties>')


def _opacity(fr: dict) -> str:
    if fr.get("opacity") is None or fr["opacity"] >= 1:
        return ""
    return f'<TransparencySetting><BlendingSetting Opacity="{_n(round(fr["opacity"] * 100, 2))}"/></TransparencySetting>'


def _story(sid: str, text: str, ps: dict, colour_ref: str) -> str:
    """One story: paragraphs (blank-line separated) of lines (forced line breaks), all in the frame's formatting."""
    font = ps.get("font_family") or "Minion Pro"
    attrs = (f'AppliedCharacterStyle="CharacterStyle/$ID/[No character style]" FontStyle={quoteattr(ps.get("font_style") or "Regular")} '
             f'PointSize="{_n(ps["size_pt"])}" FillColor="{colour_ref}" Tracking="{_n(round(ps.get("tracking") or 0, 1))}"'
             + (' Capitalization="AllCaps"' if ps.get("uppercase") else ""))
    props = (f'<Properties><AppliedFont type="string">{escape(font)}</AppliedFont>'
             f'<Leading type="unit">{_n(ps["leading_pt"])}</Leading></Properties>')
    paras = [p for p in text.split("\n\n")] or [""]
    body = []
    for i, para in enumerate(paras):
        content = escape(para).replace("\n", " ")  # forced line break: the line ends where it did
        body.append(f'<ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/$ID/NormalParagraphStyle" '
                    f'Justification="{JUSTIFY.get(ps.get("align") or "left", "LeftAlign")}" '
                    f'FirstLineIndent="{_n(ps.get("first_line_indent_pt") or 0) if i else 0}" '
                    f'SpaceBefore="{_n(ps.get("space_before_pt") or 0) if i else 0}" Hyphenation="false">'
                    f'<CharacterStyleRange {attrs}>{props}<Content>{content}</Content>'
                    + ("<Br/>" if i < len(paras) - 1 else "") + "</CharacterStyleRange></ParagraphStyleRange>")
    return (f'{HEAD}<idPkg:Story {PKG} DOMVersion="{DOM}"><Story Self="{sid}" AppliedTOCStyle="n" TrackChanges="false" '
            f'StoryTitle="$ID/" AppliedNamedGrid="n"><StoryPreference OpticalMarginAlignment="false" '
            f'OpticalMarginSize="12" FrameType="TextFrameType" StoryOrientation="Horizontal" '
            f'StoryDirection="LeftToRightDirection"/>{"".join(body)}</Story></idPkg:Story>')


def _image_size_pt(data: bytes) -> tuple[float, float, int, int]:
    """Native size of a picture in points (from its own resolution, 72 ppi when it has none) and in pixels."""
    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        dpi = im.info.get("dpi") or (72, 72)
    dx, dy = (float(dpi[0]) or 72.0), (float(dpi[1]) or 72.0)
    return w * 72 / dx, h * 72 / dy, w, h


def render_idml(contract: dict, resolve_asset: Callable[[str], bytes | None], rotations: dict[str, float] | None = None
                ) -> IdmlResult:
    """`rotations`: frame id -> degrees (the contract has no rotation; the editor's frames do)."""
    rotations = rotations or {}
    doc = contract["document"]
    W, H = float(doc["page_width_pt"]), float(doc["page_height_pt"])
    bleed = float(doc.get("bleed_pt") or 0)
    styles = {p["name"]: p for p in contract["styles"]["paragraph"]}
    swatches = {c["name"]: c["hex"] for c in contract["styles"]["colors"]}
    assets = {a["id"]: a for a in contract.get("assets") or []}
    warnings: list[str] = []
    files: dict[str, bytes] = {}
    stories: list[str] = []
    spreads: list[str] = []
    links: dict[str, str] = {}
    fonts: set[tuple[str, str]] = set()
    uid = iter(range(1, 10_000_000))

    def colour(name: str | None) -> str:
        return f"Color/{name}" if name and name in swatches else "Color/Black"

    for page in contract["pages"]:
        n = page["page_number"]
        sp, pg = f"sp{n}", f"pg{n}"
        items = []
        for fr in sorted(page["frames"], key=lambda f: f.get("z", 0)):
            top, left, bottom, right = (float(v) for v in fr["bounds_pt"])
            w, h = right - left, bottom - top
            if w <= 0 or h <= 0:
                continue
            # Spread coordinates: the page's top-left corner is at (-W/2, -H/2).
            cx, cy = left + w / 2 - W / 2, top + h / 2 - H / 2
            m = _matrix(cx, cy, rotations.get(fr["id"], 0))
            label = quoteattr(fr.get("label") or fr.get("role") or "")
            self_id = f"u{next(uid)}"
            kind = fr["kind"]
            if kind == "text":
                ps = styles.get(fr.get("paragraph_style") or "") or next(iter(styles.values()))
                sid = f"st{next(uid)}"
                stories.append(sid)
                files[f"Stories/Story_{sid}.xml"] = _story(sid, fr.get("content") or "", ps, colour(ps.get("color")))
                fonts.add((ps.get("font_family") or "Minion Pro", ps.get("font_style") or "Regular"))
                first = ps["leading_pt"] / 2 + BASELINE_K * ps["size_pt"]
                cols = int(fr.get("columns") or 1)
                items.append(
                    f'<TextFrame Self="{self_id}" ParentStory="{sid}" PreviousTextFrame="n" NextTextFrame="n" '
                    f'ContentType="TextType" ItemLayer="ly1" Name={label} ItemTransform="{m}">{_path(w, h)}{_opacity(fr)}'
                    f'<TextFramePreference TextColumnCount="{cols}" TextColumnGutter="{_n(fr.get("gutter_pt") or 12)}" '
                    f'FirstBaselineOffset="FixedHeight" MinimumFirstBaselineOffset="{_n(round(first, 3))}" '
                    f'VerticalJustification="TopAlign" AutoSizingType="Off"/></TextFrame>')
            elif kind == "image":
                img = fr.get("image") or {}
                aid = img.get("asset_id")
                data = resolve_asset(aid) if aid else None
                inner = ""
                if data:
                    try:
                        pw, ph, px, py = _image_size_pt(data)
                    except OSError:
                        warnings.append(f"Page {n}: picture {aid} could not be read")
                        data = None
                if data:
                    ext = (assets.get(aid, {}).get("file") or ".jpg")
                    ext = ext[ext.rfind("."):].lower() if "." in ext else ".jpg"
                    name = links.setdefault(aid, f"{aid}{ext}")
                    files[f"Links/{name}"] = data
                    # Fill the frame proportionally, then zoom and pan as the editor's crop does.
                    s0 = max(w / pw, h / ph)
                    zs = float(img.get("scale") or 1)
                    fx, fy = 0.5 + float(img.get("offset_x") or 0) / 2, 0.5 + float(img.get("offset_y") or 0) / 2
                    ox, oy = w * fx, h * fy
                    dx, dy = (w - pw * s0) * fx, (h - ph * s0) * fy
                    s = s0 * zs
                    tx, ty = ox + (dx - ox) * zs - w / 2, oy + (dy - oy) * zs - h / 2
                    inner = (f'<Image Self="u{next(uid)}" ItemTransform="{_n(round(s, 6))} 0 0 {_n(round(s, 6))} '
                             f'{_n(round(tx, 4))} {_n(round(ty, 4))}"><Properties><GraphicBounds Left="0" Top="0" '
                             f'Right="{_n(round(pw, 4))}" Bottom="{_n(round(ph, 4))}"/></Properties>'
                             f'<Link Self="u{next(uid)}" LinkResourceURI={quoteattr("file:Links/" + name)} '
                             f'LinkResourceFormat="{LINK_FORMAT.get(ext, "$ID/JPEG")}" StoredState="Normal" '
                             f'LinkResourceSize="{len(data)}"/></Image>')
                elif aid:
                    warnings.append(f"Page {n}: picture {aid} has no file; its frame is left empty")
                items.append(f'<Rectangle Self="{self_id}" ContentType="GraphicType" ItemLayer="ly1" Name={label} '
                             f'StrokeWeight="0" FillColor="Swatch/None" StrokeColor="Swatch/None" ItemTransform="{m}">'
                             f'{_path(w, h)}{_opacity(fr)}{inner}</Rectangle>')
            elif kind == "line":
                weight = float(fr.get("stroke_weight_pt") or 0.5)
                horizontal = w >= h
                a, b = ((-w / 2, 0), (w / 2, 0)) if horizontal else ((0, -h / 2), (0, h / 2))
                pts = "".join(f'<PathPointType Anchor="{_n(x)} {_n(y)}" LeftDirection="{_n(x)} {_n(y)}" '
                              f'RightDirection="{_n(x)} {_n(y)}"/>' for x, y in (a, b))
                items.append(f'<GraphicLine Self="{self_id}" ItemLayer="ly1" Name={label} StrokeWeight="{_n(weight)}" '
                             f'StrokeColor="{colour(fr.get("stroke"))}" FillColor="Swatch/None" ItemTransform="{m}">'
                             f'<Properties><PathGeometry><GeometryPathType PathOpen="true"><PathPointArray>{pts}'
                             f'</PathPointArray></GeometryPathType></PathGeometry></Properties>{_opacity(fr)}</GraphicLine>')
            else:
                fill = colour(fr["fill"]) if fr.get("fill") else "Swatch/None"
                stroke = colour(fr["stroke"]) if fr.get("stroke") else "Swatch/None"
                items.append(f'<Rectangle Self="{self_id}" ContentType="Unassigned" ItemLayer="ly1" Name={label} '
                             f'FillColor="{fill}" StrokeColor="{stroke}" StrokeWeight="{_n(fr.get("stroke_weight_pt") or 0)}" '
                             f'ItemTransform="{m}">{_path(w, h)}{_opacity(fr)}</Rectangle>')
        spreads.append(sp)
        files[f"Spreads/Spread_{sp}.xml"] = (
            f'{HEAD}<idPkg:Spread {PKG} DOMVersion="{DOM}"><Spread Self="{sp}" PageCount="1" BindingLocation="0" '
            f'ShowMasterItems="false" ItemTransform="1 0 0 1 0 0" FlattenerOverride="Default" AllowPageShuffle="true">'
            f'<Page Self="{pg}" Name="{n}" AppliedMaster="n" OverrideList="" TabOrder="" '
            f'GeometricBounds="0 0 {_n(H)} {_n(W)}" ItemTransform="1 0 0 1 {_n(-W / 2)} {_n(-H / 2)}" '
            f'MasterPageTransform="1 0 0 1 0 0"/>{"".join(items)}</Spread></idPkg:Spread>')

    colours = "".join(f'<Color Self="Color/{escape(name)}" Model="Process" Space="RGB" ColorValue="{_rgb(hexv)}" '
                      f'ColorOverride="Normal" AlternateSpace="NoAlternateColor" AlternateColorValue="" '
                      f'Name={quoteattr(name)} ColorEditable="true" ColorRemovable="true" Visible="true" '
                      f'SwatchCreatorID="7937"/>' for name, hexv in swatches.items())
    files["Resources/Graphic.xml"] = (
        f'{HEAD}<idPkg:Graphic {PKG} DOMVersion="{DOM}">'
        '<Color Self="Color/Black" Model="Process" Space="CMYK" ColorValue="0 0 0 100" ColorOverride="Specialblack" '
        'AlternateSpace="NoAlternateColor" AlternateColorValue="" Name="Black" ColorEditable="false" '
        'ColorRemovable="false" Visible="true" SwatchCreatorID="7937"/>'
        '<Color Self="Color/Paper" Model="Process" Space="CMYK" ColorValue="0 0 0 0" ColorOverride="Specialpaper" '
        'AlternateSpace="NoAlternateColor" AlternateColorValue="" Name="Paper" ColorEditable="true" '
        'ColorRemovable="false" Visible="true" SwatchCreatorID="7937"/>'
        '<Color Self="Color/Registration" Model="Registration" Space="CMYK" ColorValue="100 100 100 100" '
        'ColorOverride="Specialregistration" AlternateSpace="NoAlternateColor" AlternateColorValue="" '
        'Name="Registration" ColorEditable="false" ColorRemovable="false" Visible="true" SwatchCreatorID="7937"/>'
        f'{colours}<Swatch Self="Swatch/None" Name="None" ColorEditable="false" ColorRemovable="false" '
        'Visible="true" SwatchCreatorID="7937"/>'
        '<StrokeStyle Self="StrokeStyle/$ID/Solid" Name="$ID/Solid"/></idPkg:Graphic>')
    files["Resources/Fonts.xml"] = f'{HEAD}<idPkg:Fonts {PKG} DOMVersion="{DOM}"/>'
    files["Resources/Styles.xml"] = (
        f'{HEAD}<idPkg:Styles {PKG} DOMVersion="{DOM}">'
        '<RootCharacterStyleGroup Self="u_rcs"><CharacterStyle Self="CharacterStyle/$ID/[No character style]" '
        'Imported="false" Name="$ID/[No character style]"/></RootCharacterStyleGroup>'
        '<RootParagraphStyleGroup Self="u_rps"><ParagraphStyle Self="ParagraphStyle/$ID/[No paragraph style]" '
        'Name="$ID/[No paragraph style]"/><ParagraphStyle Self="ParagraphStyle/$ID/NormalParagraphStyle" '
        'Name="$ID/NormalParagraphStyle" NextStyle="ParagraphStyle/$ID/NormalParagraphStyle"/>'
        '</RootParagraphStyleGroup>'
        '<RootObjectStyleGroup Self="u_ros"><ObjectStyle Self="ObjectStyle/$ID/[None]" Name="$ID/[None]"/>'
        '<ObjectStyle Self="ObjectStyle/$ID/[Normal Graphics Frame]" Name="$ID/[Normal Graphics Frame]"/>'
        '<ObjectStyle Self="ObjectStyle/$ID/[Normal Text Frame]" Name="$ID/[Normal Text Frame]"/>'
        '</RootObjectStyleGroup></idPkg:Styles>')
    pages = len(contract["pages"])
    files["Resources/Preferences.xml"] = (
        f'{HEAD}<idPkg:Preferences {PKG} DOMVersion="{DOM}">'
        f'<DocumentPreference PageHeight="{_n(H)}" PageWidth="{_n(W)}" PagesPerDocument="{pages}" FacingPages="false" '
        f'DocumentBleedTopOffset="{_n(bleed)}" DocumentBleedBottomOffset="{_n(bleed)}" '
        f'DocumentBleedInsideOrLeftOffset="{_n(bleed)}" DocumentBleedOutsideOrRightOffset="{_n(bleed)}" '
        f'DocumentBleedUniformSize="true" PageBinding="LeftToRight" AllowPageShuffle="true" '
        f'StartPageNumber="1" Intent="PrintIntent"/>'
        '<ViewPreference HorizontalMeasurementUnits="Points" VerticalMeasurementUnits="Points"/>'
        '</idPkg:Preferences>')
    title = escape(doc.get("title") or "PressFlow")
    designmap = (
        f'{HEAD}<?aid style="50" type="document" readerVersion="6.0" featureSet="257" product="8.0(370)" ?>'
        f'<Document {PKG} DOMVersion="{DOM}" Self="d" StoryList="{" ".join(stories)}" ZeroPoint="0 0" '
        f'ActiveLayer="ly1" CMYKProfile="Coated FOGRA39 (ISO 12647-2:2004)" RGBProfile="sRGB IEC61966-2.1" '
        'SolidColorIntent="UseColorSettings" AfterBlendingIntent="UseColorSettings" DefaultImageIntent="UseColorSettings" '
        'RGBPolicy="PreserveEmbeddedProfiles" CMYKPolicy="CombinationOfPreserveAndSafeCmyk" AccurateLABSpots="false">'
        f'<Properties><Label><KeyValuePair Key="PressFlowTitle" Value="{title}"/></Label></Properties>'
        '<idPkg:Graphic src="Resources/Graphic.xml"/><idPkg:Fonts src="Resources/Fonts.xml"/>'
        '<idPkg:Styles src="Resources/Styles.xml"/><idPkg:Preferences src="Resources/Preferences.xml"/>'
        '<Layer Self="ly1" Name="Layer 1" Visible="true" Locked="false" IgnoreWrap="false" ShowGuides="true" '
        'LockGuides="false" UI="true" Expendable="true" Printable="true"/>'
        + "".join(f'<idPkg:Spread src="Spreads/Spread_{sp}.xml"/>' for sp in spreads)
        + f'<Section Self="sec1" Length="{pages}" Name="" ContinueNumbering="false" IncludeSectionPrefix="false" '
          f'PageNumberStyle="Arabic" PageStart="pg{contract["pages"][0]["page_number"]}" PageNumberStart="1" '
          f'SectionPrefix="" Marker=""/>'
        + "".join(f'<idPkg:Story src="Stories/Story_{sid}.xml"/>' for sid in stories)
        + "</Document>")

    idml = io.BytesIO()
    with zipfile.ZipFile(idml, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), MIMETYPE, compress_type=zipfile.ZIP_STORED)  # first and uncompressed
        z.writestr("META-INF/container.xml",
                   f'{HEAD}<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="designmap.xml" media-type="text/xml"/></rootfiles></container>',
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("designmap.xml", designmap, compress_type=zipfile.ZIP_DEFLATED)
        for path, content in files.items():
            if not path.startswith("Links/"):
                z.writestr(path, content, compress_type=zipfile.ZIP_DEFLATED)

    slug = "".join(ch if ch.isalnum() else "-" for ch in (doc.get("title") or "magazine").lower()).strip("-")[:60]
    slug = slug or "magazine"
    fonts_list = "\n".join(f"  {fam} — {style}" for fam, style in sorted(fonts))
    readme = (f"{doc.get('title') or 'Magazine'} — InDesign package from PressFlow\n\n"
              f"1. Keep {slug}.idml and the Links folder together.\n"
              f"2. Open {slug}.idml in Adobe InDesign (CS6 or later), then File > Save As > InDesign document (.indd).\n"
              "3. If InDesign lists missing fonts, install these and the text reflows into them:\n"
              f"{fonts_list or '  (none)'}\n\n"
              "Pictures are linked from Links/. Text carries its frame's formatting as local overrides; lines keep "
              "the breaks they had in PressFlow.\n")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{slug}/{slug}.idml", idml.getvalue())
        for path, content in files.items():
            if path.startswith("Links/"):
                z.writestr(f"{slug}/{path}", content, compress_type=zipfile.ZIP_STORED)
        z.writestr(f"{slug}/README.txt", readme)
    return IdmlResult(out.getvalue(), pages, warnings)
