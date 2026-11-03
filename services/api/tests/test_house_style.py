"""Reference house style: recurring page furniture and column rules are found and reapplied to composed pages."""

from app.services.page_builder import house_style_elements
from app.services.reference_analysis import house_style


def shape(x, y, w, h, color="#1B4178", kind="shape"):
    return {"kind": kind, "x": x, "y": y, "w": w, "h": h, "color": color}


def page(n, extra=(), header_len=0.6, layout="two_column_article"):
    tab_x = 0.953 if n % 2 else 0.0
    return {"pageNumber": n, "layoutType": layout, "blocks": [
        shape(tab_x, 0.0, 0.047, 0.171),
        shape(0.0, 0.061, header_len, 0.0, "#58595B", "rule") if n % 2 else shape(1 - header_len, 0.061, header_len, 0.0, "#58595B", "rule"),
        shape(0.5, 0.12, 0.0, 0.8, "#404041", "rule"),  # column rule: not furniture
        shape(0.3, 0.97, 0.01, 0.01, "#FAC210"),  # icon speck: not furniture
        shape(1.26, 0.97, 0.0, 0.015),  # facing page's share of a spread element
        *extra,
    ]}


def test_house_style_finds_furniture_per_side_and_column_rules():
    pages = [page(n, header_len=0.55 + 0.02 * (n % 4)) for n in range(2, 14)] + [page(1, layout="cover")]
    furniture, column_rules = house_style(pages)
    assert column_rules is True
    by_side = {s: [f for f in furniture if f["side"] == s] for s in ("recto", "verso")}
    for side, tab_x in (("recto", 0.953), ("verso", 0.0)):
        kinds = sorted((f["kind"], f["x"]) for f in by_side[side])
        assert ("shape", tab_x) in kinds and len(by_side[side]) == 2, by_side[side]
        rule = next(f for f in by_side[side] if f["kind"] == "rule")
        assert rule["y"] == 0.061 and 0.55 <= rule["w"] <= 0.61  # median of the varying lengths
    assert all(f["fill"] for f in furniture)


def test_house_style_elements_dress_a_page():
    profile = {"page": {"widthPt": 600, "heightPt": 800, "bleedPt": 9}, "colors": {"rule": "#58595B"},
               "grid": {"gutterPt": 12, "columnRules": True},
               "furniture": [{"side": "verso", "kind": "shape", "x": 0.0, "y": 0.0, "w": 0.05, "h": 0.2, "fill": "#1B4178"},
                             {"side": "verso", "kind": "rule", "x": 0.4, "y": 0.06, "w": 0.6, "h": 0.0, "fill": None},
                             {"side": "recto", "kind": "shape", "x": 0.95, "y": 0.0, "w": 0.05, "h": 0.2}]}
    body = {"id": "el_body", "kind": "text", "styleRole": "body", "x": 40, "y": 100, "w": 512, "h": 600, "z": 3, "columns": 2}
    els = {e["id"]: e for e in house_style_elements(profile, 2, [body])}
    tab = els["el_furn1"]
    assert (tab["x"], tab["y"], tab["w"], tab["h"]) == (-9, -9, 39, 169) and tab["locked"]  # bleeds off the corner
    rule = els["el_furn2"]
    assert rule["h"] == 0.75 and rule["w"] == 369 and rule["fill"] == "#58595B"  # runs into the bleed on the right
    col = els["el_colrule1_1"]
    assert abs(col["x"] - (40 + 250 + 6 - 0.375)) < 0.01 and col["h"] == 600  # centre of the gutter
    assert not any(e["id"].startswith("el_furn") and e["x"] > 500 for e in els.values())  # recto tab not on a verso
