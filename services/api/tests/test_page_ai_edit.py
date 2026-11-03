"""Edit with AI: model operations are validated against the page before the editor applies them."""

from app.services.llm.base import LLMProvider

FIELDS = ["elementId", "kind", "name", "x", "y", "w", "h", "text", "styleRole", "size", "leading", "weight", "tracking",
          "color", "align", "uppercase", "italic", "columns", "fill", "opacity", "hidden", "assetId", "cropScale",
          "cropX", "cropY", "arrange"]


def op(action, **kw):
    return {"op": action, **{f: None for f in FIELDS}, **kw}


class FakeEditor(LLMProvider):
    name, model = "fake", "fake-editor"
    last_prompt = ""

    def configured(self):
        return True, "fake"

    def check(self):
        return True, "fake"

    def complete_json(self, *, system, prompt, schema, **kw):
        FakeEditor.last_prompt = prompt
        return {"summary": "Made the headline red and bigger, added a caption.", "operations": [
            op("update", elementId="el_head", color="#d9402f", size=40, arrange="front"),
            op("update", elementId="el_locked", text="nope"),
            op("update", elementId="el_folio", text="nope"),
            op("update", elementId="el_missing", x=10),
            op("update", elementId="el_img", assetId="not_in_library"),
            op("add", kind="text", x=40, y=600, w=200, h=30, text="Photo: the summit", styleRole="caption"),
            op("delete", elementId="el_rule"),
            op("update", elementId="el_head", color="red", w=99999),
        ]}


PAGE = {"pageType": "event_report", "templateId": "tpl_article_image", "label": "P", "editorialStatus": "pending",
        "designStatus": "in_progress", "locked": False, "elements": [
            {"id": "el_head", "kind": "text", "role": "headline", "name": "Headline", "styleRole": "headline",
             "text": "Old headline", "x": 40, "y": 40, "w": 400, "h": 80, "z": 1, "style": {"size": 30, "leading": 36}},
            {"id": "el_locked", "kind": "text", "role": "deck", "name": "Standfirst", "text": "x", "x": 40, "y": 130,
             "w": 400, "h": 40, "z": 2, "locked": True},
            {"id": "el_folio", "kind": "text", "role": "folio", "name": "Folio", "dynamic": "folio", "x": 40, "y": 780,
             "w": 400, "h": 12, "z": 3},
            {"id": "el_img", "kind": "image", "role": "image", "name": "Image", "x": 40, "y": 200, "w": 400, "h": 300,
             "z": 4},
            {"id": "el_rule", "kind": "rule", "role": "rule", "name": "Rule", "x": 40, "y": 190, "w": 400, "h": 2, "z": 5},
        ]}


def test_ai_edit_validates_operations(client, owner, monkeypatch):
    monkeypatch.setattr("app.api.v1.planner.require_llm", lambda: FakeEditor())
    h = owner["headers"]
    proj = client.post("/api/v1/projects", headers=h, json={"title": "AI edit"}).json()
    r = client.post(f"/api/v1/projects/{proj['id']}/pages/ai-edit", headers=h,
                    json={"page": PAGE, "pageNumber": 3, "instruction": "make the headline red", "selectedId": "el_head"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"].startswith("Made") and body["model"] == "fake-editor"
    assert '"selectedFrameId": "el_head"' in FakeEditor.last_prompt and "Old headline" in FakeEditor.last_prompt

    ops = body["operations"]
    head = ops[0]
    assert head == {"op": "update", "elementId": "el_head", "patch": {}, "arrange": "front",
                    "style": {"size": 40.0, "color": "#D9402F", "leading": 48.0}}  # leading keeps 30/36
    assert {"op": "delete", "elementId": "el_rule"} in ops
    added = next(o for o in ops if o["op"] == "add")
    assert added["kind"] == "text" and added["patch"]["text"] == "Photo: the summit" and added["patch"]["styleRole"] == "caption"
    # Invalid colour dropped; width clamped to the page plus bleed.
    last = ops[-1]
    assert "style" not in last and last["patch"]["w"] < 1000
    assert not any(o.get("elementId") in ("el_locked", "el_folio", "el_missing") for o in ops)
    assert not any("assetId" in o.get("patch", {}) for o in ops)
    notes = " ".join(body["notes"])
    assert "locked" in notes and "generated" in notes and "unknown frame" in notes and "library" in notes


def test_ai_edit_refuses_locked_page(client, owner, monkeypatch):
    monkeypatch.setattr("app.api.v1.planner.require_llm", lambda: FakeEditor())
    h = owner["headers"]
    proj = client.post("/api/v1/projects", headers=h, json={"title": "Locked"}).json()
    r = client.post(f"/api/v1/projects/{proj['id']}/pages/ai-edit", headers=h,
                    json={"page": {**PAGE, "locked": True}, "instruction": "anything"})
    assert r.status_code == 409
