"""The checking agent: finds what's broken on finished pages and fixes it, so little is left for the page editor.

Every page is drawn as it really looks (page_render) and judged twice. Exact checks measure the rendered text and the
frames: overset text, overlaps, frames off the page, pictures losing most of their area to the crop, empty frames,
leftover timestamps and placeholder words; the ones with one right answer are corrected directly. A vision model then
looks at the picture, as an art director would, and proposes frame operations for what remains. Its operations go
through the page editor's validation, can't touch article text or delete copy, and only stay if the next rendering
has no more exact problems than before. Whatever can't be fixed is noted on the page for the designer.
"""

from __future__ import annotations

import copy
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from sqlalchemy import select

from app.db import session_factory
from app.models import Article, MagazinePage, Project
from app.services import page_ai_edit
from app.services import serializers as ser
from app.services.jobs.queue import JobContext, PermanentError, register
from app.services.llm import LLMError, get_vision_llm, strict_object
from app.services.page_builder import build_toc
from app.services.page_render import Renderer, Rendered, effective_style, find_browser
from app.services.project_context import assets_by_id, dump, profile_for_project
from app.services.reference_fill import PLACEHOLDER, TIMINGS, _ask

log = logging.getLogger("pressflow.page_review")

MAX_ROUNDS = 3  # looks at a page at most; each after the first checks the last round's fixes
WORKERS = 4
BODY_WORDS = 30  # frames with more words are article copy: the reviewer may fit them, never rewrite them
MIN_SIZE = {"body": 0.9, "caption": 0.85, "headline": 0.45, "deck": 0.7}
CROP_LOSS = 0.5  # a picture losing more than this share of its area to the frame's shape

SYSTEM = """You are the art director signing off a magazine page before it goes to print. You see the page as it will
print, plus its frames as JSON (points from the page's top-left). Look hard for what a reader would notice:
- text overlapping other text, or running over a picture it wasn't designed to sit on
- misaligned frames: edges that nearly line up but don't, columns that start or end at different heights, a heading not
  aligned with its text, uneven gutters or margins
- pictures pushed out of their space, cut off awkwardly (heads or faces cropped), or leaving a stray gap
- text cut off at a frame's edge, or too large or too small for its role in the hierarchy
- text with too little contrast against what's behind it
- large unintentional empty areas: a column stopping far short of its neighbour, a hole under a pull quote or picture
- leftover oddities: stray marks, rules through text, empty coloured panels

Fix what you find with the fewest operations: move and resize frames, adjust type size, colour or alignment, re-crop or
swap a picture from the photo library, send a frame forward or back. Keep the page's design. Stay inside the page and,
for text, inside the live area, unless the frame already bleeds. You may reword short headings or captions to fit, using
the same facts; never touch article text (frames marked "articleText"), never add frames, never delete article text,
never invent anything. `measured` gives the true lines of each text frame; `problems` lists faults already found.
`continues` means the article carries on over the page, so text ending mid-sentence at the foot is correct.
Report each fault you see in `issues` ("major" if a reader would notice at a glance, otherwise "minor"), and set `clean`
true only if the page is ready to print as it stands. Leave `operations` empty if you can't improve the page."""

REVIEW_TOKENS = 32000  # reasoning models spend much of this thinking
BRIEFLY = " Be brief: think only as long as you must, and give at most 8 operations for the most visible faults."

SCHEMA = strict_object({
    "clean": {"type": "boolean"},
    "issues": {"type": "array", "items": strict_object({
        "severity": {"type": "string", "enum": ["major", "minor"]},
        "description": {"type": "string"},
    })},
    "operations": {"type": "array", "items": page_ai_edit.OPERATION},
})


@dataclass
class Problem:
    kind: str
    frame: str
    text: str
    serious: bool = True


@dataclass
class PageResult:
    page_id: str
    number: int
    elements: list[dict]
    fixed: int = 0
    remaining: list[str] = field(default_factory=list)
    changed: bool = False


def words(e: dict) -> int:
    return len((e.get("text") or "").split())


