"""Design with reference (AI): a project's articles, written from its videos, set into pages copied from its
reference magazine, as editable as an editable copy.

For each article the reference pages of the matching kind are compared by how much text they hold, and an opener plus
continuation pages are chosen for its length. The article body is rewritten to that length (facts and quotes kept)
and flowed into the reference's own text columns, measured line by line in the reference's own fonts. A language
model rewrites every other text frame (headline, deck, captions, names, page number) to the same length and case,
keeping recurring furniture. Photo frames take the project's photographs; frames left over are emptied rather than
showing the reference's pictures. The reference cover and contents page open the issue.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from statistics import median

import pymupdf
from sqlalchemy import delete, select

from app.config import get_settings
from app.db import session_factory
from app.ids import new_id
from app.models import Article, MagazinePage, Project, ReferenceMagazine, ReferencePage, Speaker, TranscriptSegment
from app.services import images
from app.services import serializers as ser
from app.services.jobs.queue import JobContext, PermanentError, register
from app.services.llm import LLMError, get_llm, get_vision_llm, strict_object
from app.services.page_builder import article_paragraphs
from app.services.project_context import assets_by_id, dump, profile_for_project
from app.services.reference_import import _store_fonts, _vision_blocks
from app.services.reference_import.pages import (
    ImageCache,
    _frames,
    _page_frame,
    _pieces,
    build_flat_page,
    build_live_page,
    has_live_text,
    text_element,
)
from app.services.reference_import.webfonts import FaceInfo, GoogleFonts, collect_faces
from app.services.storage import get_storage
from app.services.templates import ref_template_id

log = logging.getLogger("pressflow.reference_fill")

# Reference page kinds tried for each article type, best first.
LAYOUTS = {
    "interview": ["interview", "two_column_article", "event_report"],
    "editorial": ["editorial", "two_column_article"],
    "government_perspective": ["editorial", "two_column_article", "event_report"],
    "speaker_profiles": ["speaker_profile", "two_column_article"],
    "photo_captions": ["photo_gallery", "event_report"],
}
DEFAULT_LAYOUTS = ["event_report", "two_column_article"]
CONTINUATION = ["two_column_article", "event_report"]
SPONSORED = re.compile(r"\b(advertorial|sponsored|promotion(al)? feature|brand ?story|partner content)\b", re.I)
COVER_TAG = "ai-cover"  # the issue's cover art: kept off the article pages
PHOTO_MIN_AREA = 0.03  # of the page: smaller pictures are logos and icons
COLUMN_SLACK = 30  # points: paragraphs this far apart at the left are one column (a list's indent)
FIT_PASSES = 3  # rewrites of a body at most, each measured against its columns
TIMINGS = re.compile(r"\b\d+(?:\s*[-–]\s*\d+)?\s*(?:seconds?|secs?)\b|\b\d{1,2}:\d{2}\b(?!\s*[ap]\.?m)", re.I)
AI_IMAGE_LIMIT = 8  # AI images per build at most (each one is a paid call)
# Template filler a model sometimes writes into a frame it has nothing for: such a frame is cleared instead.
PLACEHOLDER = re.compile(r"(?i)\W*(section |sub)?(title|heading|headline|subhead(ing)?|caption|text|name)( here)?\W*"
                         r"|lorem ipsum.*|\W*(tbd|tba|placeholder|n/?a)\W*")
WORDS_PER_CHAR = 1 / 6.0


# ------------------------------------------------------------------ measuring


def face_for(style: dict, faces: dict[str, FaceInfo]) -> FaceInfo | None:
    fam, w, it = style.get("fontFamily"), int(style.get("weight") or 400), bool(style.get("italic"))
    cands = [f for f in faces.values() if f.family == fam and f.metrics]
    if not cands:
        return None
    return min(cands, key=lambda f: (f.italic != it, abs(f.weight - w)))


def _width(face: FaceInfo | None, text: str, size: float, tracking: float) -> float:
    w = face.text_width(text, size, tracking) if face else None
    return w if w is not None else len(text) * size * 0.5


def wrap(face: FaceInfo | None, text: str, size: float, width: float, tracking: float = 0.0) -> list[str]:
    """Greedy line breaking as a browser does it (no kerning, so slightly wide: the text fits where measured)."""
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = f"{cur} {word}" if cur else word
        if cur and _width(face, trial, size, tracking) > width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


@dataclass
class Column:
    """A run of body paragraphs in one column, merged into one frame the article flows into."""

    element: dict
    lines: int
    gap_lines: float
    face: FaceInfo | None

    @property
    def style(self) -> dict:
        return self.element["style"]

    def chars_per_line(self) -> float:
        sample = "the quick brown fox jumps over a lazy dog and then some more words"
        return self.element["w"] / (_width(self.face, sample, self.style["size"], self.style.get("tracking") or 0)
                                    / len(sample))

    def word_capacity(self) -> int:
        # Words run a little longer in practice than the average, and paragraph ends leave part lines: aim under.
        return int(self.lines * self.chars_per_line() * WORDS_PER_CHAR * 0.88)


@dataclass
class Slots:
    columns: list[Column] = field(default_factory=list)
    texts: list[dict] = field(default_factory=list)  # every other text frame, rewritten by the model
    furniture: list[dict] = field(default_factory=list)  # in the top and bottom margins: kept (page number updated)
    artwork: list[dict] = field(default_factory=list)  # small pictures of the old story (logos, badges): removed
    logos: list[dict] = field(default_factory=list)  # small pictures in the margins: the magazine's own, kept
    boxes: list[tuple[float, float, float, float]] = field(default_factory=list)  # coloured call-outs: x, y, w, h
    photos: list[dict] = field(default_factory=list)

    def capacity(self) -> int:
        return sum(c.word_capacity() for c in self.columns)


def _body_size(texts: list[dict]) -> float:
    """The page's running text: the size set in the most characters across multi-line frames."""
    weight: dict[float, int] = {}
    for e in texts:
        if e["text"].count("\n") >= 1 or len(e["text"]) > 120:
            s = round(e["style"]["size"], 1)
            weight[s] = weight.get(s, 0) + len(e["text"])
    return max(weight, key=weight.get) if weight else 10.0


