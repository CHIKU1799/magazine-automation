import type { DesignProfile, PageElement } from "@/lib/domain/types";
import { liveArea } from "@/lib/templates/geometry";
import { uid } from "@/lib/utils/format";

/** One validated operation from POST /projects/{id}/pages/ai-edit. */
export interface AiPageOperation {
  op: "update" | "add" | "delete";
  elementId?: string;
  kind?: "text" | "image" | "shape";
  patch?: Partial<PageElement>;
  style?: PageElement["style"];
  arrange?: "front" | "back";
}

export interface AiPageEdit {
  summary: string;
  operations: AiPageOperation[];
  notes: string[];
  provider: string;
  model: string;
}

/** Apply "Edit with AI" operations to a page's frames; the caller stores the result as one undoable change. */
export function applyAiOperations(elements: PageElement[], ops: AiPageOperation[], profile: DesignProfile, pageNumber: number): { elements: PageElement[]; changed: number } {
  let next = [...elements];
  let changed = 0;
  const top = () => Math.max(0, ...next.map((e) => e.z)) + 1;
  const bottom = () => Math.min(0, ...next.map((e) => e.z)) - 1;
  for (const o of ops) {
    if (o.op === "delete") {
      const before = next.length;
      next = next.filter((e) => e.id !== o.elementId);
      changed += before - next.length;
      continue;
    }
    if (o.op === "update") {
      const z = o.arrange === "front" ? top() : o.arrange === "back" ? bottom() : undefined;
      next = next.map((e) => {
        if (e.id !== o.elementId) return e;
        changed += 1;
        return { ...e, ...o.patch, ...(o.style ? { style: { ...e.style, ...o.style } } : {}), ...(z !== undefined ? { z } : {}) };
      });
      continue;
    }
    // New frames default to the middle of the live area, on top.
    const la = liveArea(profile, pageNumber);
    const kind = o.kind ?? "text";
    const at = { x: la.x + la.w / 4, y: la.y + la.h / 3, z: top() };
    const base: PageElement =
      kind === "text"
        ? { id: uid("el"), kind: "text", role: "body", styleRole: "body", name: "Text frame", text: "", w: la.w / 2, h: 96, ...at }
        : kind === "image"
          ? { id: uid("el"), kind: "image", role: "image", name: "Image", w: la.w / 2, h: la.w / 3, crop: { scale: 1, offsetX: 0, offsetY: 0 }, ...at }
          : { id: uid("el"), kind: "shape", role: "accent_bar", name: "Shape", fill: profile.colors.accent, w: 120, h: 80, ...at };
    const el: PageElement = { ...base, ...o.patch, ...(o.style ? { style: o.style } : {}) };
    if (el.kind === "text" && el.styleRole) el.role = el.styleRole;
    next.push(el);
    changed += 1;
  }
  return { elements: next, changed };
}
