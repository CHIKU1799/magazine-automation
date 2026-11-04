"""AI Builder: one click from processed videos to a finished magazine.

The `ai_build` job writes the issue's articles from the videos' transcripts (unless the project already has them),
composes a cover and a contents page from the design profile, then sets the articles into pages copied from the
reference magazine (reference_fill). With no reference, the planner picks built-in templates for each article instead
and the house style applies. Everything it makes is an ordinary, editable page; articles stay drafts marked as
AI-written until someone approves them.
"""

from __future__ import annotations

import hashlib
import logging
import re

from sqlalchemy import delete, select

from app.config import get_settings
from app.db import session_factory
from app.ids import iso, new_id, utcnow
from app.models import Article, Asset, EventSession, ExtractedFact, MagazinePage, Project, Speaker, TranscriptSegment
from app.schemas.pages import MagazinePage as PageSchema
from app.services import editorial, images, planner
from app.services import serializers as ser
from app.services.claim_verification import verify_article
from app.services.jobs.queue import JobContext, PermanentError, register
from app.services.llm import LLMError
from app.services.page_review import run_review
from app.services.project_context import assets_by_id, dump, pages_dicts, profile_for_project
from app.services.reference_fill import (
    AI_IMAGE_LIMIT,
    COVER_TAG,
    _ask,
    _fill_contents,
    is_reference_copy,
    run_reference_fill,
    writer,
)
from app.services.storage import get_storage
from app.services.template_store import ensure_reference_logos, reference_templates

log = logging.getLogger("pressflow.ai_builder")

# A session's kind -> the article it becomes.
SESSION_ARTICLE = {"panel": "panel", "interview": "interview", "fireside": "interview", "keynote": "keynote",
                   "presentation": "keynote"}


def article_plan(sessions: list[EventSession], speakers: int) -> list[tuple[str, list[str]]]:
    """(article type, session ids) for the issue: an editor's letter, one story per session and, for several
    sessions, an overall report; a single talk gets a full report and a highlights round-up instead."""
    if len(sessions) <= 1:
        ids = [s.id for s in sessions]
        plan = [("editorial", ids), ("event_report", ids), ("highlights", ids)]
        if speakers >= 2:
            plan.append(("speaker_profiles", ids))
        return plan
    plan = [("editorial", [s.id for s in sessions])]
    plan += [(SESSION_ARTICLE.get(s.kind, "event_report"), [s.id]) for s in sessions[:8]]
    plan.append(("event_report", [s.id for s in sessions]))
    return plan


def _write_articles(ctx: JobContext, project_id: str, llm, start: float, end: float) -> list[str]:
    """Articles from the transcripts. Facts not yet reviewed are used too (rejected ones never are): the builder
    is a draft, and every article it writes is checked against the transcript and left for approval."""
    s = get_settings()
    with session_factory()() as db:
        project = db.get(Project, project_id)
        sessions = list(db.scalars(select(EventSession).where(EventSession.project_id == project_id)
                                   .order_by(EventSession.start_sec)))
        n_speakers = len(list(db.scalars(select(Speaker.id).where(Speaker.project_id == project_id))))
        cfg = project.config or {}
        plan = article_plan(sessions, n_speakers)
        written: list[str] = []
        for i, (art_type, session_ids) in enumerate(plan):
            ctx.progress(start + (end - start) * i / len(plan),
                         f"Writing the {art_type.replace('_', ' ')} ({i + 1} of {len(plan)})")
            context = editorial.gather_context(db, project, session_ids, s.llm_analysis_window_chars)
            context["facts"] = [f for f in db.scalars(select(ExtractedFact).where(
                ExtractedFact.project_id == project_id, ExtractedFact.status != "rejected"))
                if not session_ids or f.video_id in {x.video_id for x in context["sessions"]}]
            if not context["segments"]:
                continue
            prompt, ref_to_seg = editorial.build_prompt(project, art_type, context,
                                                        cfg.get("editorialTone", "authoritative"),
                                                        cfg.get("contentStyle", "feature"), cfg.get("language", "en"))
            prompt += "\nThis is finished magazine copy: never mention timestamps, seconds, segments or the transcript."
            try:
                raw = _ask(llm, system=editorial.SYSTEM, prompt=prompt, schema=editorial.ARTICLE_SCHEMA,
                           schema_name="article")
            except LLMError as e:
                if written:
                    log.warning("stopped writing articles at %s: %s", art_type, e)
                    break
                raise PermanentError(f"The AI couldn't write the articles: {e}") from e
            out = editorial.postprocess(raw, context, ref_to_seg)
            art = Article(id=new_id("art"), project_id=project_id, type=art_type, kicker=out["kicker"],
                          headline=out["headline"], subheadline=out["subheadline"], intro=out["intro"],
                          body=out["body"], quote_fact_ids=out["quoteFactIds"], speaker_ids=out["speakerIds"],
                          session_ids=[x.id for x in context["sessions"]], suggested_image_ids=[], captions={},
                          status="draft", generated_by="llm",
                          variants={"headlines": out["headlines"]} if out["headlines"] else None)
            claims = verify_article(editorial._article_dict(art), editorial._segs(context["segments"]))
            bad = [c for c in claims if c.status != "verified"]
            art.last_verification = {"checkedAt": iso(utcnow()), "unverified": len(bad), "total": len(claims)}
            art.review_note = "Written by the AI Builder from the transcripts." + (
                f" {len(bad)} of {len(claims)} quotes/figures could not be fully verified: run Verify before "
                f"approving." if bad else "")
            db.add(art)
            db.commit()  # one writer on SQLite: commit before the next progress update
            written.append(art.id)
        if project.status in ("setup", "ingesting", "analysing"):
            project.status = "editorial"
            db.commit()
    return written