def analyse(elements: list[dict], W: float, H: float, faces: dict[str, FaceInfo]) -> Slots:
    texts = [e for e in elements if e["kind"] == "text" and not e.get("rotation")]
    body = _body_size(texts)
    candidates = [e for e in texts if abs(e["style"]["size"] - body) <= 0.12 * body
                  and (e["text"].count("\n") >= 1 or e["w"] >= 0.3 * W)
                  and 0.05 * H < e["y"] and e["y"] + e["h"] < 0.95 * H]
    # Labels, bullets and subheads sitting between running text in its column belong to the old article: they go
    # with it, and the column runs through where they were. So does a drop cap (a letter or two in display type).
    absorbed = [e for e in texts if _drop_cap(e, body)]
    for o in texts:
        if o in candidates or o in absorbed or o["style"]["size"] > 1.6 * body:
            continue
        above = [a for a in candidates if a["y"] + a["h"] <= o["y"] + 2 and _overlap_x(o, a) >= 0.5 * o["w"]]
        below = [b for b in candidates if b["y"] >= o["y"] + o["h"] - 2 and _overlap_x(o, b) >= 0.5 * o["w"]]
        if any(abs(a["x"] - b["x"]) <= COLUMN_SLACK for a in above for b in below):
            absorbed.append(o)
    others = [e for e in elements if e not in candidates and e not in absorbed]
    slots = Slots()
    # Columns: body frames in one band of the page (a list may be indented), with nothing else in between.
    groups: list[list[dict]] = []
    for e in sorted(candidates, key=lambda e: e["y"]):
        g = next((g for g in groups if abs(g[-1]["x"] - e["x"]) <= COLUMN_SLACK
                  and abs(g[-1]["w"] - e["w"]) <= 0.2 * e["w"]
                  and not any(_between(o, g[-1], e) for o in others if o["kind"] != "image" or o["role"] != "background")),
                 None)
        if g is not None:
            g.append(e)
        else:
            groups.append([e])
    for g in groups:
        first, last = g[0], g[-1]
        lead = first["style"]["leading"]
        gaps = [b["y"] - (a["y"] + a["text"].count("\n") * lead + lead) for a, b in zip(g, g[1:])]
        gap = max(0.6 * lead, median(gaps)) if gaps else lead * 0.8
        # The running text's style: the first paragraph may be a bold intro.
        el = copy.deepcopy(max(g, key=lambda e: len(e["text"])))
        left = min(e["x"] for e in g)
        el.update(x=left, y=first["y"], id=f"el_col_{len(slots.columns) + 1}", name="Article text", text="",
                  role="body", styleRole="body", h=round(last["y"] + last["h"] - first["y"], 2),
                  paragraphGap=round(gap, 2))
        # The measure ends at the run's usual right edge (a ragged frame carries slack past it).
        multi = [e for e in g if e["text"].count("\n") >= 1] or g
        el["w"] = round(median(e["x"] + e["w"] for e in multi) - left, 2)
        lines = int((el["h"] - 0.2 * lead) // lead)
        slots.columns.append(Column(el, max(1, lines), gap / lead, face_for(el["style"], faces)))
    cols = list(slots.columns)
    band = {id(c): min(o.element["x"] for o in cols if _overlap_x(o.element, c.element)
                       >= 0.5 * min(o.element["w"], c.element["w"])) for c in cols}
    slots.columns.sort(key=lambda c: (round(band[id(c)]), c.element["y"]))
    # Anything else whose middle falls inside a column (an indented list label, a bullet) went with the old text.
    for o in texts:
        cx, cy = o["x"] + o["w"] / 2, o["y"] + o["h"] / 2
        if o not in candidates and o not in absorbed and o["style"]["size"] <= 1.6 * body and any(
                c.element["x"] <= cx <= c.element["x"] + c.element["w"] and
                c.element["y"] <= cy <= c.element["y"] + c.element["h"] for c in slots.columns):
            absorbed.append(o)
    margin = 0.09 * H  # running heads, folios and footers: the magazine's furniture
    slots.furniture = [e for e in texts if e not in candidates and e not in absorbed
                       and (e["y"] + e["h"] <= margin or e["y"] >= H - margin)]
    slots.texts = [e for e in texts if e not in candidates and e not in absorbed and e not in slots.furniture]
    page_area = W * H
    slots.photos = [e for e in elements if e["kind"] == "image" and e["role"] != "background"
                    and e["w"] * e["h"] >= PHOTO_MIN_AREA * page_area]
    # Smaller pictures between the margins are the old story's logos and badges; the magazine's own sit in them.
    slots.artwork = [e for e in elements if e["kind"] == "image" and e["role"] != "background"
                     and e not in slots.photos and margin < e["y"] + e["h"] / 2 < H - margin]
    slots.logos = [e for e in elements if e["kind"] == "image" and e["role"] != "background" and e not in slots.photos
                   and not any(e is a for a in slots.artwork)]
    return slots


def _drop_cap(e: dict, body: float) -> bool:
    return len(e["text"].strip()) <= 2 and e["style"]["size"] >= 2 * body


def _near(a: dict, b: dict, gap: float = 12) -> bool:
    return (a["x"] < b["x"] + b["w"] + gap and b["x"] < a["x"] + a["w"] + gap
            and a["y"] < b["y"] + b["h"] + gap and b["y"] < a["y"] + a["h"] + gap)


def _overlap_x(a: dict, b: dict) -> float:
    return max(0.0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))


def _between(o: dict, a: dict, b: dict) -> bool:
    """Whether frame `o` sits between `a` and `b` (a above b) in their column."""
    top, bottom = a["y"] + a["h"], b["y"]
    return o["y"] < bottom and o["y"] + o["h"] > top and o["x"] < a["x"] + a["w"] and o["x"] + o["w"] > a["x"]


def flow(paragraphs: list[str], columns: list[Column]) -> tuple[list[list[str]], list[str]]:
    """Paragraphs into columns line by line (a paragraph can break across columns). Returns (per column, leftover)."""
    out: list[list[str]] = [[] for _ in columns]
    queue = list(paragraphs)
    for i, col in enumerate(columns):
        s = col.style
        width = col.element["w"]
        room = float(col.lines)
        while queue and room >= 1:
            para = queue[0]
            subhead = para.startswith("## ")
            text = para[3:] if subhead else para
            lines = wrap(col.face, text, s["size"], width, s.get("tracking") or 0)
            need = len(lines) + (col.gap_lines if out[i] else 0)
            if need <= room:
                out[i].append(para)
                room -= need
                queue.pop(0)
                continue
            # Break the paragraph at a line and carry the rest on, never leaving one lone line on either side.
            fit = min(int(room - (col.gap_lines if out[i] else 0)), len(lines) - 2)
            if fit >= 2 and not subhead:
                head = " ".join(lines[:fit])
                out[i].append(head)
                queue[0] = text[len(head):].strip()
            break
        if queue and out[i] and out[i][-1].startswith("## "):
            queue.insert(0, out[i].pop())  # a subhead goes on with the paragraph it introduces
    return out, queue


# ------------------------------------------------------------------ the language model


FRAMES_SYSTEM = """You set a new article into a magazine page copied from a reference issue. The page's design
stays; its words change. The article's running text is already placed. For every other text frame listed, decide:
- "keep": furniture of the magazine itself, which stays word for word: the magazine or website name, taglines,
  social handles, advertising lines.
- "replace": what belonged to the old story, rewritten for the new one: the largest title is the article's headline
  (its own words; shorten only if it can't fit), then deck, standfirst, intro, section titles, labels such as
  "Message"; a pull quote (one of the article's quotes, word for word); a caption (from the article's captions or
  facts); speaker names and designations (from `speakers`); a byline or "Edited by" credit only from article.byline.
- "clear": old-story content with no counterpart in the material: a byline when article.byline is empty, a second
  person's name when there is one speaker, a quote when there are no quotes. Clearing is always better than
  padding: never fill a frame just because it exists, never write generic filler, and never put the same words in two
  frames.
New text must fit: at most `maxChars` characters and `lines` lines, the same letter case as the old text (ALL CAPS stays
ALL CAPS), the same kind of content. Put a line break (\n) only where the old text had one. Use only facts, names and
quotes from the material; never invent people, titles, numbers or quotes."""

FRAMES_SCHEMA = strict_object({"frames": {"type": "array", "items": strict_object({
    "id": {"type": "string"},
    "action": {"type": "string", "enum": ["keep", "replace", "clear"]},
    "text": {"type": "string"},
})}})

