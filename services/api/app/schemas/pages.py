from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from app.schemas.base import ID, CamelModel, ISODate, PageType, Severity, SlotRole, TextRole

ElementKind = Literal["text", "image", "shape", "rule"]


class ImageCrop(CamelModel):
    scale: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0


class ElementStyle(CamelModel):
    model_config = ConfigDict(extra="allow")  # frontend-only style keys round-trip untouched

    size: float | None = None
    leading: float | None = None
    weight: int | None = None
    tracking: float | None = None
    color: str | None = None
    uppercase: bool | None = None
    italic: bool | None = None
    preview_font: str | None = None
    align: Literal["left", "center", "right", "justify"] | None = None


class PageElement(CamelModel):
    model_config = ConfigDict(extra="allow")  # unknown frontend fields round-trip untouched

    id: ID
    kind: ElementKind
    role: SlotRole
    name: str
    x: float
    y: float
    w: float
    h: float
    z: int
    locked: bool | None = None
    hidden: bool | None = None
    # Text regenerated from the flatplan (page numbers, contents list) rather than edited by hand.
    dynamic: Literal["folio", "toc"] | None = None
    text: str | None = None
    style_role: TextRole | None = None
    style: ElementStyle | None = None
    columns: int | None = None
    gutter: float | None = None
    asset_id: ID | None = None
    crop: ImageCrop | None = None
    fill: str | None = None
    stroke: str | None = None
    stroke_width: float | None = None
    radius: float | None = None
    opacity: float | None = None


EditorialStatus = Literal["pending", "approved"]
DesignStatus = Literal["not_started", "in_progress", "ready", "approved"]


class MagazinePage(CamelModel):
    id: ID
    project_id: ID
    page_type: PageType
    template_id: ID
    label: str
    article_id: ID | None = None
    article_part: int | None = None
    elements: list[PageElement]
    editorial_status: EditorialStatus
    design_status: DesignStatus
    locked: bool
    notes: str | None = None


class MagazinePageIn(CamelModel):
    """Input shape for PUT /projects/{id}/pages (id/projectId optional)."""

    id: ID | None = None
    project_id: ID | None = None
    page_type: PageType
    template_id: ID
    label: str = ""
    article_id: ID | None = None
    article_part: int | None = None
    elements: list[PageElement] = Field(default_factory=list)
    editorial_status: EditorialStatus = "pending"
    design_status: DesignStatus = "not_started"
    locked: bool = False
    notes: str | None = None


class PagesPutRequest(CamelModel):
    pages: list[MagazinePageIn]


class PagesPutResponse(CamelModel):
    pages: list[MagazinePage]
    preserved_locked: list[ID] = Field(default_factory=list)


class PageAiEditRequest(CamelModel):
    """The page as the editor holds it (unsaved edits included) and the designer's instruction."""

    page: MagazinePageIn
    page_number: int = Field(default=1, ge=1)
    instruction: str = Field(min_length=2, max_length=2000)
    selected_id: ID | None = None


class PageAiEditResponse(CamelModel):
    summary: str
    # Validated editor operations: {op: update|add|delete, elementId?, kind?, patch, style?, arrange?}.
    operations: list[dict]
    notes: list[str] = Field(default_factory=list)
    provider: str
    model: str


class PlanRecommendRequest(CamelModel):
    target_page_count: int | None = Field(default=None, ge=2, le=500)


class PlanRecommendResponse(CamelModel):
    engine: Literal["heuristic"] = "heuristic"
    pages: list[MagazinePage]
    rationale: list[str]


# ------------------------------------------------------------------ assets


AssetKind = Literal["photo", "portrait", "ad", "logo", "video_frame"]


class Asset(CamelModel):
    id: ID
    project_id: ID
    kind: AssetKind
    file_name: str
    width_px: int
    height_px: int
    session_id: ID | None = None
    video_id: ID | None = None
    speaker_id: ID | None = None
    tags: list[str]
    caption: str | None = None
    credit: str | None = None
    seed: int
    url: str | None = None
    simulated: bool