def _cover_and_contents(project_id: str, org_id: str) -> None:
    """A cover and a contents page from the design profile, when the project has none (its own are kept)."""
    with session_factory()() as db:
        p = db.get(Project, project_id)
        existing = pages_dicts(db, project_id)
        assets = assets_by_id(db, p.id)
        imported = {i for i, a in assets.items() if "reference-import" in (a.get("tags") or [])}
        # A cover or contents copied from the reference, or built on a picture cut from it, is that issue's, not
        # this project's: it's made anew.
        have = {pg["pageType"] for pg in existing if not is_reference_copy(pg.get("elements"), imported)}
        if {"cover", "toc"} <= have:
            return
        profile, _ = profile_for_project(db, p)
        project = dump(ser.project(p))
        project["title"] = clean_title(project.get("title") or "")
        articles = [dump(ser.article(a)) for a in db.scalars(select(Article).where(Article.project_id == p.id))]
        speakers = [dump(ser.speaker(s)) for s in db.scalars(select(Speaker).where(Speaker.project_id == p.id))]
        facts = [dump(ser.fact(f)) for f in db.scalars(select(ExtractedFact).where(ExtractedFact.project_id == p.id))]
        refs = reference_templates(db, org_id)
        cover_tid = planner.cover_template(project, existing, refs)
        if cover_tid in refs:
            ensure_reference_logos(db, org_id, p.id, refs[cover_tid])
            db.flush()
        # The pages cut from the reference aren't photographs of this event.
        photos = [a for i, a in assets.items() if i not in imported]
        pages, _ = planner.recommend(project, project["config"], articles, photos, profile, [], speakers=speakers,
                                     facts=facts, reference_templates=refs)
        front = [pg for pg in pages if pg["pageType"] in ("cover", "toc") and pg["pageType"] not in have]
        art = next((i for i, a in assets.items() if COVER_TAG in (a.get("tags") or [])), None)
        for pg in front:
            frames = [e for e in pg.get("elements") or [] if e.get("kind") == "image"]
            if art and pg["pageType"] == "cover" and frames:  # the picture made for this cover goes on it
                max(frames, key=lambda e: e["w"] * e["h"])["assetId"] = art
        for pos, pg in enumerate(front):
            v = PageSchema.model_validate(_in_reference_type(_tidied(pg), profile))
            db.add(MagazinePage(id=new_id("pg"), project_id=p.id, position=-len(front) + pos, page_type=v.page_type,
                                template_id=v.template_id, label=v.label,
                                elements=[e.model_dump(by_alias=True, exclude_none=True) for e in v.elements],
                                editorial_status="pending", design_status="in_progress", locked=False))
        db.commit()


def untimed(text: str) -> str:
    """Video timestamps out of finished copy ("articulated at 103 seconds", "his remarks at 67-78 seconds")."""
    span = r"\d+(?:\s*[-‐-–]\s*\d+)?\s*seconds?\b"  # models also write non-breaking and en dashes
    text = re.sub(rf"(?i),?\s*\b(?:articulated|delivered|stated|said|mentioned|made)\s+(?:at|around)\s+{span},?", "", text)
    return re.sub(rf"(?i)\s+(?:at|around)\s+{span}", "", text)