FIT_SYSTEM = """You fit a finished magazine article to the exact space a page layout gives it. Rewrite the body to
targetWords, within a few words: the result is measured line by line. Tighten, or expand by developing what the
article and sourceNotes (the transcript) actually say. Keep every fact, name, figure and quotation exactly as given,
and the article's order and voice. Never invent facts. Never mention timestamps, seconds, segments or the transcript
itself. Paragraphs are separate strings; a subhead is its own string starting with "## "."""

FIT_SCHEMA = strict_object({"paragraphs": {"type": "array", "items": {"type": "string"}}})


def is_reference_copy(elements: list[dict], imported: set[str] | frozenset[str] = frozenset()) -> bool:
    """A page copied from a reference PDF still shows that issue's content (its people, stories, page numbers), as
    does one built around a picture cut from it (`imported`: such asset ids)."""
    return any(e.get("literal") or e.get("assetId") in imported for e in elements or [])


def _daily_limit(e: LLMError) -> bool:
    msg = str(e).lower()
    return "429" in msg and ("per day" in msg or "(tpd)" in msg or "(rpd)" in msg or "daily" in msg)


def _rejected_output(e: LLMError) -> bool:
    """The provider refused this one request (a reply that didn't match the JSON schema, or a request over its size
    limit): the other model may well take it."""
    msg = str(e).lower()
    return "failed to validate json" in msg or "failed_generation" in msg or "http 413" in msg \
        or "request too large" in msg


class Writer:
    """The text model, moving to the vision model's endpoint (e.g. OpenAI beside a free Groq tier) once the
    first one's daily quota is spent: waiting out a daily limit isn't an option mid-build."""

    def __init__(self, primary, fallback=None):
        self.primary, self.fallback, self.switched = primary, fallback, False

    def configured(self):
        return self.primary.configured() if self.primary.configured()[0] or not self.fallback else self.fallback.configured()

    def complete_json(self, **kw) -> dict:
        if self.switched:
            return self.fallback.complete_json(**kw)
        try:
            return self.primary.complete_json(**kw)
        except LLMError as e:
            if self.fallback and _rejected_output(e):  # this reply only: the next request can go back to the first
                log.warning("text model's reply was rejected; asking %s", getattr(self.fallback, "model", "the fallback"))
                return self.fallback.complete_json(**kw)
            if not (self.fallback and _daily_limit(e)):
                raise
            log.warning("text model's daily quota is spent; continuing with %s", getattr(self.fallback, "model", "the fallback"))
            self.switched = True
            return self.fallback.complete_json(**kw)


def writer() -> Writer:
    primary, vision = get_llm(), get_vision_llm()
    same = (getattr(vision, "model", None), getattr(vision, "base_url", None)) == \
        (getattr(primary, "model", None), getattr(primary, "base_url", None))
    return Writer(primary, vision if vision is not primary and not same and vision.configured()[0] else None)


