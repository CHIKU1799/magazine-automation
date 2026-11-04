"""AI Builder: processed videos -> articles -> a whole magazine in the reference's design, in one job."""

import json

import pytest

from app.db import session_factory
from app.ids import new_id
from app.models import EventSession, TranscriptSegment, VideoSource
from app.services import ai_builder, reference_fill
from app.services.ai_builder import article_plan
from app.services.llm import LLMError, LLMProvider
from app.services.reference_import import webfonts
from tests.conftest import SAMPLE_PDF, run_jobs


def test_article_plan_follows_the_sessions():
    one = [EventSession(id="s1", kind="presentation")]
    assert [t for t, _ in article_plan(one, speakers=1)] == ["editorial", "event_report", "highlights"]
    many = [EventSession(id="s1", kind="panel"), EventSession(id="s2", kind="keynote")]
    plan = article_plan(many, speakers=3)
    assert [t for t, _ in plan] == ["editorial", "panel", "keynote", "event_report"]
    assert plan[1][1] == ["s1"] and plan[-1][1] == ["s1", "s2"]


def test_a_rejected_reply_is_retried_on_the_other_model():
    class Primary:
        def complete_json(self, **kw):
            raise LLMError('Endpoint returned HTTP 400: {"error":{"message":"Failed to validate JSON."}}', retryable=False)

    class Fallback:
        def complete_json(self, **kw):
            return {"ok": True}

    w = reference_fill.Writer(Primary(), Fallback())
    assert w.complete_json() == {"ok": True} and not w.switched  # this reply only


def test_pages_copied_from_a_reference_are_not_the_projects_own():
    assert reference_fill.is_reference_copy([{"kind": "text", "literal": True}])
    assert not reference_fill.is_reference_copy([{"kind": "text", "text": "Contents"}])


def test_timestamps_come_out_of_finished_copy():
    assert ai_builder.untimed("This view, articulated at 103 seconds, aligns.") == "This view aligns."
    assert ai_builder.untimed("His remarks at 67-78 seconds matter.") == "His remarks matter."
    assert ai_builder.untimed("His remarks at 67‑78 seconds matter.") == "His remarks matter."  # non-breaking
    assert ai_builder.untimed("He taught for 16 years at 3 schools.") == "He taught for 16 years at 3 schools."


class FakeAI(LLMProvider):
    """Writes a fixed article, keeps fitted bodies, clears frames (headlines are set regardless)."""

    name, model = "fake", "fake-1"

    def configured(self):
        return True, "fake"

    def check(self):
        return True, "fake"

    def complete_json(self, *, system, prompt, schema, schema_name, **kw):
        if schema_name == "article":
            para = "Delegates at the summit discussed how schools can adopt new tools safely and fairly. " * 5
            return {"kicker": None, "headline": "Schools and the new tools", "subheadline": "What the summit agreed",
                    "intro": "", "body": [{"kind": "paragraph", "text": para, "speakerId": None, "sourceSegments": []}
                                          for _ in range(8)],
                    "quoteFactIds": [], "headlineVariants": []}
        data = json.loads(prompt)
        if schema_name == "fitted_body":
            return {"paragraphs": data["paragraphs"]}
        return {"frames": [{"id": f["id"], "action": "clear", "text": ""} for f in data["frames"]]}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(webfonts.GoogleFonts, "has", lambda self, family: False)


