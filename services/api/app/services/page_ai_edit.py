"""Edit with AI: a natural-language instruction becomes validated frame operations for one page.

The model only proposes operations; this module checks every one against the page (frame ids, locks, generated
frames, project assets, sane geometry and type) and drops what doesn't hold. The editor applies the result as a
single undoable change.
"""

from __future__ import annotations

import json
import re
from typing import Any, get_args

from app.schemas.base import TextRole
from app.services.llm import LLMProvider, nullable, strict_object

TEXT_ROLES = list(get_args(TextRole))
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")

OPERATION = strict_object({
    "op": {"type": "string", "enum": ["update", "add", "delete"]},
    "elementId": nullable({"type": "string"}),
    "kind": nullable({"type": "string", "enum": ["text", "image", "shape"]}),
    "name": nullable({"type": "string"}),
    "x": nullable({"type": "number"}), "y": nullable({"type": "number"}),
    "w": nullable({"type": "number"}), "h": nullable({"type": "number"}),
    "text": nullable({"type": "string"}),
    "styleRole": nullable({"type": "string", "enum": TEXT_ROLES}),
    "size": nullable({"type": "number"}), "leading": nullable({"type": "number"}),
    "weight": nullable({"type": "number"}), "tracking": nullable({"type": "number"}),
    "color": nullable({"type": "string"}),
    "align": nullable({"type": "string", "enum": ["left", "center", "right", "justify"]}),
    "uppercase": nullable({"type": "boolean"}), "italic": nullable({"type": "boolean"}),
    "columns": nullable({"type": "integer"}),
    "fill": nullable({"type": "string"}), "opacity": nullable({"type": "number"}),
    "hidden": nullable({"type": "boolean"}),
    "assetId": nullable({"type": "string"}),
    "cropScale": nullable({"type": "number"}), "cropX": nullable({"type": "number"}), "cropY": nullable({"type": "number"}),
    "arrange": nullable({"type": "string", "enum": ["front", "back"]}),
})
SCHEMA = strict_object({"summary": {"type": "string"}, "operations": {"type": "array", "items": OPERATION}})

SYSTEM = """You are the layout assistant inside a magazine page editor. You turn the designer's instruction into a
minimal list of operations on the page's frames. Coordinates are points from the page's top-left corner; frames may
extend past the trim into the bleed. Change only what the instruction asks. Prefer the design profile's paragraph
styles (styleRole) and colours. Keep text inside the live area unless asked otherwise. When rewriting text keep the
facts; never invent names, quotes or figures. Locked frames and generated frames (folio, toc) cannot be changed.
For operations, set every field you do not change to null. For "update" and "delete", elementId is required; for
"add", kind is required and elementId is null. Use assetId only with ids from the photo library. Text may use blank
lines between paragraphs and the markers "## " (subhead), "Q: " (question) and "^ " (drop-cap lead).
In `summary`, tell the designer in one or two sentences what you changed, or why you could not."""


def _r(v: float) -> float:
    return round(float(v), 1)


def _element_brief(e: dict) -> dict:
    out = {k: e.get(k) for k in ("id", "kind", "name", "role", "styleRole", "x", "y", "w", "h", "z", "columns", "fill",
                                 "opacity", "assetId", "style") if e.get(k) not in (None, {}, "")}
    if e.get("locked"):
        out["locked"] = True
    if e.get("hidden"):
        out["hidden"] = True
    if e.get("dynamic"):
        out["generated"] = e["dynamic"]
    if e.get("text"):
        t = e["text"]
        out["text"] = t if len(t) <= 600 else t[:600] + f"… [{len(t) - 600} more characters]"
    return out


def build_prompt(instruction: str, page: dict, page_number: int, profile: dict, assets: list[dict],
                 selected_id: str | None) -> str:
    W, H = profile["page"]["widthPt"], profile["page"]["heightPt"]
    m = profile["margins"]
    recto = page_number % 2 == 1
    left, right = (m["inside"], m["outside"]) if recto else (m["outside"], m["inside"])
    typo = {r: {k: s.get(k) for k in ("size", "leading", "weight", "previewFont", "uppercase")}
            for r, s in profile["typography"].items()}
    photos = [{"id": a["id"], "caption": a.get("caption"), "tags": a.get("tags"), "px": f"{a.get('widthPx')}x{a.get('heightPx')}"}
              for a in assets if a.get("kind") != "ad"][:60]
    context = {
        "page": {"number": page_number, "type": page.get("pageType"), "widthPt": W, "heightPt": H,
                 "bleedPt": profile["page"].get("bleedPt"),
                 "liveArea": {"x": left, "y": m["top"], "w": _r(W - left - right), "h": _r(H - m["top"] - m["bottom"])},
                 "columns": profile["grid"]["columns"], "gutterPt": profile["grid"]["gutterPt"]},
        "colours": profile["colors"],
        "paragraphStyles": typo,
        "frames": [_element_brief(e) for e in sorted(page.get("elements") or [], key=lambda e: e.get("z", 0))],
        "selectedFrameId": selected_id,
        "photoLibrary": photos,
    }
    sel = (f' The designer has selected frame "{selected_id}"; "this", "it" and "the selection" refer to it.'
           if selected_id else "")
    return f"Page context (JSON):\n{json.dumps(context, ensure_ascii=False)}\n\nInstruction:{sel}\n{instruction.strip()}"


