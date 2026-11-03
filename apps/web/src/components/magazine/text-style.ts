import type * as React from "react";
import type { DesignProfile, PageElement, TextRole, TextStyle } from "@/lib/domain/types";

/** Web fonts available to the HTML preview renderer. */
export const PREVIEW_FONTS: { name: string; stack: string; classification: TextStyle["classification"] }[] = [
  { name: "Source Serif 4", stack: "var(--font-source-serif), Georgia, serif", classification: "serif" },
  { name: "Newsreader", stack: "var(--font-newsreader), Georgia, serif", classification: "serif" },
  { name: "Playfair Display", stack: "var(--font-playfair), Georgia, serif", classification: "display" },
  { name: "Inter", stack: "var(--font-inter), Helvetica, Arial, sans-serif", classification: "sans" },
  { name: "JetBrains Mono", stack: "var(--font-jetbrains), monospace", classification: "mono" },
];

export function fontStack(name: string | undefined, classification: TextStyle["classification"] = "serif"): string {
  const found = PREVIEW_FONTS.find((f) => f.name === name);
  if (found) return found.stack;
  return classification === "sans" ? PREVIEW_FONTS[3].stack : classification === "display" ? PREVIEW_FONTS[2].stack : PREVIEW_FONTS[0].stack;
}

export function effectiveStyle(profile: DesignProfile, el: PageElement): TextStyle & { align?: string } {
  const role: TextRole = el.styleRole ?? "body";
  const base = profile.typography[role] ?? profile.typography.body;
  return { ...base, ...el.style } as TextStyle & { align?: string };
}

export function textCss(profile: DesignProfile, el: PageElement): React.CSSProperties {
  const s = effectiveStyle(profile, el);
  const role = el.styleRole ?? "body";
  const own = el.style?.fontFamily;
  return {
    fontFamily: own ? `"${own}", ${fontStack(s.previewFont, s.classification)}` : fontStack(s.previewFont, s.classification),
    fontSize: s.size,
    lineHeight: `${s.leading}px`,
    fontWeight: s.weight,
    letterSpacing: `${s.tracking / 1000}em`,
    color: s.color,
    textTransform: s.uppercase ? "uppercase" : undefined,
    fontStyle: s.italic ? "italic" : undefined,
    textAlign: (s.align as React.CSSProperties["textAlign"]) ?? (role === "body" ? "justify" : "left"),
    hyphens: role === "body" && !el.literal ? "auto" : "manual",
    whiteSpace: el.literal ? "pre-line" : undefined,
    columnCount: el.columns && el.columns > 1 ? el.columns : undefined,
    columnGap: el.columns && el.columns > 1 ? el.gutter ?? profile.grid.gutterPt : undefined,
    ["--pf-indent" as string]: role === "body" && !el.literal ? "0.9em" : "0",
  };
}

/**
 * Shrink a frame's type until it fits, matching the PDF renderer's `shrink_to_fit`
 * for headlines. Sizes are written to the node; it never grows type past `size`.
 */
export function shrinkToFit(node: HTMLElement, size: number, leading: number): void {
  const apply = (f: number) => {
    node.style.fontSize = `${size * f}px`;
    node.style.lineHeight = `${leading * f}px`;
  };
  apply(1);
  if (!isOverset(node)) return;
  let lo = 0.1;
  let hi = 1;
  for (let i = 0; i < 10; i++) {
    const mid = (lo + hi) / 2;
    apply(mid);
    if (isOverset(node)) hi = mid;
    else lo = mid;
  }
  apply(lo);
}

/**
 * True when a text frame's content doesn't fit. Multi-column frames overflow
 * sideways into extra columns; single-column frames are measured by where the
 * last laid-out block ends (scrollHeight would also count the ink of display
 * type whose line height is tighter than its font size).
 */
export function isOverset(node: HTMLElement, columns?: number): boolean {
  if (node.scrollWidth > node.clientWidth + 1.5) return true;
  if (columns && columns > 1) return false;
  const last = node.lastElementChild as HTMLElement | null;
  if (!last) return false;
  const bottom = last.offsetTop - node.offsetTop + last.offsetHeight;
  return bottom > node.clientHeight + 1.5;
}