def is_article_text(e: dict) -> bool:
    return e.get("kind") == "text" and words(e) > BODY_WORDS


def live_area(profile: dict, number: int) -> tuple[float, float, float, float]:
    m = profile["margins"]
    W, H = profile["page"]["widthPt"], profile["page"]["heightPt"]
    left, right = (m["inside"], m["outside"]) if number % 2 else (m["outside"], m["inside"])
    return float(left), float(m["top"]), W - float(right), H - float(m["bottom"])


def _rect(e: dict) -> tuple[float, float, float, float]:
    return e["x"], e["y"], e["x"] + e["w"], e["y"] + e["h"]


def _area(a: tuple) -> float:
    return max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])


def _shared(a: tuple, b: tuple) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def find_problems(elements: list[dict], shot: Rendered, profile: dict, assets: dict[str, dict]) -> list[Problem]:
    """The faults that can be measured."""
    W, H = profile["page"]["widthPt"], profile["page"]["heightPt"]
    bleed = float(profile["page"].get("bleedPt") or 0)
    out: list[Problem] = []
    shown = [e for e in elements if not e.get("hidden")]
    texts = [e for e in shown if e.get("kind") == "text" and not e.get("rotation") and not e.get("dynamic")
             and (e.get("text") or "").strip()]
    inks = {e["id"]: tuple(shot.frames[e["id"]]["ink"]) for e in texts
            if (shot.frames.get(e["id"]) or {}).get("ink")}
    for e in texts:
        m = shot.frames.get(e["id"])
        ink = inks.get(e["id"])
        name = e.get("name") or e["id"]
        if m and not m.get("fits"):
            out.append(Problem("overset", e["id"], f"Text in “{name}” doesn't fit its frame and is cut off."))
        if m and e.get("styleRole") == "headline" and m.get("shrink", 1) < 0.5:
            out.append(Problem("tiny_headline", e["id"], f"Headline “{name}” had to shrink to {m['shrink']:.0%} to fit.", False))
        if ink and (ink[0] < -0.5 or ink[1] < -0.5 or ink[2] > W + 0.5 or ink[3] > H + 0.5):
            out.append(Problem("off_page", e["id"], f"Text in “{name}” runs off the page."))
        if TIMINGS.search(e.get("text") or ""):
            out.append(Problem("timestamp", e["id"], f"“{name}” mentions a video timestamp."))
        if PLACEHOLDER.fullmatch((e.get("text") or "").strip()):
            out.append(Problem("placeholder", e["id"], f"“{name}” still holds placeholder text."))
    for i, a in enumerate(texts):
        for b in texts[i + 1:]:
            ra, rb = inks.get(a["id"]), inks.get(b["id"])
            if ra and rb and _shared(ra, rb) > 0.06 * min(_area(ra), _area(rb)):
                out.append(Problem("text_overlap", a["id"], f"“{a.get('name')}” overlaps “{b.get('name')}”."))
    page_area = W * H
    for img in (e for e in shown if e.get("kind") == "image"):
        name = img.get("name") or img["id"]
        rect = _rect(img)
        cover = _area(rect) >= 0.45 * page_area or img.get("role") in ("background", "hero_image")
        if not img.get("assetId"):
            if img.get("role") != "ad":
                out.append(Problem("empty_image", img["id"], f"Picture frame “{name}” is empty."))
            continue
        if not cover:
            over = (rect[0] < -bleed - 1, rect[1] < -bleed - 1, rect[2] > W + bleed + 1, rect[3] > H + bleed + 1)
            near = (-bleed + 1 < rect[0] < -0.5, -bleed + 1 < rect[1] < -0.5, W + 0.5 < rect[2] < W + bleed - 1,
                    H + 0.5 < rect[3] < H + bleed - 1)
            if any(over) or any(near):
                out.append(Problem("image_off_page", img["id"], f"Picture “{name}” runs past the page edge without a "
                                                                "full bleed."))
            for t in texts:
                ink = inks.get(t["id"])
                if ink and t.get("z", 0) >= img.get("z", 0) and _shared(ink, rect) > 0.08 * _area(ink):
                    out.append(Problem("text_on_image", t["id"], f"“{t.get('name')}” runs over picture “{name}”."))
        asset = assets.get(img["assetId"]) or {}
        pw, ph = asset.get("widthPx") or 0, asset.get("heightPx") or 0
        if pw and ph and img["w"] > 0 and img["h"] > 0:
            frame, picture = img["w"] / img["h"], pw / ph
            lost = 1 - min(frame, picture) / max(frame, picture)
            if lost > CROP_LOSS:
                out.append(Problem("heavy_crop", img["id"], f"Picture “{name}” loses {lost:.0%} of its area to the "
                                                            "frame's shape.", False))
    return out


