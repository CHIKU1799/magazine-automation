"""Import a reference magazine as an editable copy: a new project whose pages reproduce the reference PDF.

The `reference_import` job reads the fonts once (webfonts.py), then builds every page (pages.py): live text as text
frames in the PDF's own fonts, photographs as image frames, the rest as a background picture. Pages with no live
text (outlined or flattened type) are read by the vision model and their text rebuilt as live frames wherever it
can be cleared from the background without a trace.
"""

from __future__ import annotations

import hashlib
import json
import logging

import pymupdf
from sqlalchemy import delete, select

from app.db import session_factory
from app.ids import new_id
from app.models import Asset, DesignProfile, MagazinePage, Project, ReferenceMagazine, ReferencePage
from app.schemas.base import PAGE_TYPES
from app.services.jobs.queue import JobContext, PermanentError, register
from app.services.llm import get_vision_llm
from app.services.reference_import.pages import ImageCache, build_flat_page, build_live_page, has_live_text
from app.services.reference_import.webfonts import FaceInfo, GoogleFonts, collect_faces, google_css_import
from app.services.storage import get_storage
from app.services.templates import ref_template_id

log = logging.getLogger("pressflow.reference_import")

KEEP_AS_PICTURE = ("advertisement", "full_page_image")  # artwork supplied whole: not rebuilt from vision


def fonts_prefix(org_id: str, project_id: str) -> str:
    return f"fonts/{org_id}/{project_id}"


def fonts_css(manifest: dict, project_id: str) -> str:
    """@font-face rules for a project's imported fonts (Google families come first through @import)."""
    out = []
    if manifest.get("googleCss"):
        out.append(f'@import url("{manifest["googleCss"]}");')
    for f in manifest.get("faces") or []:
        style = "italic" if f["italic"] else "normal"
        out.append(
            "@font-face {"
            f' font-family: "{f["family"]}"; font-weight: {f["weight"]}; font-style: {style};'
            f' src: url("/api/v1/projects/{project_id}/fonts/{f["id"]}.woff") format("woff");'
            f' unicode-range: {f["unicodeRange"]}; font-display: block; }}'
        )
    return "\n".join(out) + "\n"


def _store_fonts(org_id: str, project_id: str, faces: dict[str, FaceInfo]) -> dict:
    storage = get_storage()
    prefix = fonts_prefix(org_id, project_id)
    unique = list({id(f): f for f in faces.values()}.values())
    manifest = {"googleCss": google_css_import(unique), "faces": []}
    for face in unique:
        for fl in face.files:
            storage.put_bytes(f"{prefix}/{fl['id']}.woff", fl["bytes"], "font/woff")
            manifest["faces"].append({"id": fl["id"], "family": face.family, "weight": face.weight,
                                      "italic": face.italic, "unicodeRange": fl["unicodeRange"]})
    storage.put_bytes(f"{prefix}/manifest.json", json.dumps(manifest).encode(), "application/json")
    return manifest


def _profile(db, ref: ReferenceMagazine) -> dict:
    from app.services import serializers as ser
    from app.services.project_context import dump
    from app.services.house_style import house_profile

    dp = db.get(DesignProfile, ref.design_profile_id) if ref.design_profile_id else None
    return dump(ser.design_profile(dp)) if dp else house_profile()


def _vision_blocks(pdf: bytes, row: ReferencePage | None, page_number: int, height_pt: float) -> list[dict]:
    """Located text and logos: the ones saved by Re-analyse page when present, otherwise asked for now."""
    from app.services.llm import LLMError
    from app.services.reference_jobs import _vision_cover_blocks

    saved = [b for b in (row.blocks if row else []) or [] if b.get("source") == "vision"]
    if saved:
        return saved
    try:
        return _vision_cover_blocks(pdf, {"pageNumber": page_number, "heightPt": height_pt})
    except LLMError as e:
        log.warning("vision layout detection failed on page %s: %s", page_number, e)
        return []