def clean_title(title: str) -> str:
    """A magazine's name from a project title that came from a file: 'Tech Maha Impact-Print file (25 MAY) (1)
    (editable)' -> 'Tech Maha Impact'."""
    name = re.sub(r"\([^)]*\)", " ", title)
    name = re.sub(r"(?i)[\s_-]*\b(print[\s_-]*file|print[\s_-]*ready|final|revised|draft|copy|v\d+)\b.*$", "", name)
    name = re.sub(r"[\s_-]+$", "", re.sub(r"\s+", " ", name)).strip()
    return name or title


def _cover_art(project_id: str, org_id: str, ctx: JobContext) -> None:
    """A cover needs a picture: an AI image (labelled) when the project has no photograph of its own."""
    with session_factory()() as db:
        p = db.get(Project, project_id)
        assets = list(db.scalars(select(Asset).where(Asset.project_id == project_id)))
        # A real photograph, or cover art already made; AI images made for stories don't stand in for a cover.
        if any(a.kind in ("photo", "video_frame") and a.storage_key and "reference-import" not in (a.tags or [])
               and (images.AI_TAG not in (a.tags or []) or COVER_TAG in (a.tags or [])) for a in assets):
            return
        articles = list(db.scalars(select(Article).where(Article.project_id == project_id,
                                                         Article.status != "rejected").limit(4)))
        themes = "; ".join(a.headline for a in articles)
        event = p.event_name or clean_title(p.title)
    ctx.progress(57, "Creating the cover image")
    prompt = (f"Striking full-page magazine cover photograph for an issue about {event}. Stories: {themes}. "
              "Leave calm, uncluttered space in the upper third for the masthead and the lower third for headlines.")
    try:
        png, w, h = images.generate(prompt, "portrait")
    except images.ImageGenError as e:
        log.warning("no AI cover image: %s", e)
        return
    aid = new_id("ast")
    key = f"assets/{org_id}/{project_id}/{aid}.png"
    get_storage().put_bytes(key, png, "image/png")
    with session_factory()() as db:
        db.add(Asset(id=aid, org_id=org_id, project_id=project_id, kind="photo", file_name=f"ai-cover-{aid[-8:]}.png",
                     content_type="image/png", size_bytes=len(png), width_px=w, height_px=h,
                     tags=[images.AI_TAG, COVER_TAG], caption="Cover image",
                     credit=f"AI-generated image ({get_settings().image_model})",
                     seed=int(hashlib.sha256(aid.encode()).hexdigest()[:8], 16), storage_key=key, simulated=False))
        db.commit()


def _tidied(page: dict) -> dict:
    """Template leftovers on a composed page: a picture's working caption, an empty credit, a rule through a heading."""
    els = page.get("elements") or []
    keep = []
    for e in els:
        text = e.get("text") or ""
        if e.get("role") == "caption" and text.startswith("AI image for"):
            continue
        if text:
            e["text"] = re.sub(r"\n{3,}", "\n\n", re.sub(r"(?m)^Published by\s*$", "", text)).strip()
        if e.get("kind") == "rule" and e["h"] <= 3 and any(
                o.get("kind") == "text" and o is not e and (o.get("text") or "") and o["y"] < e["y"] < o["y"] + o["h"]
                and o["x"] < e["x"] + e["w"] and e["x"] < o["x"] + o["w"] for o in els):
            continue
        keep.append(e)
    page["elements"] = keep
    return page


def _in_reference_type(page: dict, profile: dict) -> dict:
    """Built-in cover and contents frames set in the reference's own typefaces (as the design profile measured
    them), so they match the pages copied from it; the project's font stylesheet serves those faces."""
    from app.services.reference_import.webfonts import family_name, style_name, weight_of

    typo = profile.get("typography") or {}
    for el in page.get("elements") or []:
        detected = (typo.get(el.get("styleRole") or "body") or {}).get("detectedFont")
        if el.get("kind") != "text" or not detected:
            continue
        style = el.setdefault("style", {})
        style.setdefault("fontFamily", family_name(detected))
        style.setdefault("weight", weight_of(detected))
        if style_name(detected):
            style.setdefault("fontStyle", style_name(detected))
    return page