def _next_below(e: dict, elements: list[dict]) -> float | None:
    tops = [o["y"] for o in elements if o is not e and not o.get("hidden") and o.get("role") != "background"
            and o["y"] >= e["y"] + e["h"] - 2 and o["x"] < e["x"] + e["w"] and e["x"] < o["x"] + o["w"]]
    return min(tops) if tops else None


def auto_fix(elements: list[dict], problems: list[Problem], shot: Rendered, profile: dict, number: int) -> int:
    """Faults with one right answer, fixed directly: text that's too long gets room, or slightly smaller type."""
    by_id = {e["id"]: e for e in elements}
    x0, _y0, x1, y1 = live_area(profile, number)
    fixes = 0
    for p in problems:
        e = by_id.get(p.frame)
        if e is None or e.get("locked"):
            continue
        if p.kind == "off_page":
            ink = (shot.frames.get(e["id"]) or {}).get("ink")
            if ink and ink[2] > x1:
                e["w"] = round(max(40.0, x1 - e["x"]), 2)
                fixes += 1
            if ink and ink[0] < x0 and e["x"] < x0:
                e["w"] = round(e["w"] - (x0 - e["x"]), 2)
                e["x"] = x0
                fixes += 1
        elif p.kind == "timestamp":
            from app.services.ai_builder import untimed

            e["text"] = untimed(e["text"])
            fixes += 1
        elif p.kind == "overset":
            m = shot.frames.get(e["id"]) or {}
            need = float(m.get("scrollH") or 0) - float(m.get("clientH") or 0)
            room = (_next_below(e, elements) or y1) - (e["y"] + e["h"]) - 4
            if need > 0 and room >= min(need, 8):
                e["h"] = round(e["h"] + min(room, need + 2), 2)
                fixes += 1
                continue
            s = effective_style(profile, e)
            floor = MIN_SIZE.get(e.get("styleRole") or "body", 0.8)
            base = ((profile.get("typography") or {}).get(e.get("styleRole") or "body") or {}).get("size") or s.get("size")
            size = float(s.get("size") or 10)
            lead = float(s.get("leading") or size * 1.2)
            if e.get("paragraphGap", 0) > 3:
                e["paragraphGap"] = round(e["paragraphGap"] * 0.6, 2)
                fixes += 1
            elif size * 0.96 >= floor * float(base or size) * 0.8:
                e["style"] = {**(e.get("style") or {}), "size": round(size * 0.96, 2), "leading": round(lead * 0.97, 2)}
                fixes += 1
    return fixes


def apply_operations(elements: list[dict], ops: list[dict]) -> int:
    """The page editor's operations on frames (lib/designer/ai-edit.ts). Returns how many took effect."""
    by_id = {e["id"]: e for e in elements}
    done = 0
    for op in ops:
        e = by_id.get(op.get("elementId") or "")
        if e is None:
            continue
        if op["op"] == "delete":
            elements.remove(e)
            done += 1
            continue
        if op["op"] != "update":
            continue
        e.update(op.get("patch") or {})
        if op.get("style"):
            e["style"] = {**(e.get("style") or {}), **op["style"]}
        if op.get("arrange"):
            zs = [o.get("z", 0) for o in elements]
            e["z"] = max(zs) + 1 if op["arrange"] == "front" else min(zs) - 1
        done += 1
    return done


