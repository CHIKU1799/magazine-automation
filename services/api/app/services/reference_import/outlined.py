"""Pages whose type is outlined (vector glyphs, no live text): find the lines from the glyph shapes, have a vision
model only read them, and rebuild them as live text frames.

Where the text is comes from the PDF's own geometry, which is exact; the model never places anything. Every line
candidate is cut out of the rendered page and the crops are stacked, numbered, into one picture, so the model's job
is reading short strips: words, logo or decoration. Glyphs that are plain fills are then removed from the
background exactly (redaction of the paths); glyphs drawn through clipping paths (gradient type) are painted out by
the image model inside the same exact boxes, because removing a clip would spread its gradient.
"""

from __future__ import annotations

import io
import logging
from collections import Counter
from dataclasses import dataclass, field
from statistics import median

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFont, ImageStat

from app.services.reference_analysis.fonts import clean_font_name
from app.services.reference_import.webfonts import FaceInfo

log = logging.getLogger("pressflow.reference_import.outlined")

CAP_RATIO = 0.73  # ascender/cap height of Latin type as a share of the type size
STRIP_H = 56
STRIP_MAX_W = 1400
MAX_STRIPS = 60


@dataclass
class Shape:
    x0: float
    y0: float
    x1: float
    y1: float
    kind: str  # f | fs | clip
    fill: tuple | None = None

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def w(self) -> float:
        return self.x1 - self.x0


@dataclass
class InkLine:
    shapes: list[Shape] = field(default_factory=list)

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return (min(s.x0 for s in self.shapes), min(s.y0 for s in self.shapes), max(s.x1 for s in self.shapes),
                max(s.y1 for s in self.shapes))

    @property
    def baseline(self) -> float:
        h = self.rect[3] - self.rect[1]
        return median([s.y1 for s in self.shapes if s.h >= 0.45 * h] or [s.y1 for s in self.shapes])

    @property
    def vector_only(self) -> bool:
        return all(s.kind in ("f", "fs") for s in self.shapes)


def ink_shapes(page: pymupdf.Page, ox: float, oy: float, W: float, H: float) -> list[Shape]:
    """Glyph-sized filled shapes and clipping paths (outlined type is made of them), in trim coordinates."""
    try:
        drawings = page.get_drawings(extended=True)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for d in drawings[:30000]:
        kind = d.get("type")
        if kind not in ("f", "fs", "clip"):
            continue
        r = d.get("scissor") if kind == "clip" else d.get("rect")
        if r is None:
            continue
        s = Shape(r.x0 - ox, r.y0 - oy, r.x1 - ox, r.y1 - oy, kind, tuple(d["fill"]) if d.get("fill") else None)
        if 0.4 < s.h <= (0.08 if kind == "clip" else 0.12) * H and 0.2 < s.w <= (0.95 if kind == "clip" else 0.3) * W:
            out.append(s)
    return out