def _clamp(v: Any, lo: float, hi: float) -> float | None:
    if not isinstance(v, (int, float)):
        return None
    return round(max(lo, min(hi, float(v))), 2)


def validate(ops: list[dict], page: dict, profile: dict, asset_ids: set[str]) -> tuple[list[dict], list[str]]:
    """Clean model operations into editor operations; returns (operations, notes about anything dropped)."""
    W, H = profile["page"]["widthPt"], profile["page"]["heightPt"]
    bleed = float(profile["page"].get("bleedPt") or 0)
    elements = {e["id"]: e for e in page.get("elements") or []}
    out: list[dict] = []
    notes: list[str] = []
    for op in ops:
        kind = op.get("op")
        el = elements.get(op.get("elementId") or "")
        if kind in ("update", "delete"):
            if el is None:
                notes.append(f"Skipped a change to an unknown frame ({op.get('elementId')}).")
                continue
            if el.get("locked"):
                notes.append(f"“{el.get('name')}” is locked, so it wasn't changed.")
                continue
            if kind == "delete":
                out.append({"op": "delete", "elementId": el["id"]})
                continue
        elif kind == "add":
            if op.get("kind") not in ("text", "image", "shape"):
                notes.append("Skipped a new frame without a kind.")
                continue
        else:
            continue

        patch: dict[str, Any] = {}
        for k, lo, hi in (("x", -bleed - W, 2 * W + bleed), ("y", -bleed - H, 2 * H + bleed), ("w", 4, W + 2 * bleed),
                          ("h", 4, H + 2 * bleed), ("opacity", 0, 1)):
            v = _clamp(op.get(k), lo, hi)
            if v is not None:
                patch[k] = v
        if isinstance(op.get("hidden"), bool):
            patch["hidden"] = op["hidden"]
        if op.get("name"):
            patch["name"] = str(op["name"])[:80]
        if op.get("fill") and HEX.match(op["fill"]):
            patch["fill"] = op["fill"].upper()
        target_kind = el["kind"] if el else op["kind"]
        if target_kind == "text":
            if op.get("text") is not None:
                if el and el.get("dynamic"):
                    notes.append(f"“{el.get('name')}” is generated automatically, so its text wasn't changed.")
                else:
                    patch["text"] = op["text"]
            if op.get("styleRole") in TEXT_ROLES:
                patch["styleRole"] = op["styleRole"]
            cols = op.get("columns")
            if isinstance(cols, int):
                patch["columns"] = max(1, min(6, cols))
        if target_kind == "image" and op.get("assetId"):
            if op["assetId"] in asset_ids:
                patch["assetId"] = op["assetId"]
            else:
                notes.append("Skipped a photo that isn't in this project's library.")
        if target_kind == "image" and any(op.get(k) is not None for k in ("cropScale", "cropX", "cropY")):
            crop = (el or {}).get("crop") or {"scale": 1, "offsetX": 0, "offsetY": 0}
            patch["crop"] = {"scale": _clamp(op.get("cropScale"), 1, 3) or crop.get("scale", 1),
                             "offsetX": _clamp(op.get("cropX"), -1, 1) if op.get("cropX") is not None else crop.get("offsetX", 0),
                             "offsetY": _clamp(op.get("cropY"), -1, 1) if op.get("cropY") is not None else crop.get("offsetY", 0)}

        style: dict[str, Any] = {}
        if target_kind == "text":
            for k, lo, hi in (("size", 4, 400), ("leading", 4, 480), ("tracking", -200, 800)):
                v = _clamp(op.get(k), lo, hi)
                if v is not None:
                    style[k] = v
            if isinstance(op.get("weight"), (int, float)):
                style["weight"] = int(round(max(100, min(900, op["weight"])) / 100) * 100)
            if op.get("color") and HEX.match(op["color"]):
                style["color"] = op["color"].upper()
            if op.get("align"):
                style["align"] = op["align"]
            for k in ("uppercase", "italic"):
                if isinstance(op.get(k), bool):
                    style[k] = op[k]
            # A size change without a leading keeps the frame's proportion.
            if "size" in style and "leading" not in style:
                base = (el or {}).get("style") or {}
                role = patch.get("styleRole") or (el or {}).get("styleRole") or "body"
                ref = profile["typography"].get(role) or profile["typography"]["body"]
                size0, lead0 = base.get("size") or ref["size"], base.get("leading") or ref["leading"]
                style["leading"] = round(style["size"] * lead0 / size0, 1)

        res: dict[str, Any] = {"op": kind, "patch": patch}
        if style:
            res["style"] = style
        if op.get("arrange") in ("front", "back"):
            res["arrange"] = op["arrange"]
        if kind == "update":
            if not patch and not style and "arrange" not in res:
                continue
            res["elementId"] = el["id"]
        else:
            res["kind"] = op["kind"]
        out.append(res)
    return out, notes


def ai_edit(llm: LLMProvider, instruction: str, page: dict, page_number: int, profile: dict, assets: list[dict],
            selected_id: str | None) -> tuple[str, list[dict], list[str]]:
    prompt = build_prompt(instruction, page, page_number, profile, assets, selected_id)
    out = llm.complete_json(system=SYSTEM, prompt=prompt, schema=SCHEMA, max_tokens=8000, schema_name="page_edit")
    ops, notes = validate(out.get("operations") or [], page, profile, {a["id"] for a in assets})
    return (out.get("summary") or "").strip(), ops, notes
