"""Edit as a design: a reference PDF becomes a new project of editable pages, with its fonts served to the editor."""

import pytest

from app.services.reference_import import webfonts
from tests.conftest import SAMPLE_PDF, run_jobs


@pytest.fixture(autouse=True)
def _no_google(monkeypatch):
    # No network in tests: every face is served from the PDF's own embedded fonts.
    monkeypatch.setattr(webfonts.GoogleFonts, "has", lambda self, family: False)


def test_reference_becomes_an_editable_project(client, owner):
    h = owner["headers"]
    up = client.post("/api/v1/references", headers=h, data={"title": "Sample"},
                     files={"file": ("sample.pdf", SAMPLE_PDF.read_bytes(), "application/pdf")}).json()
    run_jobs()
    r = client.post(f"/api/v1/references/{up['reference']['id']}/import", headers=h)
    assert r.status_code == 202, r.text
    started = r.json()
    run_jobs()
    job = client.get(f"/api/v1/jobs/{started['jobId']}", headers=h).json()
    assert job["status"] == "succeeded", job

    ws = client.get(f"/api/v1/projects/{started['projectId']}/workspace", headers=h).json()
    assert ws["project"]["referenceId"] == up["reference"]["id"]
    assert len(ws["pages"]) == 12
    frames = [e for p in ws["pages"] for e in p["elements"] if e["kind"] == "text"]
    assert frames and all(e["literal"] and e["style"].get("fontFamily") for e in frames)
    assets = {a["id"] for a in ws["assets"]}
    assert all(e["assetId"] in assets for p in ws["pages"] for e in p["elements"] if e["kind"] == "image")

    # The sample is set in the standard PDF fonts, which aren't embedded: the editor falls back to web fonts.
    css = client.get(f"/api/v1/projects/{started['projectId']}/fonts.css", headers=h)
    assert css.status_code == 200 and css.text.startswith('@import url("https://fonts.googleapis.com/css2?')
    assert client.get(f"/api/v1/projects/{started['projectId']}/fonts/f1.woff", headers=h).status_code == 404


def test_projects_without_imported_fonts_get_an_empty_stylesheet(client, owner):
    h = owner["headers"]
    proj = client.post("/api/v1/projects", headers=h, json={"title": "Plain"}).json()
    r = client.get(f"/api/v1/projects/{proj['id']}/fonts.css", headers=h)
    assert r.status_code == 200 and r.text == ""
