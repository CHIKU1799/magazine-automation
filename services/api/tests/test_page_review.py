"""The checking agent: exact checks on rendered measurements, direct fixes, and limits on what the reviewer may touch."""

from app.services.page_render import Rendered, effective_style
from app.services.page_review import apply_operations, auto_fix, find_problems, safe_operations

PROFILE = {"page": {"widthPt": 600, "heightPt": 800, "bleedPt": 10},
           "margins": {"top": 50, "bottom": 50, "inside": 40, "outside": 40},
           "typography": {"body": {"size": 10, "leading": 12, "color": "#111111"},
                          "headline": {"size": 30, "leading": 32}},
           "colors": {"paper": "#FFFFFF"}}


def text(id_, x, y, w, h, words=3, **kw):
    return {"id": id_, "kind": "text", "name": id_, "x": x, "y": y, "w": w, "h": h, "z": 1, "text": "word " * words, **kw}


def kinds(problems):
    return {p.kind for p in problems}


def test_finds_overset_text_overlaps_and_frames_off_the_page():
    a, b = text("a", 50, 100, 200, 100), text("b", 60, 120, 200, 100)
    shot = Rendered(b"", {"a": {"fits": False, "ink": [50, 100, 250, 200], "shrink": 1},
                          "b": {"fits": True, "ink": [60, 120, 620, 200], "shrink": 1}})
    found = kinds(find_problems([a, b], shot, PROFILE, {}))
    assert {"overset", "off_page", "text_overlap"} <= found


def test_finds_empty_and_misplaced_pictures_and_text_over_them():
    empty = {"id": "e", "kind": "image", "name": "e", "x": 50, "y": 50, "w": 100, "h": 100, "z": 1}
    off = {"id": "o", "kind": "image", "name": "o", "x": 500, "y": 300, "w": 105, "h": 100, "z": 1, "assetId": "x"}
    label = text("t", 510, 310, 80, 20)
    label["z"] = 2
    shot = Rendered(b"", {"t": {"fits": True, "ink": [510, 310, 590, 330], "shrink": 1}})
    found = kinds(find_problems([empty, off, label], shot, PROFILE, {"x": {"widthPx": 1000, "heightPx": 1000}}))
    assert {"empty_image", "image_off_page", "text_on_image"} <= found


def test_a_full_bleed_cover_picture_may_carry_text():
    cover = {"id": "c", "kind": "image", "role": "hero_image", "name": "c", "x": -10, "y": -10, "w": 620, "h": 820, "z": 0,
             "assetId": "x"}
    title = text("t", 50, 50, 300, 60)
    shot = Rendered(b"", {"t": {"fits": True, "ink": [50, 50, 300, 100], "shrink": 1}})
    assert not find_problems([cover, title], shot, PROFILE, {"x": {"widthPx": 1200, "heightPx": 1600}})


def test_leftover_timestamps_and_placeholders_are_caught():
    a = text("a", 50, 50, 200, 40)
    a["text"] = "His remarks at 67-78 seconds matter."
    b = text("b", 50, 200, 200, 40)
    b["text"] = "Section Title"
    found = kinds(find_problems([a, b], Rendered(b"", {}), PROFILE, {}))
    assert {"timestamp", "placeholder"} <= found


def test_overset_text_gets_the_free_space_below_it_first():
    a = text("a", 50, 100, 200, 100)
    shot = Rendered(b"", {"a": {"fits": False, "ink": [50, 100, 250, 230], "scrollH": 130, "clientH": 100, "shrink": 1}})
    problems = find_problems([a], shot, PROFILE, {})
    assert auto_fix([a], problems, shot, PROFILE, 1) == 1
    assert a["h"] == 132 and "style" not in a


def test_overset_text_with_no_room_is_set_slightly_smaller():
    a = text("a", 50, 100, 200, 100, styleRole="body")
    below = text("b", 50, 201, 200, 400)
    shot = Rendered(b"", {"a": {"fits": False, "ink": [50, 100, 250, 230], "scrollH": 130, "clientH": 100, "shrink": 1}})
    problems = [p for p in find_problems([a, below], shot, PROFILE, {}) if p.kind == "overset"]
    assert auto_fix([a, below], problems, shot, PROFILE, 1) == 1
    assert a["style"]["size"] == 9.6 and a["h"] == 100


def test_the_reviewer_cannot_rewrite_or_delete_article_text_or_move_the_background():
    body = text("body", 50, 100, 200, 400, words=200)
    title = text("title", 50, 50, 200, 40)
    bg = {"id": "bg", "kind": "image", "role": "background", "x": 0, "y": 0, "w": 600, "h": 800}
    ops = [{"op": "delete", "elementId": "body"},
           {"op": "update", "elementId": "body", "patch": {"text": "shorter", "h": 300}},
           {"op": "update", "elementId": "bg", "patch": {"x": 5}},
           {"op": "update", "elementId": "title", "patch": {"text": "A better title"}},
           {"op": "add", "kind": "text", "patch": {}}]
    allowed = safe_operations(ops, [body, title, bg])
    assert allowed == [{"op": "update", "elementId": "body", "patch": {"h": 300}},
                       {"op": "update", "elementId": "title", "patch": {"text": "A better title"}}]


def test_operations_apply_as_the_page_editor_applies_them():
    a, b = text("a", 50, 50, 200, 40, style={"size": 10}), text("b", 50, 100, 200, 40)
    done = apply_operations([a, b], [{"op": "update", "elementId": "a", "patch": {"x": 60}, "style": {"color": "#000000"},
                                      "arrange": "front"}])
    assert done == 1 and a["x"] == 60 and a["style"] == {"size": 10, "color": "#000000"} and a["z"] == 2


def test_empty_style_values_do_not_override_the_design():
    el = {"styleRole": "body", "style": {"size": 9, "color": None}}
    style = effective_style(PROFILE, el)
    assert style["size"] == 9 and style["color"] == "#111111"
