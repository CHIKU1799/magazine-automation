"use client";

import * as React from "react";
import { useSearchParams } from "next/navigation";
import { Printer } from "lucide-react";
import { useWorkspace } from "@/lib/store/workspace";
import { useProjectData } from "@/lib/store/hooks";
import { loadLists, loadWorkspace } from "@/lib/api/live-sync";
import { PageView } from "@/components/magazine/page-view";
import { ProjectFonts } from "@/components/magazine/project-fonts";
import { Button } from "@/components/ui/button";

/**
 * Print view: every page at 100% on its own sheet, for the browser's
 * "Save as PDF". This is the HTML preview renderer, not InDesign output.
 */
function PrintView() {
  const params = useSearchParams();
  const hydrated = useWorkspace((s) => s.hasHydrated);
  const mode = useWorkspace((s) => s.mode);
  const projectId = params.get("project") ?? undefined;
  const data = useProjectData(projectId);
  const { profile, pages, project } = data;
  const auto = params.get("auto") === "1";
  // Live data is not kept in the browser and this page sits outside the app shell, so load it here.
  const [loaded, setLoaded] = React.useState(false);
  const [loadError, setLoadError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!hydrated) return;
    if (mode !== "live" || !projectId) return setLoaded(true);
    loadLists()
      .then(() => loadWorkspace(projectId))
      .then(() => setLoaded(true), (e) => setLoadError((e as Error).message));
  }, [hydrated, mode, projectId]);
  // Print only once every page's fonts and pictures have arrived; a copied magazine's are many and large.
  React.useEffect(() => {
    if (!auto || !loaded || !pages.length) return;
    let cancelled = false;
    (async () => {
      await new Promise((r) => setTimeout(r, 500));
      await document.fonts.ready;
      await Promise.all(Array.from(document.images).map((img) => (img.complete ? null : new Promise((r) => { img.onload = img.onerror = r; }))));
      await document.fonts.ready;
      if (!cancelled) window.print();
    })();
    return () => {
      cancelled = true;
    };
  }, [auto, loaded, pages.length]);

  if (!hydrated) return null;
  if (loadError) return <p className="p-10 text-sm">Couldn’t load the issue: {loadError}</p>;
  if (!loaded) return <p className="p-10 text-sm text-ink-3">Loading pages…</p>;
  if (!profile || !project) return <p className="p-10 text-sm">Nothing to print — open this from the Export Centre.</p>;
  const W = profile.page.widthPt;
  const H = profile.page.heightPt;

  return (
    <>
      <style>{`@page { size: ${W}pt ${H}pt; margin: 0 } @media print { html, body { background: #fff !important } .sheet { break-after: page; box-shadow: none !important; margin: 0 !important } }`}</style>
      <ProjectFonts projectId={project.id} />
      <div className="no-print sticky top-0 z-10 flex items-center gap-3 border-b border-line bg-surface px-6 py-3 text-sm">
        <span className="font-medium">{project.title}</span>
        <span className="text-ink-3">
          {pages.length} pages · HTML preview renderer — use “Save as PDF” in the print dialog. Not InDesign output; draft pages are not watermarked here.
        </span>
        <Button variant="primary" size="sm" className="ml-auto" onClick={() => window.print()}>
          <Printer /> Print / Save as PDF
        </Button>
      </div>
      <div className="flex flex-col items-center gap-6 bg-[#e9e7e1] py-6 print:block print:gap-0 print:bg-white print:py-0">
        {pages.map((p, i) => (
          <div key={p.id} className="sheet shadow-page" style={{ width: `${W}pt`, height: `${H}pt`, overflow: "hidden" }}>
            {/* 1 CSS px = 0.75 pt, so render at 4/3 to fill a sheet sized in points. */}
            <PageView page={p} profile={profile} assets={data.assetsById} ctx={data.ctxFor(i)} scale={4 / 3} localUrls={data.localUrls} />
          </div>
        ))}
      </div>
    </>
  );
}

export default function PrintPage() {
  return (
    <React.Suspense>
      <PrintView />
    </React.Suspense>
  );
}
