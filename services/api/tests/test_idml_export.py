"""InDesign package: an .idml (well-formed, mimetype first and stored) zipped with its linked pictures."""

import io
import zipfile
import xml.dom.minidom

import pytest

from app.services.reference_import import webfonts
from tests.conftest import SAMPLE_PDF, run_jobs


@pytest.fixture(autouse=True)
def _no_google(monkeypatch):
    monkeypatch.setattr(webfonts.GoogleFonts, "has", lambda self, family: False)


def test_idml_package_from_an_editable_copy(client, owner):
    h = owner["headers"]
    up = client.post("/api/v1/references", headers=h, data={"title": "Sample"},
                     files={"file": ("sample.pdf", SAMPLE_PDF.read_bytes(), "application/pdf")}).json()
    run_jobs()
    project_id = client.post(f"/api/v1/references/{up['reference']['id']}/import", headers=h).json()["projectId"]
    run_jobs()

    r = client.post(f"/api/v1/projects/{project_id}/exports", headers=h, json={"format": "idml"})
    assert r.status_code == 202, r.text
    run_jobs()
    exp = client.get(f"/api/v1/exports/{r.json()['id']}", headers=h).json()
    assert exp["status"] == "complete", exp
    assert exp["fileName"].endswith("-indesign-idml.zip")

    pkg = zipfile.ZipFile(io.BytesIO(client.get(f"/api/v1/exports/{exp['id']}/download", headers=h).content))
    names = pkg.namelist()
    idml_name = next(n for n in names if n.endswith(".idml"))
    assert any(n.endswith("README.txt") for n in names)
    links = [n for n in names if "/Links/" in n]

    idml = zipfile.ZipFile(io.BytesIO(pkg.read(idml_name)))
    first = idml.infolist()[0]
    assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
    assert idml.read("mimetype") == b"application/vnd.adobe.indesign-idml-package"
    for n in idml.namelist():
        if n.endswith(".xml"):
            xml.dom.minidom.parseString(idml.read(n))
    designmap = idml.read("designmap.xml").decode()
    spreads = [n for n in idml.namelist() if n.startswith("Spreads/")]
    assert len(spreads) == 12 and designmap.count("<idPkg:Spread ") == 12
    assert 'PagesPerDocument="12"' in idml.read("Resources/Preferences.xml").decode()
    stories = [n for n in idml.namelist() if n.startswith("Stories/")]
    assert stories and all(f'src="{s}"' in designmap for s in stories)
    linked = {u.split("file:Links/", 1)[1].split('"', 1)[0]
              for s in spreads for u in idml.read(s).decode().split("LinkResourceURI=")[1:]}
    assert linked and {n.rsplit("/", 1)[1] for n in links} == linked