def _ask(llm, **kw) -> dict:
    """complete_json that waits out rate limits (free tiers allow a few thousand tokens a minute)."""
    import time

    for attempt in range(6):
        try:
            return llm.complete_json(**kw)
        except LLMError as e:
            msg = str(e)
            if attempt == 5 or not ("429" in msg or "rate limit" in msg.lower()):
                raise
            m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", msg)
            wait = (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else 15.0 * (attempt + 1)
            log.info("rate limited; waiting %.0fs", wait)
            time.sleep(min(90.0, wait + 1))
    raise AssertionError("unreachable")


def _frame_brief(e: dict, faces: dict[str, FaceInfo]) -> dict:
    s = e["style"]
    face = face_for(s, faces)
    lines = max(1, int(round((e["h"] - 0.25 * s["leading"]) / s["leading"])))
    sample = "Abcdefghij klmnopqrs tuvwxyz"
    per_char = _width(face, sample, s["size"], s.get("tracking") or 0) / len(sample)
    return {"id": e["id"], "text": e["text"][:300], "lines": lines,
            "maxChars": max(2, int(e["w"] / per_char * lines * 0.95)), "sizePt": round(s["size"], 1)}


def rewrite_frames(llm, texts: list[dict], faces: dict[str, FaceInfo], context: dict) -> dict[str, dict]:
    if not texts:
        return {}
    import json

    briefs = [_frame_brief(e, faces) for e in texts]
    out = _ask(llm, system=FRAMES_SYSTEM, schema=FRAMES_SCHEMA, schema_name="page_frames", max_tokens=6000,
                            prompt=json.dumps({"material": context, "frames": briefs}, ensure_ascii=False))
    return {f["id"]: f for f in out.get("frames") or []}


def fit_body(llm, article: dict, paragraphs: list[str], target_words: int, source: str = "") -> list[str]:
    """The body rewritten to `target_words`. `source` (transcript) gives an expansion something true to say."""
    import json

    have = sum(len(p.split()) for p in paragraphs)
    material = {"headline": article.get("headline"), "targetWords": target_words, "currentWords": have,
                "paragraphs": paragraphs}
    if source and target_words > have:
        material["sourceNotes"] = source
    # Providers count max_tokens against their per-minute limit: ask for what the target needs.
    out = _ask(llm, system=FIT_SYSTEM, schema=FIT_SCHEMA, schema_name="fitted_body",
               max_tokens=max(1500, target_words * 3),
               prompt=json.dumps(material, ensure_ascii=False))
    return [_tidy(p.strip()) for p in out.get("paragraphs") or [] if p.strip()] or paragraphs


def callout_boxes(page: pymupdf.Page, ox: float, oy: float, W: float, H: float, slots: Slots) -> list[tuple]:
    """Coloured panels drawn on the page that aren't behind running text or a picture: pull quotes, name bands."""
    out = []
    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001
        return out
    frames = [c.element for c in slots.columns] + slots.photos
    for d in drawings:
        fill, r = d.get("fill"), d.get("rect")
        if not fill or r is None or min(fill[:3]) > 0.93:
            continue
        box = (r.x0 - ox, r.y0 - oy, r.width, r.height)
        if not (80 <= box[2] <= 0.92 * W and 24 <= box[3] <= 0.5 * H):
            continue
        rect = {"x": box[0], "y": box[1], "w": box[2], "h": box[3]}
        # Behind running text or a picture it isn't a panel of its own; a band showing from under a picture is.
        if any(_overlap_x(rect, f) * max(0.0, min(f["y"] + f["h"], box[1] + box[3]) - max(f["y"], box[1]))
               >= 0.7 * box[2] * box[3] for f in frames):
            continue
        out.append(box)
    return out


def pull_quotes(article: dict, paragraphs: list[str]) -> list[str]:
    """Lines fit for a call-out: the article's quotes, then its own sentences of a good length."""
    quotes = [f"“{b['text'].strip().strip(chr(34) + '“”')}”" for b in article.get("body") or []
              if b.get("kind") == "quote" and 6 <= len((b.get("text") or "").split()) <= 34]
    sentences = [s.strip() for p in paragraphs if not p.startswith("## ")
                 for s in re.findall(r"[^.!?]+[.!?]+", p) if 9 <= len(s.split()) <= 26]
    return quotes + sentences[1::3]


def fill_callouts(slots: Slots, answers: dict[str, dict], pull: list[str], name: str | None) -> list[dict]:
    """A coloured panel never stays empty or keeps old words: a pull quote in a panel, a name on a thin band.
    Returns the frames that now carry a quote."""
    boxed = []
    for x, y, w, h in slots.boxes:
        inside = [e for e in slots.texts if x <= e["x"] + e["w"] / 2 <= x + w and y <= e["y"] + e["h"] / 2 <= y + h]
        if not inside:
            continue
        main = max(inside, key=lambda e: e["style"]["size"])
        said = answers.get(main["id"]) or {}
        words = len(said.get("text", "").split()) if said.get("action") == "replace" else 0
        if h < 60:
            if not words and name:
                answers[main["id"]] = {"id": main["id"], "action": "replace", "text": name}
            continue
        if words >= 5 or not pull:
            continue
        pad = min(14.0, 0.12 * w)
        main.update(x=round(x + pad, 2), y=round(y + pad * 0.7, 2), w=round(w - 2 * pad, 2), h=round(h - 1.4 * pad, 2))
        answers[main["id"]] = {"id": main["id"], "action": "replace", "text": pull.pop(0)}
        boxed.append(main)
        for other in inside:
            if other is not main:
                answers[other["id"]] = {"id": other["id"], "action": "clear", "text": ""}
    return boxed


def _lines(col: Column, para: str) -> int:
    text = para[3:] if para.startswith("## ") else para
    return len(wrap(col.face, text, col.style["size"], col.element["w"], col.style.get("tracking") or 0)) or 1


def measure(paragraphs: list[str], columns: list[Column]) -> tuple[float, int]:
    """(spare lines left in the columns, lines that don't fit), set as `flow` sets them."""
    chunks, left = flow(paragraphs, columns)
    spare = 0.0
    for col, paras in zip(columns, chunks):
        spare += col.lines - sum(_lines(col, p) for p in paras) - col.gap_lines * max(0, len(paras) - 1)
    return max(0.0, spare), sum(_lines(columns[-1], p) for p in left) if columns else 0


def stretch_columns(slots: Slots, elements: list[dict]) -> None:
    """Columns grow into the space old frames and logos left, up and down to whatever still stands on the page."""
    columns = [c.element for c in slots.columns]
    if not columns:
        return
    foot = max(c["y"] + c["h"] for c in columns)
    head = min(c["y"] for c in columns)
    solid = [e for e in elements if e not in columns and (e["kind"] != "image" or e["role"] != "background")
             and head - 200 < e["y"] + e["h"] and e["y"] < foot + 1]
    solid += [{"x": x, "y": y, "w": w, "h": h} for x, y, w, h in slots.boxes]
    for col in slots.columns:
        el = col.element
        lead = col.style["leading"]
        beside = [o for o in solid + [c for c in columns if c is not el]
                  if _overlap_x(o, el) > 0.25 * min(o["w"], el["w"])]
        bottom = min([o["y"] - 0.8 * lead for o in beside if o["y"] >= el["y"] + el["h"] - 2] + [foot])
        if bottom - (el["y"] + el["h"]) >= 2 * lead:
            el["h"] = round(bottom - el["y"], 2)
        top = max([o["y"] + o["h"] + 0.8 * lead for o in beside if o["y"] + o["h"] <= el["y"] + 2] + [head - 120])
        if el["y"] - top >= 3 * lead:
            el["h"] = round(el["h"] + el["y"] - top, 2)
            el["y"] = round(top, 2)
        col.lines = max(1, int((el["h"] - 0.2 * lead) // lead))


def grow_photos(slots: Slots, used: list[Column]) -> None:
    """An article that ends early leaves a column empty: a picture beside that space takes it over."""
    for col in slots.columns:
        if any(c is col for c in used):
            continue
        el = col.element
        nearby = [p for p in slots.photos if _overlap_x(p, el) > 0.4 * min(p["w"], el["w"])]
        if not nearby:
            continue
        ph = max(nearby, key=lambda p: p["w"] * p["h"])
        x0, y0 = min(ph["x"], el["x"]), min(ph["y"], el["y"])
        x1, y1 = max(ph["x"] + ph["w"], el["x"] + el["w"]), max(ph["y"] + ph["h"], el["y"] + el["h"])
        ph.update(x=round(x0, 2), y=round(y0, 2), w=round(x1 - x0, 2), h=round(y1 - y0, 2))


def _echoes(text: str, paragraph: str) -> bool:
    """Whether a frame's text restates a paragraph (the same words, not necessarily in the same order)."""
    words = set(re.findall(r"\w+", text.lower()))
    return len(words) >= 8 and len(words & set(re.findall(r"\w+", paragraph.lower()))) >= 0.6 * len(words)


def merge_short(paragraphs: list[str], columns: list[Column]) -> list[str]:
    """Many short paragraphs lose a line each to spacing and part-filled ends: joining the shortest neighbours gets
    those lines back with every word kept."""
    paras = list(paragraphs)
    while measure(paras, columns)[1] and len(paras) > 2:
        pairs = [(len(paras[i]) + len(paras[i + 1]), i) for i in range(len(paras) - 1)
                 if not paras[i].startswith("## ") and not paras[i + 1].startswith("## ")]
        if not pairs:
            break
        i = min(pairs)[1]
        paras[i:i + 2] = [f"{paras[i]} {paras[i + 1]}"]
    return paras


def fit_to_columns(llm, article: dict, paragraphs: list[str], columns: list[Column], source: str) -> list[str]:
    """Rewrites the body until it fills its columns: the count comes from setting it, so each pass corrects the last."""
    total = sum(c.lines for c in columns)
    if not total or not paragraphs:
        return paragraphs
    best, best_score = paragraphs, None
    paras = paragraphs
    for attempt in range(FIT_PASSES + 1):
        spare, over = measure(paras, columns)
        timed = bool(TIMINGS.search(" ".join(paras)))
        score = spare + 3 * over + (40 if timed else 0)
        if best_score is None or score < best_score:
            best, best_score = paras, score
        if (over == 0 and spare <= max(3, 0.04 * total) and not timed) or attempt == FIT_PASSES:
            break
        words = sum(len(p.split()) for p in paras)
        target = round(words * (total * 0.97) / max(1.0, total - spare + over))
        try:
            paras = fit_body(llm, article, paras, target, source)
        except LLMError as e:
            log.warning("fitting the article to its pages failed: %s", e)
            break
    if measure(best, columns)[1]:  # still a little long: tighter paragraph spacing before any text is cut
        for c in columns:
            c.gap_lines *= 0.5
            c.element["paragraphGap"] = round(c.element["paragraphGap"] * 0.5, 2)
    for _ in range(3):  # then type a touch smaller, on the same leading
        if not measure(best, columns)[1]:
            break
        for c in columns:
            c.style["size"] = round(c.style["size"] * 0.98, 2)
    best = merge_short(best, columns)
    for _ in range(2):  # and only then a firmly shorter rewrite, so the article ends on its own last paragraph
        over = measure(best, columns)[1]
        log.info("article %s: %d of %d lines, %d over", article.get("id"), sum(_lines(columns[0], p) for p in best), total,
                 over)
        if not over:
            break
        words = sum(len(p.split()) for p in best)
        total = sum(c.lines for c in columns)
        try:
            shorter = fit_body(llm, article, best, round(words * total / (total + over) * 0.9))
        except LLMError:
            break
        if measure(shorter, columns)[1] < over:
            best = shorter
    return best


def settle(columns: list[Column], chunks: list[list[str]]) -> None:
    """Whatever space is left on the last page is shared between its paragraphs, so the columns reach the foot."""
    for col, paras in zip(columns, chunks):
        if len(paras) < 2:
            continue
        lead = col.style["leading"]
        used = sum(_lines(col, p) for p in paras) + col.gap_lines * (len(paras) - 1)
        room = (col.lines - used) * lead
        if room > 0:
            col.element["paragraphGap"] = round(col.element["paragraphGap"] + min(room / (len(paras) - 1), 1.2 * lead), 2)


def face_covering(style: dict, text: str, faces: dict[str, FaceInfo]) -> FaceInfo | None:
    """A complete font for text a subset can't set. The PDF embeds only the letters it used, so new words may need
    others; one whole substitute reads better than a line of two fonts. None when the frame's own face will do."""
    face = face_for(style, faces)
    if face is None or face.google:
        return None
    have = set().union(*(cov for _d, cov in face.metrics))
    if {ord(c) for c in text if not c.isspace()} <= have:
        return None
    pool = list({id(f): f for f in faces.values() if f.google and f.italic == face.italic and f.metrics}.values())
    pool = [f for f in pool if f.classification == face.classification] or pool
    return min(pool, key=lambda f: abs(f.weight - face.weight)) if pool else None


def use_face(style: dict, face: FaceInfo | None) -> None:
    if face:
        style.update(fontFamily=face.family, weight=face.weight, italic=face.italic)
        style.pop("fontStyle", None)
        if face.style_name:
            style["fontStyle"] = face.style_name


def _fill_contents(elements: list[dict], entries: int) -> None:
    """A short contents list leaves most of the template empty: the list runs across the page, the picture fills the
    rest, the credits sit at its foot."""
    by_id = {e["id"]: e for e in elements}
    toc, img, credits = by_id.get("el_toc"), by_id.get("el_img"), by_id.get("el_credits")
    if not (toc and img) or entries > 8:
        return
    right = img["x"] + img["w"]
    bottom = toc["y"] + toc["h"]
    toc["w"] = round(right - toc["x"], 2)
    toc["h"] = round(entries * 46 + 8, 2)
    img.update(x=toc["x"], y=round(toc["y"] + toc["h"] + 14, 2), w=toc["w"])
    img["h"] = round(bottom - 44 - img["y"], 2)
    if credits:
        credits.update(x=toc["x"], y=round(bottom - 34, 2), w=toc["w"], h=34)
    for e in elements:
        if e.get("role") == "caption" and e is not credits:
            e["hidden"] = True


def in_display_type(elements: list[dict], pages: list[dict], faces: dict[str, FaceInfo], profile: dict) -> list[dict]:
    """The cover's and contents' headings in the display face of the issue's first title, fitted to their frames
    here (the InDesign package has no shrink-to-fit of its own)."""
    texts = [e for e in (pages[0]["elements"] if pages else []) if e["kind"] == "text"]
    if not texts:
        return elements
    title = max(texts, key=lambda e: e["style"].get("size") or 0)["style"]
    out = copy.deepcopy(elements)
    _fill_contents(out, sum(1 for p in pages if not p["article_part"]))
    for e in out:
        if e.get("kind") == "text" and e.get("role") == "headline" and (e.get("text") or "").strip():
            style = e.setdefault("style", {})
            base = (profile.get("typography") or {}).get(e.get("styleRole") or "headline") or {}
            style.setdefault("size", base.get("size") or 40)
            style.setdefault("leading", base.get("leading") or style["size"] * 1.1)
            style.update({k: title[k] for k in ("fontFamily", "weight", "italic") if k in title})
            style.pop("fontStyle", None)
            if title.get("fontStyle"):
                style["fontStyle"] = title["fontStyle"]
            _set_text(e, e["text"], faces)
    return out


def _tidy(text: str) -> str:
    """Characters a model likes and most fonts lack: non-breaking hyphens and spaces."""
    return text.replace("‑", "-").replace(" ", " ").replace(" ", " ")


def _set_text(e: dict, text: str, faces: dict[str, FaceInfo], room: float | None = None) -> None:
    """New copy in a frame, shrinking the type as far as it takes when it needs more lines than the frame has
    (`room`: the height it may use, when less than the frame's)."""
    s = e["style"]
    text = _tidy(text)
    if len(text) > 80:  # running sentences wrap to the frame rather than keep a model's line breaks
        text = re.sub(r"\s*\n\s*", " ", text)
    e["text"] = text
    use_face(s, face_covering(s, text, faces))
    face = face_for(s, faces)
    size, leading = s["size"], s["leading"]
    width = e["w"] * 0.97  # measured without kerning or hinting: keep a little in hand
    room = min(e["h"], room or e["h"]) + 0.01
    for f in [1.0 - 0.05 * i for i in range(18)]:  # down to 15%: a long headline in a short title's frame
        need = sum(len(wrap(face, part, size * f, width, s.get("tracking") or 0)) or 1 for part in text.split("\n"))
        if need * leading * f <= room:
            break
    if f < 1.0:
        s["size"], s["leading"] = round(size * f, 2), round(leading * f, 2)
    e["name"] = " ".join(text.split()[:5]) or e["name"]


# ------------------------------------------------------------------ choosing pages


@dataclass
class Candidate:
    number: int
    layout: str
    capacity: int
    display: bool  # carries a headline-size frame: an opener
    photos: int


def survey(src: pymupdf.Document, rows: dict[int, ReferencePage], faces: dict[str, FaceInfo], profile: dict
           ) -> list[Candidate]:
    """Every reference page with running text, measured from its text alone (fast: nothing is rendered)."""
    out = []
    for number, row in rows.items():
        if row.layout_type in ("advertisement", "full_page_image", "cover", "toc") or number == 1:
            continue
        page = src[number - 1]
        if not has_live_text(page) or SPONSORED.search(page.get_text()[:4000] or ""):
            continue
        ox, oy, W, H = _page_frame(page)
        blocks, _ = _pieces(page, ox, oy)
        texts = [text_element(f, faces, 10, i, i) for i, f in enumerate(_frames(blocks))]
        try:
            infos = page.get_image_info()
        except Exception:  # noqa: BLE001
            infos = []
        photos = sum(1 for i in infos if (i["bbox"][2] - i["bbox"][0]) * (i["bbox"][3] - i["bbox"][1]) >= PHOTO_MIN_AREA * W * H)
        slots = analyse(texts, W, H, faces)
        if not slots.columns:
            continue
        body = _body_size(texts)
        display = any(e["style"]["size"] >= 1.5 * body and len(e["text"].strip()) > 3 for e in slots.texts)
        out.append(Candidate(number, row.layout_type, slots.capacity(), display, photos))
    return out


def choose_pages(article: dict, words: int, cands: list[Candidate], used: set[int], photos: int) -> list[Candidate]:
    """An opener of the article's kind plus continuation pages, closest to the article's length."""
    kinds = LAYOUTS.get(article.get("type") or "", DEFAULT_LAYOUTS)

    def rank(c: Candidate, kind_order: list[str], need: int) -> tuple:
        kind = kind_order.index(c.layout) if c.layout in kind_order else len(kind_order)
        return (c.number in used, kind, abs(c.capacity - need) / max(need, 1) + 0.15 * max(0, c.photos - photos))

    openers = [c for c in cands if c.display] or cands
    if not openers:
        return []
    chosen = [min(openers, key=lambda c: rank(c, kinds, words))]
    remaining = words - chosen[0].capacity
    conts = [c for c in cands if not c.display] or cands
    while remaining > 0.25 * chosen[0].capacity and len(chosen) < 6:
        pool = [c for c in conts if c not in chosen]
        if not pool:
            break
        nxt = min(pool, key=lambda c: rank(c, CONTINUATION, remaining))
        chosen.append(nxt)
        remaining -= nxt.capacity
    return chosen


# ------------------------------------------------------------------ the job


def _photo_pool(article: dict | None, assets: dict[str, dict], used: set[str]) -> list[str]:
    ids = list((article or {}).get("suggestedImageIds") or [])
    sess = set((article or {}).get("sessionIds") or [])
    photos = [a for a in assets.values() if a.get("kind") in ("photo", "video_frame") and a.get("url")
              and not {"reference-import", COVER_TAG} & set(a.get("tags") or [])]
    ids += [a["id"] for a in photos if a.get("sessionId") in sess]
    ids += [a["id"] for a in photos]
    seen, out = set(), []
    for i in ids:
        if i in assets and i not in seen and i not in used:
            seen.add(i)
            out.append(i)
    return out


def _fill_photos(slots: Slots, pool: list[str], used: set[str], illustrate=None) -> int:
    """The project's photographs into the page's photo frames, largest frame first; `illustrate(frame)` may supply
    an AI image for a frame no photograph is left for. Returns the frames left empty."""
    empty = 0
    for e in sorted(slots.photos, key=lambda e: -e["w"] * e["h"]):
        aid = next((a for a in pool if a not in used), None) or (illustrate(e) if illustrate else None)
        if aid:
            used.add(aid)
            e.update(assetId=aid, crop={"scale": 1, "offsetX": 0, "offsetY": 0}, name="Photo")
        else:
            e.pop("assetId", None)  # an empty frame to place a photo in, never the reference's picture
            e["name"] = "Photo (empty)"
            empty += 1
    return empty


def _apply_frames(slots: Slots, answers: dict[str, dict], elements: list[dict], faces: dict[str, FaceInfo],
                  article: dict | None = None, boxed: tuple = ()) -> None:
    """The model's answers, checked: on an opening page the largest title is the headline, no other frame repeats
    it (the deck gets the subheadline), and no sentence of the old story survives a "keep". `boxed` frames were
    filled from call-outs and are neither title nor deck."""
    headline = (article or {}).get("headline")
    body = _body_size(slots.texts + [c.element for c in slots.columns])
    by_size = sorted((e for e in slots.texts if not any(e is b for b in boxed)), key=lambda e: -e["style"]["size"])
    title = by_size[0] if headline and by_size and by_size[0]["style"]["size"] >= 1.4 * body else None
    if title:
        # A title set in several frames (two colours, two sizes) is one block: the headline fills all of it.
        parts = [e for e in by_size[1:] if e["style"]["size"] >= 0.6 * title["style"]["size"]
                 and abs(e["y"] - (title["y"] + title["h"])) < title["style"]["size"] * 1.5 + e["h"]]
        for e in parts:
            x0, y0 = min(title["x"], e["x"]), min(title["y"], e["y"])
            x1, y1 = max(title["x"] + title["w"], e["x"] + e["w"]), max(title["y"] + title["h"], e["y"] + e["h"])
            title.update(x=round(x0, 2), y=round(y0, 2), w=round(x1 - x0, 2), h=round(y1 - y0, 2))
            slots.texts.remove(e)
            elements.remove(e)
        by_size = [e for e in by_size if e not in parts]
    deck = next((e for e in by_size[1:] if e["style"]["size"] >= 1.3 * body), None) if title else None
    if title:
        # A short title may overlap a picture by design; a long headline in its place stops short of it.
        for ph in slots.photos:
            if ph["x"] > title["x"] + 20 and ph["y"] < title["y"] + title["h"] and ph["y"] + ph["h"] > title["y"]:
                title["w"] = round(min(title["w"], ph["x"] - title["x"] - 8), 2)

    def same(a: str, b: str) -> bool:
        norm = lambda t: re.sub(r"\W+", " ", t).strip().lower()  # noqa: E731
        return bool(a and b) and (norm(a) in norm(b) or norm(b) in norm(a))

    # New copy keeps to the text area, which a frame's slack for the old line could run past.
    right = max((c.element["x"] + c.element["w"] for c in slots.columns), default=None)
    if right:
        for e in slots.texts:
            if e["x"] < right < e["x"] + e["w"]:
                e["w"] = round(right - e["x"], 2)

    def room_above_next(e: dict) -> float:
        below = [o["y"] for o in slots.texts if o is not e and o["y"] > e["y"] + 4 and _overlap_x(o, e) > 0]
        return max(e["style"]["leading"], min(below) - e["y"] if below else e["h"])

    deck_done = False
    for e in list(slots.texts):
        a = answers.get(e["id"]) or {}
        new = a.get("text", "").strip() if a.get("action") == "replace" else ""
        if e is title:
            _set_text(e, new or headline, faces, room_above_next(e))
            continue
        sub = (article or {}).get("subheadline") or ""
        if e is deck and not deck_done:
            _set_text(e, new if new and not same(new, headline) else sub or new or e["text"], faces, room_above_next(e))
            deck_done = True
            continue
        if title and new and same(new, headline):
            elements.remove(e)
            continue
        if not a or a.get("action") == "keep":
            # Furniture in the margins never reaches the model, so nothing it keeps here is the magazine's own: a
            # label beside a logo at most. Text of the old story never stays.
            if len(e["text"].split()) > 4 or not slots.artwork and not any(_near(e, g) for g in slots.logos):
                elements.remove(e)
            continue
        if a["action"] == "clear" or not new or PLACEHOLDER.fullmatch(new):
            elements.remove(e)
        else:
            _set_text(e, new, faces)


SECTION_LABELS = {"editorial": "From the editor", "event_report": "Event report", "highlights": "Highlights",
                  "keynote": "Keynote", "interview": "Interview", "panel": "Panel discussion",
                  "government_perspective": "Government perspective", "tech_feature": "Technology",
                  "speaker_profiles": "Speakers"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def section_label(article: dict) -> str:
    """The running head for an article: its kicker, or what kind of story it is."""
    kicker = re.split(r"[·|]", article.get("kicker") or "")[0].strip()
    return kicker or SECTION_LABELS.get(article.get("type") or "", (article.get("type") or "Feature").replace("_", " "))


def _update_furniture(slots: Slots, page_number: int, date: str | None, label: str | None = None,
                      faces: dict[str, FaceInfo] | None = None) -> None:
    """Page numbers follow the new order; an issue month/year becomes the project's, in the same case and form; the
    running head at the top names this article, not the reference's section."""
    m = re.match(r"(\d{4})-(\d{2})", date or "")
    tops = [e for e in slots.furniture if e.get("y", 1e9) < 100 and re.search(r"[A-Za-z]{3}", e["text"])]
    if label and tops:
        head = max(tops, key=lambda e: len(e["text"]))
        _set_text(head, label.upper() if head["text"].isupper() else label, faces or {})
    for e in slots.furniture:
        text = e["text"].strip()
        if re.fullmatch(r"\d{1,3}", text):
            e["text"] = str(page_number)
        elif m and re.fullmatch(r"[A-Za-z]{3,9}\.?\s+\d{4}", text):
            month = MONTHS[int(m.group(2)) - 1]
            word = text.split()[0].rstrip(".")
            month = month[:3] if len(word) == 3 else month
            e["text"] = f"{month.upper() if word.isupper() else month} {m.group(1)}"


def _plain(paragraphs: list[str]) -> list[str]:
    """Article markup reduced to what a copied frame sets: paragraphs and "## " subheads."""
    out = []
    for p in paragraphs:
        if p.startswith("^ "):
            p = p[2:]
        elif p.startswith("> "):
            p = p[2:]
        elif p.startswith("Q: "):
            p = "## " + p[3:]
        out.append(p)
    return out


@register("reference_fill")
def run_reference_fill(ctx: JobContext) -> None:
    project_id = ctx.payload["projectId"]
    storage = get_storage()
    with session_factory()() as db:
        p = db.get(Project, project_id)
        if p is None:
            raise PermanentError("Project no longer exists")
        ref = db.get(ReferenceMagazine, ctx.payload.get("referenceId") or p.reference_id or "")
        if ref is None or not ref.storage_key:
            raise PermanentError("The project has no reference magazine with a stored PDF")
        org_id, ref_id, key = p.org_id, ref.id, ref.storage_key
        profile, _ = profile_for_project(db, p)
        project = dump(ser.project(p))
        wanted = set(ctx.payload.get("articleIds") or [])
        articles = [dump(ser.article(a)) for a in db.scalars(select(Article).where(Article.project_id == p.id)
                                                              .order_by(Article.created_at))]
        articles = [a for a in articles if (a["id"] in wanted if wanted else a["status"] != "rejected")]
        if ctx.payload.get("articleIds"):  # in the order given (the issue's running order)
            order = {aid: i for i, aid in enumerate(ctx.payload["articleIds"])}
            articles.sort(key=lambda a: order.get(a["id"], len(order)))
        speakers = {s.id: dump(ser.speaker(s)) for s in db.scalars(select(Speaker).where(Speaker.project_id == p.id))}
        assets = assets_by_id(db, p.id)
        rows = {r.page_number: r for r in db.scalars(select(ReferencePage).where(ReferencePage.reference_id == ref_id))}
        for r in rows.values():
            db.expunge(r)
        imported = {i for i, a in assets.items() if "reference-import" in (a.get("tags") or [])}
        front_pages = [pg for pg in db.scalars(select(MagazinePage).where(MagazinePage.project_id == p.id)
                                               .order_by(MagazinePage.position))
                       if pg.page_type in ("cover", "toc") and not pg.locked
                       and not is_reference_copy(pg.elements, imported)]
        kept_front = [pg.id for pg in front_pages]
        front_assets = {e["assetId"] for pg in front_pages for e in pg.elements if e.get("assetId")}
        source = " ".join(s.text for s in db.scalars(select(TranscriptSegment).where(
            TranscriptSegment.project_id == p.id).order_by(TranscriptSegment.video_id, TranscriptSegment.idx)))[:4000]
    if not articles:
        raise PermanentError("Write at least one article from the videos first (Content Studio)")
    llm = writer()
    if not llm.configured()[0]:
        raise PermanentError("Design with reference needs an AI provider (LLM_PROVIDER)")
    pdf = storage.get_bytes(key)
    src = pymupdf.open(stream=pdf, filetype="pdf")
    try:
        ctx.progress(3, "Reading the reference's fonts")
        google = GoogleFonts()
        try:
            faces = collect_faces(src, google)
        finally:
            google.close()
        _store_fonts(org_id, project_id, faces)
        ctx.progress(10, "Measuring the reference's pages")
        cands = survey(src, rows, faces, profile)
        if not cands:
            raise PermanentError("The reference has no article pages with live text to set copy into")

        # The plan: each article's pages. The project's own cover and contents page stay in front: a reference's are
        # built around its own event (artwork headline, its people in the background) and its own stories.
        plan: list[dict] = []
        used_pages: set[int] = set()
        for a in articles:
            paras = _plain(article_paragraphs(a, speakers, set()))
            words = sum(len(x.split()) for x in paras)
            pages = choose_pages(a, words, cands, used_pages, len(_photo_pool(a, assets, set())))
            used_pages.update(c.number for c in pages)
            for i, c in enumerate(pages):
                plan.append({"kind": "article", "number": c.number, "article": a, "part": i, "of": len(pages),
                             "paragraphs": paras if i == 0 else None, "capacity": sum(x.capacity for x in pages)})

        vision = get_vision_llm()
        cache = ImageCache()
        used_photos: set[str] = set(front_assets)  # no picture appears twice in the issue
        out_pages: list[dict] = []
        empty_photos = 0
        cut_paragraphs = 0
        images_made = [0]
        new_assets: list = []

        def save(data: bytes, ctype: str, w: int, h: int, kind: str, name: str) -> str:
            import hashlib

            from app.models import Asset

            aid = new_id("ast")
            ext = {"image/png": ".png", "image/jpeg": ".jpg"}[ctype]
            skey = f"assets/{org_id}/{project_id}/{aid}{ext}"
            storage.put_bytes(skey, data, ctype)
            new_assets.append(Asset(id=aid, org_id=org_id, project_id=project_id, kind=kind,
                                    file_name=f"{name}{ext}"[:400], content_type=ctype, size_bytes=len(data),
                                    width_px=w, height_px=h, tags=["reference-import"], caption=name,
                                    credit=f"From {ref.title}", storage_key=skey, simulated=False,
                                    seed=int(hashlib.sha256(aid.encode()).hexdigest()[:8], 16)))
            return aid

        ai_budget = [AI_IMAGE_LIMIT if ctx.payload.get("aiImages") and images.configured()[0] else 0]

        def illustrate(frame: dict, article: dict) -> str | None:
            """An editorial AI image for an empty photo frame (labelled as AI-generated), while the budget lasts."""
            if ai_budget[0] <= 0:
                return None
            ai_budget[0] -= 1
            images_made[0] += 1
            ctx.progress(15 + 80 * idx / len(plan), f"Page {idx + 1} of {len(plan)}: creating AI image {images_made[0]}")
            w, h = frame["w"], frame["h"]
            aspect = "portrait" if h > 1.15 * w else "landscape" if w > 1.15 * h else "square"
            prompt = (f"Photograph for a magazine article titled \"{article['headline']}\". "
                      f"{article.get('subheadline') or ''} Setting: {project.get('eventName') or 'a conference'}.")
            try:
                png, pw, ph = images.generate(prompt, aspect)
            except images.ImageGenError as e:
                log.warning("AI image for an empty frame failed: %s", e)
                ai_budget[0] = 0  # one refusal or outage: leave the rest empty rather than retry each frame
                return None
            aid = save(png, "image/png", pw, ph, "photo", f"AI image for {article['headline'][:60]}")
            new_assets[-1].tags = [images.AI_TAG]
            new_assets[-1].credit = f"AI-generated image ({get_settings().image_model})"
            return aid

        def build(number: int) -> tuple[list[dict], Slots]:
            work = pymupdf.open(stream=pdf, filetype="pdf")  # a fresh copy each page: backgrounds remove shared images
            try:
                elements = None
                if not has_live_text(src[number - 1]) and vision.supports_vision and vision.configured()[0]:
                    row = rows.get(number)
                    elements = build_flat_page(src, work, number - 1, faces, profile, save, cache, vision,
                                               lambda: _vision_blocks(pdf, row, number, src[number - 1].rect.height))
                if elements is None:
                    elements = build_live_page(src, work, number - 1, faces, profile, save, cache, every_photo=True)
            finally:
                work.close()
            ox, oy, W, H = _page_frame(src[number - 1])
            slots = analyse(elements, W, H, faces)
            slots.boxes = callout_boxes(src[number - 1], ox, oy, W, H, slots)
            return elements, slots

        def briefing(entry: dict, page_number: int) -> dict:
            article = entry["article"]
            spk = [speakers[s] for s in article.get("speakerIds") or [] if s in speakers]
            return {"magazine": project.get("title"), "event": project.get("eventName"),
                    "date": project.get("publicationDate"), "pageNumber": page_number,
                    "role": f"page {entry['part'] + 1} of {entry['of']} of the article"
                            + (" (its opening page)" if entry["part"] == 0 else " (a continuation)"),
                    "article": {k: article.get(k) for k in ("type", "kicker", "headline", "subheadline", "byline",
                                                            "intro", "captions")},
                    "quotes": [b["text"] for b in article.get("body") or [] if b.get("kind") == "quote"][:4],
                    "speakers": [{k: s.get(k) for k in ("name", "designation", "organisation")} for s in spk]}

        idx = -1
        groups = [[e for e in plan if e["article"] is a] for a in articles]
        for entries in filter(None, groups):
            article = entries[0]["article"]
            # Every page of the article first: the body is measured against all its columns before any is set.
            first_idx = idx + 1
            built = []
            for k, entry in enumerate(entries):
                ctx.progress(15 + 80 * (first_idx + k) / len(plan),
                             f"Page {first_idx + k + 1} of {len(plan)}: copying reference p.{entry['number']}")
                built.append(build(entry["number"]))
            answers = []
            for k, (entry, (_els, slots)) in enumerate(zip(entries, built)):
                try:
                    answers.append(rewrite_frames(llm, slots.texts, faces,
                                                  briefing(entry, len(kept_front) + first_idx + k + 1)))
                except LLMError as e:
                    raise PermanentError(f"The AI couldn't write page {first_idx + k + 1}: {e}") from e
            paras = [_tidy(p) for p in entries[0]["paragraphs"]]
            # The intro heads the body; when a frame on the opener carries it, the body doesn't repeat it.
            if paras and any(_echoes(a.get("text") or "", paras[0]) for a in answers[0].values()
                             if a.get("action") == "replace"):
                paras = paras[1:]
            # Each page is cleared and its frames written first, so the columns can grow into the space that leaves;
            # the body is then fitted to the columns as they finally stand.
            pull = pull_quotes(article, paras)
            # The band under a picture names the event: a person's name would claim an AI image shows them.
            name = project.get("eventName") or section_label(article)
            for k, (elements, slots) in enumerate(built):
                boxed = fill_callouts(slots, answers[k], pull, name)
                _apply_frames(slots, answers[k], elements, faces, article if k == 0 else None, boxed)
                _update_furniture(slots, len(kept_front) + first_idx + k + 1, project.get("publicationDate"),
                                  section_label(article), faces)
                gone = {id(e) for e in elements if e["kind"] == "text"} - {id(e) for e in slots.texts} \
                    - {id(e) for e in slots.furniture}
                elements[:] = [e for e in elements if id(e) not in gone and not any(e is a for a in slots.artwork)]
                stretch_columns(slots, elements)
            ctx.progress(15 + 80 * first_idx / len(plan), f"Fitting “{article['headline'][:40]}” to its pages")
            carry = fit_to_columns(llm, article, paras, [c for _e, sl in built for c in sl.columns], source)
            for k, (entry, (elements, slots)) in enumerate(zip(entries, built)):
                idx = first_idx + k
                number = entry["number"]
                chunks, carry = flow(carry, slots.columns)
                if k == len(entries) - 1 and not carry:
                    settle(slots.columns, chunks)
                for col, para_list in zip(slots.columns, chunks):
                    if para_list:
                        col.element["text"] = "\n\n".join(para_list)
                        use_face(col.style, face_covering(col.style, " ".join(para_list), faces))
                        elements.append(col.element)
                if k == len(entries) - 1 and carry:
                    # More than the pages hold: the article ends at the last whole sentence that fits.
                    log.info("article %s: %d paragraph(s) did not fit", article["id"], len(carry))
                    cut_paragraphs += len(carry)
                    last = next((c.element for c in reversed(slots.columns) if c.element["text"]), None)
                    if last:
                        kept = last["text"].split("\n\n")
                        cut = re.match(r"(?s)(.*[.!?][\"”’)]*)\s", kept[-1] + " ")
                        kept[-1] = cut.group(1) if cut else kept[-1]
                        last["text"] = "\n\n".join(p for p in kept if p.strip())
                if k == len(entries) - 1:
                    grow_photos(slots, [c for c, chunk in zip(slots.columns, chunks) if chunk])
                empty_photos += _fill_photos(slots, _photo_pool(article, assets, used_photos), used_photos,
                                             illustrate=lambda frame, a=article: illustrate(frame, a))
                layout = rows[number].layout_type
                out_pages.append({"page_type": layout if layout in ("event_report", "interview", "editorial")
                                  else "two_column_article", "template_id": ref_template_id(ref_id, number),
                                  "label": article["headline"][:80] + (f" (cont. {k})" if k else ""),
                                  "article_id": article["id"], "article_part": k, "elements": elements})
    finally:
        src.close()

    with session_factory()() as db:
        for a in new_assets:
            db.add(a)
        locked = list(db.scalars(select(MagazinePage).where(MagazinePage.project_id == project_id,
                                                            MagazinePage.locked.is_(True))
                                 .order_by(MagazinePage.position)))
        db.execute(delete(MagazinePage).where(MagazinePage.project_id == project_id, MagazinePage.locked.is_(False),
                                              MagazinePage.id.not_in(kept_front)))
        for pos, pid in enumerate(kept_front):
            front = db.get(MagazinePage, pid)
            front.position = pos
            if ctx.payload.get("styleFront"):
                front.elements = in_display_type(front.elements, out_pages, faces, profile)
        for pos, pg in enumerate(out_pages, start=len(kept_front)):
            db.add(MagazinePage(id=new_id("pg"), project_id=project_id, position=pos, page_type=pg["page_type"],
                                template_id=pg["template_id"], label=pg["label"], article_id=pg["article_id"],
                                article_part=pg["article_part"], elements=pg["elements"], editorial_status="pending",
                                design_status="in_progress", locked=False))
        for i, row in enumerate(locked):  # locked pages keep their content, after the new ones
            row.position = len(kept_front) + len(out_pages) + i
        p = db.get(Project, project_id)
        p.status = "design"
        db.commit()
    note = f" {empty_photos} photo frame(s) are empty for you to fill." if empty_photos else ""
    if cut_paragraphs:
        note += f" {cut_paragraphs} paragraph(s) didn't fit and were left out: see Content Studio for the full text."
    ctx.progress(100, f"Set {len(articles)} article(s) into {len(out_pages)} reference pages.{note}")
    ctx.set_result(project_id)