def _illustrate(pages: list[dict], articles: dict[str, dict], project: dict, project_id: str, org_id: str,
                ctx: JobContext, start: float, end: float) -> int:
    """AI images (labelled) for the empty picture frames, largest first. Returns the frames left empty."""
    frames = [(e, pg) for pg in pages for e in pg.get("elements") or []
              if e.get("kind") == "image" and not e.get("assetId") and e.get("role") != "ad"]
    frames.sort(key=lambda fp: -fp[0]["w"] * fp[0]["h"])
    made: list[Asset] = []
    settings = get_settings()
    left = len(frames)
    for i, (e, pg) in enumerate(frames[:AI_IMAGE_LIMIT]):
        article = articles.get(pg.get("articleId") or "") or {}
        ctx.progress(start + (end - start) * i / max(1, len(frames)), f"Creating AI image {i + 1} of "
                     f"{min(len(frames), AI_IMAGE_LIMIT)}")
        aspect = "portrait" if e["h"] > 1.15 * e["w"] else "landscape" if e["w"] > 1.15 * e["h"] else "square"
        topic = article.get("headline") or project.get("eventName") or project.get("title")
        prompt = (f"Photograph for a magazine article titled \"{topic}\". {article.get('subheadline') or ''} "
                  f"Setting: {project.get('eventName') or 'a conference'}.")
        try:
            png, w, h = images.generate(prompt, aspect)
        except images.ImageGenError as err:
            log.warning("AI image failed: %s", err)
            break  # a refusal or outage: the rest stay empty rather than retry each frame
        aid = new_id("ast")
        key = f"assets/{org_id}/{project_id}/{aid}.png"
        get_storage().put_bytes(key, png, "image/png")
        made.append(Asset(id=aid, org_id=org_id, project_id=project_id, kind="photo", file_name=f"ai-{aid[-8:]}.png",
                          content_type="image/png", size_bytes=len(png), width_px=w, height_px=h, tags=[images.AI_TAG],
                          caption=topic, credit=f"AI-generated image ({settings.image_model})", storage_key=key,
                          seed=int(hashlib.sha256(aid.encode()).hexdigest()[:8], 16), simulated=False))
        e.update(assetId=aid, crop={"scale": 1, "offsetX": 0, "offsetY": 0})
        left -= 1
    with session_factory()() as db:
        db.add_all(made)
        db.commit()
    return left


def _compose_own(ctx: JobContext, project_id: str, org_id: str, article_ids: list[str] | None, ai_images: bool) -> str:
    """No reference: the planner picks built-in templates for each article, in the house style, with exactly as many
    pages as the content needs. Replaces the project's unlocked pages. Returns a note about the result."""
    with session_factory()() as db:
        p = db.get(Project, project_id)
        profile, _ = profile_for_project(db, p)
        project = dump(ser.project(p))
        project["title"] = clean_title(project.get("title") or "")
        rows = {a.id: a for a in db.scalars(select(Article).where(Article.project_id == p.id))}
        chosen = [rows[i] for i in article_ids] if article_ids else [a for a in rows.values() if a.status != "rejected"]
        articles = [dump(ser.article(a)) for a in chosen]
        speakers = [dump(ser.speaker(s)) for s in db.scalars(select(Speaker).where(Speaker.project_id == p.id))]
        facts = [dump(ser.fact(f)) for f in db.scalars(select(ExtractedFact).where(ExtractedFact.project_id == p.id))]
        assets = assets_by_id(db, p.id)
        photos = [a for a in assets.values() if not {"reference-import", COVER_TAG} & set(a.get("tags") or [])]
        config = {**project["config"], "includeAdvertisements": False, "adPageCount": 0}
        pages, _ = planner.recommend(project, config, articles, photos, profile, [], speakers=speakers, facts=facts,
                                     reference_templates={}, pad=False)
    # A gallery is the event's own photographs: AI images don't make one.
    real = [a for a in photos if images.AI_TAG not in (a.get("tags") or [])]
    if len(real) < 4:
        pages = [pg for pg in pages if pg["pageType"] != "photo_gallery"]
    art = next((i for i, a in assets.items() if COVER_TAG in (a.get("tags") or [])), None)
    n_articles = sum(1 for pg in pages if pg.get("articleId") and not pg.get("articlePart"))
    for pg in pages:
        _tidied(pg)
        for e in pg["elements"]:
            if e.get("text"):
                e["text"] = untimed(e["text"])
        if pg["pageType"] == "toc":
            _fill_contents(pg["elements"], n_articles)
        frames = [e for e in pg["elements"] if e.get("kind") == "image"]
        if art and pg["pageType"] == "cover" and frames:
            max(frames, key=lambda e: e["w"] * e["h"])["assetId"] = art
    empty = _illustrate(pages, {a["id"]: a for a in articles}, project, project_id, org_id, ctx, 60, 86) \
        if ai_images and images.configured()[0] else \
        sum(1 for pg in pages for e in pg["elements"] if e.get("kind") == "image" and not e.get("assetId"))
    with session_factory()() as db:
        locked = list(db.scalars(select(MagazinePage).where(MagazinePage.project_id == project_id,
                                                            MagazinePage.locked.is_(True))
                                 .order_by(MagazinePage.position)))
        db.execute(delete(MagazinePage).where(MagazinePage.project_id == project_id, MagazinePage.locked.is_(False)))
        for pos, pg in enumerate(pages):
            v = PageSchema.model_validate(pg)
            db.add(MagazinePage(id=new_id("pg"), project_id=project_id, position=pos, page_type=v.page_type,
                                template_id=v.template_id, label=v.label, article_id=v.article_id,
                                article_part=v.article_part,
                                elements=[e.model_dump(by_alias=True, exclude_none=True) for e in v.elements],
                                editorial_status="pending", design_status="in_progress", locked=False))
        for i, row in enumerate(locked):
            row.position = len(pages) + i
        db.get(Project, project_id).status = "design"
        db.commit()
    return f"{empty} photo frame(s) are empty for you to fill." if empty else ""