def ink_lines(shapes: list[Shape]) -> list[InkLine]:
    """Shapes side by side on one band, close enough to be letters of the same line, joined (union-find)."""
    n = len(shapes)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = sorted(range(n), key=lambda i: shapes[i].x0)
    for ai, i in enumerate(order):
        a = shapes[i]
        for j in order[ai + 1:]:
            b = shapes[j]
            if b.x0 - a.x1 > 0.9 * max(a.h, b.h):
                break
            overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
            if overlap >= 0.5 * min(a.h, b.h) and max(a.h, b.h) <= 1.8 * min(a.h, b.h) + 2:
                parent[find(i)] = find(j)
    groups: dict[int, list[Shape]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(shapes[i])
    lines = [InkLine(g) for g in groups.values() if len(g) >= 2 or g[0].w > 2 * g[0].h]
    # A shape inside another line's band (an i-dot, a counter) belongs to that line rather than standing alone.
    lines.sort(key=lambda ln: (ln.rect[2] - ln.rect[0]) * (ln.rect[3] - ln.rect[1]), reverse=True)
    kept: list[InkLine] = []
    for ln in lines:
        r = ln.rect
        host = next((k for k in kept if k.rect[0] <= r[0] and k.rect[2] >= r[2] and k.rect[1] - 2 <= r[1]
                     and k.rect[3] + 2 >= r[3] and k.rect[3] - k.rect[1] <= 2.5 * median(s.h for s in k.shapes)), None)
        if host:
            host.shapes += ln.shapes
        else:
            kept.append(ln)
    return sorted(kept, key=lambda ln: (round(ln.rect[1]), ln.rect[0]))


# ------------------------------------------------------------------ reading


READ_PROMPT = """Each numbered strip below was cut out of one magazine page. For every number, decide what the strip
shows: "text" (words, names, numbers), "logo" (a brand mark or wordmark), or "other" (decoration, lines, dots,
pictures). For text, transcribe it exactly as printed, with the same capitalisation and punctuation, and give its
weight. Answer for every number."""


def _strips(render: Image.Image, scale: float, lines: list[InkLine], ox: float, oy: float) -> list[Image.Image]:
    """Numbered crops of each line, stacked into pictures of at most ~25 strips."""
    sheets, rows = [], []
    font = ImageFont.load_default()
    for i, ln in enumerate(lines):
        x0, y0, x1, y1 = ln.rect
        pad = 0.25 * (y1 - y0) + 1
        bx0 = min(render.width - 1, max(0, round((x0 + ox - pad) * scale)))
        by0 = min(render.height - 1, max(0, round((y0 + oy - pad) * scale)))
        # A line partly off the page still gets a strip (possibly blank), so the numbering stays aligned.
        box = (bx0, by0, max(bx0 + 1, min(render.width, round((x1 + ox + pad) * scale))),
               max(by0 + 1, min(render.height, round((y1 + oy + pad) * scale))))
        crop = render.crop(box)
        f = STRIP_H / max(1, crop.height)
        crop = crop.resize((max(1, min(STRIP_MAX_W, round(crop.width * f))), STRIP_H), Image.Resampling.LANCZOS)
        rows.append((i + 1, crop))
    for start in range(0, len(rows), 25):
        chunk = rows[start:start + 25]
        width = 90 + max(c.width for _n, c in chunk)
        sheet = Image.new("RGB", (width, len(chunk) * (STRIP_H + 14) + 10), "white")
        d = ImageDraw.Draw(sheet)
        for k, (num, crop) in enumerate(chunk):
            y = 10 + k * (STRIP_H + 14)
            d.rectangle((0, y, 80, y + STRIP_H), fill=(30, 30, 30))
            d.text((10, y + STRIP_H // 2 - 6), f"#{num}", fill="white", font=font)
            sheet.paste(crop, (90, y))
            d.line((0, y + STRIP_H + 7, width, y + STRIP_H + 7), fill=(255, 0, 0), width=1)
        sheets.append(sheet)
    return sheets


def read_lines(llm, render: Image.Image, scale: float, lines: list[InkLine], ox: float, oy: float) -> dict[int, dict]:
    """Index (1-based) -> {kind, text, weight, italic}. Raises LLMError."""
    from app.services.llm import nullable, strict_object

    schema = strict_object({"items": {"type": "array", "items": strict_object({
        "index": {"type": "integer"},
        "kind": {"type": "string", "enum": ["text", "logo", "other"]},
        "text": nullable({"type": "string"}),
        "weight": {"type": "string", "enum": ["light", "regular", "medium", "bold", "black"]},
        "italic": {"type": "boolean"},
    })}})
    pngs = []
    for sheet in _strips(render, scale, lines, ox, oy):
        buf = io.BytesIO()
        sheet.save(buf, "PNG")
        pngs.append(buf.getvalue())
    out = llm.complete_json(system="You read text from images exactly as printed. Answer only from the images.",
                            prompt=READ_PROMPT, schema=schema, images=pngs, max_tokens=6000, schema_name="strips")
    return {int(it["index"]): it for it in out.get("items") or []}


# ------------------------------------------------------------------ frames

WEIGHTS = {"light": 300, "regular": 400, "medium": 500, "bold": 700, "black": 900}


def surroundings(img: Image.Image, box: tuple[int, int, int, int]) -> tuple[tuple[int, int, int], bool]:
    """Median colour of the band just outside a pixel box, and whether that band is plain (one flat colour)."""
    x0, y0, x1, y1 = box
    pad = max(3, round(0.25 * (y1 - y0)))
    bands = [(x0 - pad, y0 - pad, x1 + pad, y0), (x0 - pad, y1, x1 + pad, y1 + pad), (x0 - pad, y0, x0, y1),
             (x1, y0, x1 + pad, y1)]
    stats = []
    for bx0, by0, bx1, by1 in bands:
        bx0, by0, bx1, by1 = max(0, bx0), max(0, by0), min(img.width, bx1), min(img.height, by1)
        if bx1 - bx0 >= 1 and by1 - by0 >= 1:
            stats.append(ImageStat.Stat(img.crop((bx0, by0, bx1, by1)).convert("RGB")))
    if not stats:
        return (255, 255, 255), False
    colour = tuple(int(median(st.median[i] for st in stats)) for i in range(3))
    plain = all(max(st.stddev) < 12 for st in stats) and \
        all(max(abs(st.median[i] - colour[i]) for i in range(3)) < 10 for st in stats)
    return colour, plain


def _clamp(box: tuple[int, int, int, int], img: Image.Image) -> tuple[int, int, int, int]:
    """A pixel box inside the picture (PIL pads crops beyond the edge with black, which would read as ink)."""
    x0, y0 = min(img.width - 1, max(0, box[0])), min(img.height - 1, max(0, box[1]))
    return x0, y0, max(x0 + 1, min(img.width, box[2])), max(y0 + 1, min(img.height, box[3]))


@dataclass
class Ink:
    colour: str
    single: bool  # one ink colour (anti-aliased edges aside)
    coverage: float  # share of the box the ink covers: heavier type covers more


def ink_of(img: Image.Image, box: tuple[int, int, int, int], paper: tuple[int, int, int]) -> Ink:
    """The colour type is printed in, whether it is a single one, and how much of the line it covers."""
    pixels = [tuple(p) for p in np.asarray(img.crop(box).convert("RGB")).reshape(-1, 3).tolist()]
    dist = [(sum(abs(a - b) for a, b in zip(p, paper)), p) for p in pixels]
    far = max((d for d, _p in dist), default=0)
    if far < 120:
        return Ink("#000000", False, 0.0)
    core = [p for d, p in dist if d >= 0.75 * far]
    chans = [sorted(p[i] for p in core) for i in range(3)]
    ink = tuple(c[len(c) // 2] for c in chans)
    # An anti-aliased pixel lies between the paper and the ink; a pixel of a second ink colour lies off that line.
    u = [i - p for i, p in zip(ink, paper)]
    uu = sum(x * x for x in u) or 1
    off = 0
    strong = [p for d, p in dist if d >= 0.4 * far]
    for p in strong:
        v = [a - b for a, b in zip(p, paper)]
        t = sum(a * b for a, b in zip(v, u)) / uu
        if sum((a - t * b) ** 2 for a, b in zip(v, u)) ** 0.5 > 60:
            off += 1
    coverage = sum(1 for d, _p in dist if d >= 0.5 * far) / max(1, len(pixels))
    return Ink("#" + "".join(f"{c:02X}" for c in ink), off <= 0.06 * len(strong), coverage)


def pick_face(faces: dict[str, FaceInfo], family: str | None, weight: int, italic: bool) -> str | None:
    """Base-font key of the face of `family` nearest to the weight (the document's own fonts)."""
    cands = [(k, f) for k, f in faces.items() if (family is None or f.family == family) and f.italic == italic]
    cands = cands or [(k, f) for k, f in faces.items() if family is None or f.family == family]
    if not cands:
        return None
    return min(cands, key=lambda kf: abs(kf[1].weight - weight))[0]


@dataclass
class OutlinedText:
    """Lines read as text, as pieces for the page builder's frame grouping, and how to clear them behind."""

    pieces: list = field(default_factory=list)
    logos: list[tuple[float, float, float, float]] = field(default_factory=list)
    redact: list[pymupdf.Rect] = field(default_factory=list)  # page coordinates: plain-fill glyph paths to remove
    cover: list[tuple[tuple, tuple]] = field(default_factory=list)  # (trim box, rgb): type on plain paper, covered


def outlined_text(page: pymupdf.Page, ox: float, oy: float, W: float, H: float, faces: dict[str, FaceInfo],
                  family: str | None, llm) -> OutlinedText | None:
    """None when the page has no outlined lines (a flattened picture) or the model can't read them.

    A line is rebuilt only when it can be cleared from the background without a trace: its glyphs are plain filled
    paths (removed exactly), or it is one colour on plain paper (covered with that paper). Type in a gradient or
    over a picture stays in the background as artwork."""
    from app.services.llm import LLMError
    from app.services.reference_import.pages import Piece, merge_words

    lines = [ln for ln in ink_lines(ink_shapes(page, ox, oy, W, H))
             if ln.rect[3] - ln.rect[1] <= 0.08 * H and ln.rect[3] - ln.rect[1] <= 2.5 * median(s.h for s in ln.shapes)]
    if not lines:
        return None
    lines = lines[:MAX_STRIPS * 2]
    scale = 3.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    render = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    try:
        read = read_lines(llm, render, scale, lines, ox, oy)
    except LLMError as e:
        log.warning("reading outlined lines failed: %s", e)
        return None
    out = OutlinedText()
    raw = []
    for i, ln in enumerate(lines):
        item = read.get(i + 1) or {}
        kind, text = item.get("kind"), (item.get("text") or "").strip()
        if kind not in ("text", "logo") or (kind == "text" and not text):
            continue
        x0, y0, x1, y1 = ln.rect
        pad = max(1.0, 0.08 * (y1 - y0))
        px = _clamp((round((x0 + ox - pad) * scale), round((y0 + oy - pad) * scale), round((x1 + ox + pad) * scale),
                     round((y1 + oy + pad) * scale)), render)
        paper, plain = surroundings(render, px)
        ink = ink_of(render, _clamp((round((x0 + ox) * scale), round((y0 + oy) * scale), round((x1 + ox) * scale),
                                     round((y1 + oy) * scale)), render), paper)
        colour = ink.colour
        if kind == "text" and not ink.single:
            continue  # type in more than one colour (or a gradient) can't be one frame: it stays as artwork
        if ln.vector_only:
            out.redact.append(pymupdf.Rect(x0 + ox - pad, y0 + oy - pad, x1 + ox + pad, y1 + oy + pad))
        elif plain and (ink.single or kind == "logo"):
            out.cover.append(((x0 - pad, y0 - pad, x1 + pad, y1 + pad), paper))
        else:
            continue
        if kind == "logo":
            out.logos.append(ln.rect)
            continue
        weight = WEIGHTS.get(item.get("weight") or "regular", 400)
        # The model's sense of weight is loose; the ink is the check (regular covers up to ~0.28 of a line's box).
        if weight >= 900 and ink.coverage < 0.45:
            weight = 700
        if weight >= 600 and ink.coverage < 0.27:
            weight = 400
        raw.append((ln, text, weight, bool(item.get("italic")), max(3.0, (ln.baseline - y0) / CAP_RATIO), colour))
    # Sizes measured per line jitter by a few per cent: lines of one size are snapped to their common size so
    # they group into one frame.
    snapped: dict[float, float] = {}
    for sz in sorted({round(r[4], 1) for r in raw}):
        anchor = next((a for a in snapped.values() if abs(a - sz) <= 0.07 * a), None)
        snapped[sz] = anchor if anchor is not None else sz
    pieces = []
    for ln, text, weight, italic, size, colour in raw:
        size = snapped[round(size, 1)]
        key = pick_face(faces, family, weight, italic) or f"{family or 'Sans'}-{weight}"
        x0, y0, x1, y1 = ln.rect
        pieces.append(Piece(text=text, x0=x0, y0=y0, x1=x1, y1=y1, baseline=ln.baseline, size=size, color=colour,
                            font=key))
    for p in merge_words(sorted(pieces, key=lambda q: (round(q.baseline / max(1.0, 0.3 * q.size)), q.x0))):
        face = faces.get(p.font)
        natural = face.text_width(p.text, p.size) if face else None
        if natural and len(p.text) > 1:
            # The substitute face is set to the outlined line's exact width.
            p.tracking = round(max(-80.0, min(250.0, ((p.x1 - p.x0) - natural) / (len(p.text) - 1) / p.size * 1000)), 1)
        out.pieces.append(p)
    return out


def family_for(profile: dict, faces: dict[str, FaceInfo]) -> str | None:
    """Outlined type is usually the display face: the reference's headline font when it was measured."""
    detected = ((profile.get("typography") or {}).get("headline") or {}).get("detectedFont") or ""
    face = faces.get(clean_font_name(detected))
    if face:
        return face.family
    counts = Counter(f.family for f in faces.values())
    return counts.most_common(1)[0][0] if counts else None
