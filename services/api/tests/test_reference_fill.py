"""Design with reference: articles set into pages copied from the project's reference magazine."""

import pytest

from app.services import reference_fill
from app.services.llm import LLMProvider
from app.services.reference_fill import Column, Slots, _update_furniture, flow
from app.services.reference_import import webfonts
from tests.conftest import SAMPLE_PDF, run_jobs


def _column(lines: int, width: float = 240) -> Column:
    el = {"id": "c", "kind": "text", "x": 0, "y": 0, "w": width, "h": lines * 12, "text": "",
          "style": {"size": 10, "leading": 12, "tracking": 0}}
    return Column(el, lines, gap_lines=1, face=None)


def test_flow_breaks_paragraphs_across_columns_and_keeps_subheads_with_their_text():
    para = " ".join(["word"] * 120)
    cols = [_column(8), _column(30)]
    out, left = flow([para, "## A subhead", para], cols)
    assert not left
    assert out[0] and not out[0][-1].startswith("## ")  # a subhead never ends a column
    assert any(p.startswith("## ") for p in out[1])
    assert sum(len(p.split()) for col in out for p in col) == 240 + 3  # nothing lost or doubled


def test_flow_reports_what_does_not_fit():
    out, left = flow([" ".join(["word"] * 400)], [_column(4)])
    assert out[0] and left


def test_furniture_takes_the_new_page_number_and_issue_date():
    slots = Slots(furniture=[{"text": "60", "style": {}}, {"text": "MAY 2026", "style": {}},
                             {"text": "[ egov.eletsonline.com ]", "style": {}}])
    _update_furniture(slots, 7, "2026-12-02")
    assert [e["text"] for e in slots.furniture] == ["7", "DEC 2026", "[ egov.eletsonline.com ]"]


def test_short_paragraphs_are_joined_before_any_text_is_cut():
    paras = ["Short point number %d about the summit." % i for i in range(8)]
    col = _column(9)
    assert reference_fill.measure(paras, [col])[1] > 0
    merged = reference_fill.merge_short(paras, [col])
    assert reference_fill.measure(merged, [col])[1] == 0
    assert " ".join(merged).split() == " ".join(paras).split()  # every word kept


def test_an_article_that_ends_early_gives_its_empty_column_to_the_picture():
    full, empty = _column(10), _column(10)
    empty.element.update(x=300, y=0, w=240, h=120)
    photo = {"x": 340, "y": 150, "w": 120, "h": 100}
    slots = Slots(columns=[full, empty], photos=[photo])
    reference_fill.grow_photos(slots, [full])
    assert (photo["x"], photo["y"], photo["w"], photo["h"]) == (300, 0, 240, 250)


class FakeWriter(LLMProvider):
    """Keeps the body as it is and clears every other frame (the headline and deck are set regardless)."""

    name, model = "fake", "fake-1"

    def configured(self):
        return True, "fake"

    def check(self):
        return True, "fake"

    def complete_json(self, *, system, prompt, schema, schema_name, **kw):
        import json

        data = json.loads(prompt)
        if schema_name == "fitted_body":
            return {"paragraphs": data["paragraphs"]}
        return {"frames": [{"id": f["id"], "action": "clear", "text": ""} for f in data["frames"]]}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(webfonts.GoogleFonts, "has", lambda self, family: False)


def test_design_with_reference_end_to_end(client, owner, monkeypatch):
    h = owner["headers"]
    up = client.post("/api/v1/references", headers=h, data={"title": "Sample"},
                     files={"file": ("sample.pdf", SAMPLE_PDF.read_bytes(), "application/pdf")}).json()
    run_jobs()
    proj = client.post("/api/v1/projects", headers=h,
                       json={"title": "Summit", "referenceId": up["reference"]["id"]}).json()

    # Nothing to set yet: the API says what's missing.
    r = client.post(f"/api/v1/projects/{proj['id']}/reference-fill", headers=h)
    assert r.status_code == 422 and "articles" in r.json()["detail"]

    body = [{"id": f"b{i}", "kind": "paragraph", "text": "The summit opened with a keynote on digital public "
             "infrastructure and the people who run it. " * 4} for i in range(12)]
    art = client.post(f"/api/v1/projects/{proj['id']}/articles", headers=h,
                      json={"type": "event_report", "headline": "Rails for everyone", "subheadline": "A deck",
                            "body": body}).json()
    r = client.post(f"/api/v1/projects/{proj['id']}/reference-fill", headers=h)
    assert r.status_code == 503  # tests run without an AI provider

    monkeypatch.setattr(reference_fill, "get_llm", lambda: FakeWriter())
    monkeypatch.setattr("app.services.llm.get_llm", lambda: FakeWriter())
    r = client.post(f"/api/v1/projects/{proj['id']}/reference-fill", headers=h)
    assert r.status_code == 202, r.text
    run_jobs()
    job = client.get(f"/api/v1/jobs/{r.json()['jobId']}", headers=h).json()
    assert job["status"] == "succeeded", job

    pages = client.get(f"/api/v1/projects/{proj['id']}/workspace", headers=h).json()["pages"]
    assert pages and all(p["articleId"] == art["id"] for p in pages)
    assert all(p["templateId"].startswith(f"tpl_ref_{up['reference']['id']}_p") for p in pages)
    columns = [e for p in pages for e in p["elements"] if e["id"].startswith("el_col_") and e["text"]]
    assert columns and all(e["literal"] and e["style"].get("fontFamily") for e in columns)
    assert "keynote on digital public infrastructure" in columns[0]["text"]
    texts = " ".join(e.get("text") or "" for p in pages for e in p["elements"] if e["kind"] == "text")
    assert "Rails for everyone" in texts  # the opener's largest title is the headline