def safe_operations(ops: list[dict], elements: list[dict]) -> list[dict]:
    """Model operations narrowed to layout: article copy, and the page background, stay as they are."""
    by_id = {e["id"]: e for e in elements}
    out = []
    for op in ops:
        e = by_id.get(op.get("elementId") or "")
        if op.get("op") not in ("update", "delete") or e is None:
            continue
        if e.get("role") == "background" and e.get("kind") == "image":
            continue
        if is_article_text(e):
            patch = {k: v for k, v in (op.get("patch") or {}).items() if k not in ("text", "hidden")}
            if op["op"] == "delete" or (not patch and not op.get("style")):
                continue
            op = {**op, "patch": patch}
        out.append(op)
    return out


def _brief(elements: list[dict], page: dict, number: int, profile: dict, assets: list[dict], shot: Rendered,
           problems: list[Problem], continues: bool) -> str:
    context = page_ai_edit.build_prompt("", {**page, "elements": elements}, number, profile, assets, None)
    measured = {i: {"lines": m.get("lines"), "textBox": [round(v, 1) for v in m["ink"]] if m.get("ink") else None}
                for i, m in shot.frames.items()}
    notes = {"problems": [p.text for p in problems], "measured": measured, "continues": continues,
             "articleText": [e["id"] for e in elements if is_article_text(e)]}
    return f"{context}\n\nChecks (JSON):\n{json.dumps(notes, ensure_ascii=False)}"


def review_page(page: dict, number: int, continues: bool, llm, renderer: Renderer, profile: dict,
                assets: dict[str, dict]) -> PageResult:
    """Looks at a page, fixes it and looks again, until it's clean or the rounds run out."""
    elements = copy.deepcopy(page.get("elements") or [])
    result = PageResult(page["id"], number, elements)
    library = [a for a in assets.values() if "reference-import" not in (a.get("tags") or [])]
    ids = set(assets)
    best, best_serious = None, None
    seen: list[str] = []
    for round_no in range(MAX_ROUNDS):
        shot = renderer.render(elements, number, f"{page['id']}-{round_no}")
        problems = find_problems(elements, shot, profile, assets)
        serious = sum(p.serious for p in problems)
        if best is None or serious <= best_serious:
            best, best_serious = copy.deepcopy(elements), serious
        elif round_no:  # the last round made it worse: back to the best version, and finish there
            elements[:] = copy.deepcopy(best)
            result.fixed = max(0, result.fixed - 1)
            shot = renderer.render(elements, number, f"{page['id']}-back")
            problems = find_problems(elements, shot, profile, assets)
            result.remaining = [p.text for p in problems] + seen
            break
        fixed = auto_fix(elements, problems, shot, profile, number)
        if fixed:
            result.fixed += fixed
            if round_no < MAX_ROUNDS - 1:
                continue  # judged again on the corrected page before the model spends a look on it
        if not shot.png:
            result.remaining = [p.text for p in problems]
            break
        if round_no == MAX_ROUNDS - 1 and not problems:
            result.remaining = []
            break
        brief = _brief(elements, page, number, profile, library, shot, problems, continues)
        try:
            try:
                answer = _ask(llm, system=SYSTEM, schema=SCHEMA, schema_name="page_review", max_tokens=REVIEW_TOKENS,
                              images=[shot.png], prompt=brief)
            except LLMError as e:
                if "truncated" not in str(e):
                    raise
                answer = _ask(llm, system=SYSTEM + BRIEFLY, schema=SCHEMA, schema_name="page_review",
                              max_tokens=REVIEW_TOKENS, images=[shot.png], prompt=brief)
        except LLMError as e:
            log.warning("page %s: the reviewer failed: %s", number, e)
            result.remaining = [p.text for p in problems] + ["This page couldn't be fully checked: look it over."]
            break
        seen = [i["description"] for i in answer.get("issues") or [] if i.get("severity") == "major"]
        if answer.get("clean") and not any(p.serious for p in problems):
            result.remaining = []
            break
        ops, _notes = page_ai_edit.validate(answer.get("operations") or [], {"elements": elements}, profile, ids)
        ops = safe_operations(ops, elements)
        if not ops or round_no == MAX_ROUNDS - 1:
            result.remaining = [p.text for p in problems] + seen
            break
        result.fixed += apply_operations(elements, ops)
    result.changed = elements != page.get("elements")
    return result