class GenerateImageRequest(CamelModel):
    prompt: str = Field(min_length=3, max_length=4000)
    aspect: Literal["portrait", "landscape", "square"] = "landscape"
    caption: str | None = None
    session_id: ID | None = None


class AssetUpdate(CamelModel):
    kind: AssetKind | None = None
    session_id: ID | None = None
    speaker_id: ID | None = None
    tags: list[str] | None = None
    caption: str | None = None
    credit: str | None = None


# ------------------------------------------------------------------ preflight

PreflightCode = Literal[
    "overset_text",
    "missing_image",
    "broken_asset",
    "low_resolution",
    "font_substitution",
    "page_size_mismatch",
    "margin_violation",
    "page_numbering",
    "empty_frame",
    "missing_caption",
    "layout_overflow",
    "unapproved_content",
]
FixKind = Literal[
    "fit_text", "assign_image", "add_caption", "approve_content", "snap_to_margins", "renumber", "remove_frame", "open_editor"
]


class PreflightIssue(CamelModel):
    id: ID
    code: PreflightCode
    severity: Severity
    page_id: ID | None = None
    page_number: int | None = None
    element_id: ID | None = None
    message: str
    recommendation: str
    fix: FixKind | None = None


class PreflightResponse(CamelModel):
    checked_at: ISODate
    issues: list[PreflightIssue]
    summary: dict[str, int]
    notes: list[str]


# ------------------------------------------------------------------ exports

ExportStatus = Literal["queued", "running", "complete", "failed", "unavailable"]


class ExportRecord(CamelModel):
    id: ID
    project_id: ID
    format: Literal["pdf_digital", "pdf_print", "indd", "idml", "page_previews", "content_package", "asset_archive",
                   "layout_json"]
    status: ExportStatus
    renderer: Literal["browser", "reportlab", "indesign"]
    file_name: str
    size_bytes: int | None = None
    created_at: ISODate
    completed_at: ISODate | None = None
    created_by: ID
    download_url: str | None = None
    message: str | None = None


class ExportCreateRequest(CamelModel):
    format: Literal["pdf_digital", "pdf_print", "indd", "idml", "page_previews", "content_package", "asset_archive",
                   "layout_json"]
    contract: dict | None = None
    renderer: Literal["reportlab", "indesign", "auto"] = "auto"
    force: bool = False  # render pages whose content is not approved (watermarked)


# ------------------------------------------------------------------ activity & jobs


class ActivityItem(CamelModel):
    id: ID
    at: ISODate
    actor_id: ID
    verb: str
    target: str
    kind: Literal["video", "article", "reference", "page", "export", "project", "qc"]
    project_id: ID | None = None


JobKind = Literal["video_pipeline", "reference_analysis", "article_generation", "export_render", "page_reanalysis",
                  "reference_import", "reference_fill", "ai_build", "ai_review"]


class Job(CamelModel):
    id: ID
    kind: JobKind
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    progress: float
    message: str | None = None
    attempts: int
    created_at: ISODate
    updated_at: ISODate
    result_ref: ID | None = None
    error: str | None = None


class ReferenceFillRequest(CamelModel):
    """Design with reference (AI): which reference to copy pages from (the project's own when omitted) and which
    articles to set (every article that isn't rejected when omitted)."""

    reference_id: ID | None = None
    article_ids: list[ID] | None = None


class AiBuildRequest(CamelModel):
    """AI Builder: the reference to design with (the project's own when omitted); `rewrite` writes new articles
    from the transcripts even when the project already has some."""

    reference_id: ID | None = None
    rewrite: bool = False
    ai_images: bool = True  # empty photo frames get AI images (labelled) when an image model is configured
    no_reference: bool = False  # no reference: the built-in templates and house style, chosen for the content


class JobRef(CamelModel):
    job_id: ID
    job: Job
