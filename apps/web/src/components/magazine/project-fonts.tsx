"use client";

import { useWorkspace } from "@/lib/store/workspace";

/**
 * The fonts of a project copied from a reference PDF: Google families plus the PDF's own embedded fonts, served by
 * the API. Other projects get an empty stylesheet.
 */
export function ProjectFonts({ projectId }: { projectId?: string | null }) {
  const mode = useWorkspace((s) => s.mode);
  if (mode !== "live" || !projectId) return null;
  return <link rel="stylesheet" href={`/api/v1/projects/${projectId}/fonts.css`} />;
}