def run_review(ctx: JobContext, project_id: str, start: float, end: float) -> str:
    """Checks every unlocked page. Returns a sentence for the job's message."""
    browser = find_browser()
    if not browser:
        return "Pages weren't checked: no Chrome or Edge on the server (set CHROME_PATH)."
    llm = get_vision_llm()
    if not (llm.supports_vision and llm.configured()[0]):
        return "Pages weren't checked: no vision model is configured."
    with session_factory()() as db:
        p = db.get(Project, project_id)
        if p is None:
            raise PermanentError("Project no longer exists")
        org_id = p.org_id
        profile, _ = profile_for_project(db, p)
        rows = list(db.scalars(select(MagazinePage).where(MagazinePage.project_id == project_id)
                               .order_by(MagazinePage.position)))
        # Frames as saved: the API shape fills in empty style keys, which would override the design profile.
        pages = [{**dump(ser.page(r)), "position": r.position, "elements": copy.deepcopy(r.elements)} for r in rows]
        assets = assets_by_id(db, project_id)
        articles = {a.id: dump(ser.article(a)) for a in db.scalars(select(Article).where(Article.project_id == project_id))}
    # Storage keys aren't part of the API's asset shape.
    with session_factory()() as db:
        from app.models import Asset

        keys = dict(db.execute(select(Asset.id, Asset.storage_key).where(Asset.project_id == project_id)).all())
    for i, a in assets.items():
        a["storageKey"] = keys.get(i)
    renderer = Renderer(project_id, org_id, assets, profile, browser, build_toc(pages, articles))
    todo = [(i, pg) for i, pg in enumerate(pages) if not pg.get("locked")]
    results: list[PageResult] = []
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            jobs = {}
            for i, pg in todo:
                nxt = pages[i + 1] if i + 1 < len(pages) else None
                continues = bool(nxt and pg.get("articleId") and nxt.get("articleId") == pg.get("articleId"))
                jobs[pool.submit(review_page, pg, i + 1, continues, llm, renderer, profile, assets)] = pg
            for k, job in enumerate(as_completed(jobs)):
                ctx.progress(start + (end - start) * (k + 1) / max(1, len(jobs)),
                             f"Checked {k + 1} of {len(jobs)} pages")
                try:
                    results.append(job.result())
                except Exception as e:  # noqa: BLE001 - one page's failure doesn't lose the others
                    log.warning("checking a page failed: %s", e)
    finally:
        renderer.close()
    with session_factory()() as db:
        for r in results:
            row = db.get(MagazinePage, r.page_id)
            if row is None or row.locked:
                continue
            if r.changed:
                row.elements = r.elements
            kept = "\n".join(ln for ln in (row.notes or "").split("\n") if not ln.startswith("AI check:"))
            remark = "\n".join(f"AI check: {t}" for t in dict.fromkeys(r.remaining))
            row.notes = "\n".join(x for x in (kept, remark) if x) or None
        db.commit()
    fixed = sum(r.fixed for r in results)
    open_pages = sorted(r.number for r in results if r.remaining)
    message = f"Checked {len(results)} pages and made {fixed} fix{'' if fixed == 1 else 'es'}."
    if open_pages:
        message += f" Worth a look: page{'' if len(open_pages) == 1 else 's'} {', '.join(map(str, open_pages))}."
    return message


@register("ai_review")
def run_ai_review(ctx: JobContext) -> None:
    project_id = ctx.payload["projectId"]
    ctx.progress(2, "Checking every page")
    message = run_review(ctx, project_id, 5, 99)
    ctx.progress(100, message)
    ctx.set_result(project_id)