class _Stage:
    """The fill job's context, reporting into a slice of the builder's progress bar."""

    def __init__(self, ctx: JobContext, payload: dict, start: float, end: float):
        self.ctx, self.payload, self.start, self.end, self.last = ctx, payload, start, end, ""

    def progress(self, pct: float, message: str | None = None) -> None:
        self.last = message or self.last
        self.ctx.progress(self.start + (self.end - self.start) * pct / 100, message)

    def set_result(self, ref: str) -> None:
        pass


@register("ai_build")
def run_ai_build(ctx: JobContext) -> None:
    project_id = ctx.payload["projectId"]
    with session_factory()() as db:
        p = db.get(Project, project_id)
        if p is None:
            raise PermanentError("Project no longer exists")
        org_id = p.org_id
        if not db.scalar(select(TranscriptSegment.id).where(TranscriptSegment.project_id == project_id).limit(1)):
            raise PermanentError("No transcripts yet: process at least one video in the Event Media Library")
        have_articles = db.scalar(select(Article.id).where(Article.project_id == project_id,
                                                           Article.status != "rejected").limit(1)) is not None
    llm = writer()
    if not llm.configured()[0]:
        raise PermanentError("The AI Builder needs an AI provider (LLM_PROVIDER)")
    ctx.progress(2, "Reading the transcripts")
    article_ids = None  # the project's own articles, when it has them and no rewrite was asked for
    if ctx.payload.get("rewrite") or not have_articles:
        article_ids = _write_articles(ctx, project_id, llm, 3, 55)
        if not article_ids:
            raise PermanentError("The transcripts gave the AI nothing to write from")
    ctx.progress(56, "Composing the cover and contents")
    ai_images = ctx.payload.get("aiImages", True)
    if ai_images and images.configured()[0]:
        _cover_art(project_id, org_id, ctx)
    stage = None
    if ctx.payload.get("noReference"):
        ctx.progress(58, "Choosing templates for each article")
        own_note = _compose_own(ctx, project_id, org_id, article_ids, ai_images)
    else:
        own_note = ""
        _cover_and_contents(project_id, org_id)
        stage = _Stage(ctx, {"projectId": project_id, "referenceId": ctx.payload.get("referenceId"),
                             "articleIds": article_ids, "aiImages": ai_images, "styleFront": True}, 58, 86)
        run_reference_fill(stage)
    with session_factory()() as db:
        pages = db.scalar(select(MagazinePage.id).where(MagazinePage.project_id == project_id).limit(1))
        n_pages = len(list(db.scalars(select(MagazinePage.id).where(MagazinePage.project_id == project_id))))
        n_art = len(list(db.scalars(select(Article.id).where(Article.project_id == project_id,
                                                             Article.status != "rejected"))))
    if not pages:
        raise PermanentError("No pages were made")
    # What the layout had to say about the result (empty photo frames, text left out) stays in the final message.
    notes = own_note or (stage.last.split(". ", 1)[1] if stage and ". " in stage.last else "")
    ctx.progress(87, "Checking every page and fixing what's broken")
    try:
        check = run_review(ctx, project_id, 88, 99)
    except Exception as e:  # noqa: BLE001 - the magazine is built; a failed check mustn't lose it
        log.exception("the page check failed")
        check = f"Pages weren't checked: {e}"
    ctx.progress(100, f"Your magazine is ready: {n_pages} pages, {n_art} articles. {check} {notes}".strip())
    ctx.set_result(project_id)