def test_builds_a_magazine_from_the_transcript(client, owner, monkeypatch):
    h = owner["headers"]
    up = client.post("/api/v1/references", headers=h, data={"title": "Sample"},
                     files={"file": ("sample.pdf", SAMPLE_PDF.read_bytes(), "application/pdf")}).json()
    run_jobs()
    proj = client.post("/api/v1/projects", headers=h, json={"title": "Summit", "referenceId": up["reference"]["id"]}).json()

    r = client.post(f"/api/v1/projects/{proj['id']}/ai-build", headers=h)
    assert r.status_code == 422 and "video" in r.json()["detail"]  # nothing transcribed yet

    with session_factory()() as db:
        vid = VideoSource(id=new_id("vid"), org_id=owner["org"]["id"], project_id=proj["id"], source_type="upload_video",
                          title="Keynote")
        db.add(vid)
        db.flush()
        for i in range(6):
            db.add(TranscriptSegment(id=new_id("seg"), project_id=proj["id"], video_id=vid.id, idx=i, start=i * 10,
                                     end=i * 10 + 9, text=f"Schools need safe tools, said the speaker ({i})."))
        db.add(EventSession(id=new_id("ses"), project_id=proj["id"], video_id=vid.id, title="Keynote",
                            kind="presentation"))
        db.commit()

    r = client.post(f"/api/v1/projects/{proj['id']}/ai-build", headers=h)
    assert r.status_code == 503  # no AI provider in tests

    fake = FakeAI()
    monkeypatch.setattr(reference_fill, "get_llm", lambda: fake)
    monkeypatch.setattr(reference_fill, "get_vision_llm", lambda: fake)
    monkeypatch.setattr("app.services.llm.get_llm", lambda: fake)
    r = client.post(f"/api/v1/projects/{proj['id']}/ai-build", headers=h, json={"aiImages": False})
    assert r.status_code == 202, r.text
    run_jobs()
    job = client.get(f"/api/v1/jobs/{r.json()['jobId']}", headers=h).json()
    assert job["status"] == "succeeded", job
    assert "magazine is ready" in job["message"]

    ws = client.get(f"/api/v1/projects/{proj['id']}/workspace", headers=h).json()
    types = [p["pageType"] for p in ws["pages"]]
    assert types[:2] == ["cover", "toc"]
    article_pages = [p for p in ws["pages"] if p.get("articleId")]
    assert article_pages and all(p["templateId"].startswith("tpl_ref_") for p in article_pages)
    written = [a for a in ws["articles"] if a["generatedBy"] == "llm"]
    assert [a["type"] for a in written] == ["editorial", "event_report", "highlights"]
    assert all(a["status"] == "draft" and "AI Builder" in (a["reviewNote"] or "") for a in written)


def test_builds_its_own_design_without_a_reference(client, owner, monkeypatch):
    h = owner["headers"]
    proj = client.post("/api/v1/projects", headers=h, json={"title": "Summit", "targetPageCount": 24}).json()
    with session_factory()() as db:
        vid = VideoSource(id=new_id("vid"), org_id=owner["org"]["id"], project_id=proj["id"], source_type="upload_video",
                          title="Keynote")
        db.add(vid)
        db.flush()
        db.add(TranscriptSegment(id=new_id("seg"), project_id=proj["id"], video_id=vid.id, idx=0, start=0, end=9,
                                 text="Schools need safe tools, said the speaker."))
        db.add(EventSession(id=new_id("ses"), project_id=proj["id"], video_id=vid.id, title="Keynote", kind="presentation"))
        db.commit()
    fake = FakeAI()
    monkeypatch.setattr(reference_fill, "get_llm", lambda: fake)
    monkeypatch.setattr(reference_fill, "get_vision_llm", lambda: fake)
    monkeypatch.setattr("app.services.llm.get_llm", lambda: fake)
    r = client.post(f"/api/v1/projects/{proj['id']}/ai-build", headers=h, json={"noReference": True, "aiImages": False})
    assert r.status_code == 202, r.text
    run_jobs()
    job = client.get(f"/api/v1/jobs/{r.json()['jobId']}", headers=h).json()
    assert job["status"] == "succeeded", job

    ws = client.get(f"/api/v1/projects/{proj['id']}/workspace", headers=h).json()
    assert not ws["project"].get("referenceId") and not ws["project"].get("designProfileId")
    types = [p["pageType"] for p in ws["pages"]]
    assert types[:2] == ["cover", "toc"] and "advertisement" not in types
    assert len(ws["pages"]) < 24  # as many pages as the content needs, no blank padding
    assert not any(p["label"].startswith("Unassigned") for p in ws["pages"])
    assert all(p["templateId"].startswith("tpl_") and "_ref_" not in p["templateId"] for p in ws["pages"])
    profile = client.get("/api/v1/design-profiles/house_style", headers=h)
    assert profile.status_code == 200 and profile.json()["page"]["widthPt"] > 500