def _on_failure(ctx: JobContext, message: str) -> None:
    """A copy that never got its pages leaves no empty project behind (its stored fonts and assets go with it)."""
    project_id = ctx.payload.get("projectId")
    with session_factory()() as db:
        p = db.get(Project, project_id) if project_id else None
        if p is None or db.scalar(select(MagazinePage.id).where(MagazinePage.project_id == p.id).limit(1)):
            return
        keys = [a.storage_key for a in db.scalars(select(Asset).where(Asset.project_id == p.id)) if a.storage_key]
        db.execute(delete(Asset).where(Asset.project_id == p.id))
        db.delete(p)
        db.commit()
    storage = get_storage()
    for key in keys + [f"{fonts_prefix(p.org_id, p.id)}/manifest.json"]:
        try:
            storage.delete(key)
        except Exception:  # noqa: BLE001 - leftovers in storage are harmless; the project row is what users see
            pass
    log.warning("editable copy %s failed and was removed: %s", project_id, message)


@register("reference_import", on_final_failure=_on_failure)
def run_reference_import(ctx: JobContext) -> None:
    ref_id, project_id = ctx.payload["referenceId"], ctx.payload["projectId"]
    with session_factory()() as db:
        ref = db.get(ReferenceMagazine, ref_id)
        project = db.get(Project, project_id)
        if ref is None or project is None:
            raise PermanentError("The reference or the project no longer exists")
        if not ref.storage_key:
            raise PermanentError("Reference PDF is not stored")
        org_id, key = ref.org_id, ref.storage_key
        profile = _profile(db, ref)
        rows = {r.page_number: r for r in db.scalars(select(ReferencePage).where(ReferencePage.reference_id == ref_id))}
        for r in rows.values():
            db.expunge(r)
    storage = get_storage()
    pdf = storage.get_bytes(key)
    src = pymupdf.open(stream=pdf, filetype="pdf")
    try:
        ctx.progress(2, "Reading fonts")
        google = GoogleFonts()
        try:
            faces = collect_faces(src, google, lambda f, msg: ctx.progress(2 + 8 * f, msg))
        finally:
            google.close()
        _store_fonts(org_id, project_id, faces)
        cache = ImageCache()
        llm = get_vision_llm()
        vision = llm.supports_vision and llm.configured()[0]
        n = src.page_count
        with session_factory()() as db:
            def save(data: bytes, ctype: str, w: int, h: int, kind: str, name: str) -> str:
                aid = new_id("ast")
                ext = {"image/png": ".png", "image/jpeg": ".jpg"}[ctype]
                skey = f"assets/{org_id}/{project_id}/{aid}{ext}"
                storage.put_bytes(skey, data, ctype)
                db.add(Asset(id=aid, org_id=org_id, project_id=project_id, kind=kind, file_name=f"{name}{ext}"[:400],
                             content_type=ctype, size_bytes=len(data), width_px=w, height_px=h,
                             tags=["reference-import"], caption=name, credit=f"From {ref.title}",
                             seed=int(hashlib.sha256(aid.encode()).hexdigest()[:8], 16), storage_key=skey,
                             simulated=False))
                return aid

            for i in range(n):
                number = i + 1
                ctx.progress(10 + 88 * i / n, f"Rebuilding page {number} of {n}")
                row = rows.get(number)
                layout = row.layout_type if row and row.layout_type in PAGE_TYPES else "two_column_article"
                work = pymupdf.open(stream=pdf, filetype="pdf")  # backgrounds remove shared images: a fresh copy each page
                try:
                    elements = None
                    if vision and not has_live_text(src[i]) and layout not in KEEP_AS_PICTURE:
                        elements = build_flat_page(
                            src, work, i, faces, profile, save, cache, llm,
                            lambda: _vision_blocks(pdf, row, number, src[i].rect.height))
                    if elements is None:
                        elements = build_live_page(src, work, i, faces, profile, save, cache)
                finally:
                    work.close()
                db.add(MagazinePage(id=new_id("pg"), project_id=project_id, position=i, page_type=layout,
                                    template_id=ref_template_id(ref_id, number), label=f"Page {number}",
                                    elements=elements, editorial_status="pending", design_status="in_progress",
                                    locked=False))
                db.commit()
            p = db.get(Project, project_id)
            p.status = "design"
            p.target_page_count = n
            db.commit()
    finally:
        src.close()
    ctx.progress(100, f"Imported {n} pages")
    ctx.set_result(project_id)
