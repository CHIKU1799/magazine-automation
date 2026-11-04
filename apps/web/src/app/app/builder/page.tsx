"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { CircleCheck, CircleDashed, Clapperboard, Download, LibraryBig, Loader2, PackageOpen, PanelsTopLeft, ShieldCheck, Sparkles, Wand2 } from "lucide-react";
import { toast } from "sonner";
import { useWorkspace } from "@/lib/store/workspace";
import { useProjectData } from "@/lib/store/hooks";
import { Button } from "@/components/ui/button";
import { Badge, EmptyState, PageHeader, Panel, PanelHeader } from "@/components/ui/display";
import { Select, useConfirm } from "@/components/ui/overlays";
import { Progress, Switch } from "@/components/ui/primitives";
import { PageThumb } from "@/components/magazine/page-thumb";
import { live, waitForJob } from "@/lib/api/live";
import { loadLists, loadWorkspace } from "@/lib/api/live-sync";
import { integration, useApiStatus } from "@/lib/api/use-api-status";
import { cn } from "@/lib/utils/cn";

const STEPS = [
  { until: 56, label: "Writing the articles from the transcripts" },
  { until: 58, label: "Composing the cover and contents" },
  { until: 87, label: "Designing the pages" },
  { until: 100, label: "Checking every page and fixing what's broken" },
];
const NO_REFERENCE = "none";

const jobKey = (projectId: string) => `pf-ai-build-${projectId}`;

/**
 * AI Builder: one click from processed videos to a whole magazine in the reference's design. The API writes the
 * articles, composes the cover and contents and lays every article out on pages copied from the reference; the
 * result is ordinary editable pages, ready to export or to refine in the page editor.
 */
export default function AiBuilderPage() {
  const router = useRouter();
  const confirm = useConfirm();
  const data = useProjectData();
  const { project, profile, pages, articles, videos, assetsById } = data;
  const references = useWorkspace((s) => s.references);
  const mode = useWorkspace((s) => s.mode);
  const api = useApiStatus();
  const [referenceId, setReferenceId] = React.useState<string>("");
  const [rewrite, setRewrite] = React.useState(false);
  const [aiImages, setAiImages] = React.useState(true);
  const [running, setRunning] = React.useState<{ progress: number; message?: string; checkOnly?: boolean } | null>(null);
  const [done, setDone] = React.useState<string | null>(null);

  const usableRefs = references.filter((r) => r.hasSourcePdf && (r.status === "analysed" || r.status === "approved"));
  React.useEffect(() => {
    if (referenceId || !project) return;
    if (project.referenceId && usableRefs.some((r) => r.id === project.referenceId)) setReferenceId(project.referenceId);
    else if (!project.referenceId) setReferenceId(NO_REFERENCE);
    else if (usableRefs.length) setReferenceId(usableRefs[0].id);
  }, [referenceId, usableRefs, project]);

  const transcribed = videos.filter((v) => v.transcription.state === "complete");
  const existing = articles.filter((a) => a.status !== "rejected");
  const llm = integration(api.integrations, "llm");
  const live_ = mode === "live" && api.online;
  const blocked = !live_ ? "Needs the PressFlow API" : !transcribed.length ? "Process at least one video first" : !referenceId ? "Choose a reference magazine, or None" : llm && !llm.configured ? "Set up an AI provider in Settings" : null;

  const follow = React.useCallback(
    async (projectId: string, jobId: string) => {
      setRunning({ progress: 0, message: "Starting" });
      setDone(null);
      try {
        const job = await waitForJob(jobId, (j) => setRunning({ progress: j.progress ?? 0, message: j.message ?? undefined, checkOnly: j.kind === "ai_review" }), 2500, 60 * 60_000);
        if (job.status !== "succeeded") throw new Error(job.error ?? job.message ?? "The build didn't finish.");
        await loadLists();
        await loadWorkspace(projectId);
        setDone(job.message ?? "Your magazine is ready");
        toast.success(job.kind === "ai_review" ? "Pages checked" : "Your magazine is ready", { description: job.message ?? undefined, duration: 10000 });
      } catch (err) {
        toast.error("The AI Builder stopped", { description: (err as Error).message, duration: 12000 });
      } finally {
        setRunning(null);
        try {
          localStorage.removeItem(jobKey(projectId));
        } catch {
          /* storage unavailable */
        }
      }
    },
    [],
  );

  // A build keeps running on the API when this page is left; coming back picks it up again.
  React.useEffect(() => {
    if (!project || !live_ || running) return;
    let jobId: string | null = null;
    try {
      jobId = localStorage.getItem(jobKey(project.id));
    } catch {
      /* storage unavailable */
    }
    if (jobId) void follow(project.id, jobId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.id, live_]);

  if (!project) {
    return (
      <div className="mx-auto max-w-[900px] px-6 py-16">
        <Panel>
          <EmptyState icon={<Wand2 />} title="No project yet" description="Create a magazine project and add your event videos; the AI Builder turns them into a finished issue." action={<Button variant="primary" asChild><Link href="/app/new">Create magazine</Link></Button>} />
        </Panel>
      </div>
    );
  }

  const build = async () => {
    const own = referenceId === NO_REFERENCE;
    const replaced = own ? "Every unlocked page, including the cover and contents, is replaced by the new design, and this project stops using its reference magazine. Locked pages are kept." : "Unlocked pages other than the cover and contents are replaced by the new design. Locked pages are kept.";
    if (pages.length && !(await confirm({ title: "Build the magazine again?", description: replaced, confirmLabel: "Build magazine" }))) return;
    try {
      const { jobId } = await live.aiBuild(project.id, { ...(own ? { noReference: true } : { referenceId }), rewrite: rewrite || !existing.length, aiImages });
      try {
        localStorage.setItem(jobKey(project.id), jobId);
      } catch {
        /* storage unavailable */
      }
      await follow(project.id, jobId);
    } catch (err) {
      toast.error("Couldn't start the AI Builder", { description: (err as Error).message });
    }
  };

  // The checking agent on the pages as they stand, e.g. after edits by hand.
  const check = async () => {
    try {
      const { jobId } = await live.aiReview(project.id);
      try {
        localStorage.setItem(jobKey(project.id), jobId);
      } catch {
        /* storage unavailable */
      }
      await follow(project.id, jobId);
    } catch (err) {
      toast.error("Couldn't check the pages", { description: (err as Error).message });
    }
  };

  const reference = usableRefs.find((r) => r.id === referenceId);
  const own = referenceId === NO_REFERENCE;
  const step = running ? STEPS.findIndex((s) => running.progress < s.until) : -1;
  const checks = [
    { ok: transcribed.length > 0, label: transcribed.length ? `${transcribed.length} video${transcribed.length === 1 ? "" : "s"} transcribed` : "No transcribed videos yet", action: <Link href="/app/media" className="text-accent hover:underline">Event Media</Link> },
    { ok: !!reference || own, label: own ? "Design: AI picks the templates" : reference ? `Design: ${reference.title}` : "No reference magazine chosen", action: <Link href="/app/references" className="text-accent hover:underline">Reference Library</Link> },
    { ok: !llm || llm.configured, label: llm?.configured ? "AI provider connected" : "AI provider not set up", action: <Link href="/app/settings" className="text-accent hover:underline">Settings</Link> },
  ];

  return (
    <div className="mx-auto max-w-[1280px] px-6 py-7">
      <PageHeader title="AI Builder" description={`From ${project.title}'s videos to a finished magazine, in your reference's design or one chosen for you. Every page stays editable.`} />

      <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-[380px_1fr]">
        <div className="flex flex-col gap-4">
          <Panel>
            <PanelHeader title="Ready to build" icon={<Sparkles />} />
            <ul className="flex flex-col gap-2.5 px-4 pb-4 text-sm">
              {checks.map((c) => (
                <li key={c.label} className="flex items-center gap-2.5">
                  {c.ok ? <CircleCheck className="size-4 shrink-0 text-success" /> : <CircleDashed className="size-4 shrink-0 text-ink-4" />}
                  <span className={cn("min-w-0 flex-1", !c.ok && "text-ink-3")}>{c.label}</span>
                  {!c.ok && <span className="text-xs">{c.action}</span>}
                </li>
              ))}
            </ul>
          </Panel>

          <Panel className="flex flex-col gap-4 p-4">
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="font-medium">Reference magazine</span>
              <Select value={referenceId} onValueChange={setReferenceId} disabled={!!running} options={[{ value: NO_REFERENCE, label: "None: AI chooses the design" }, ...usableRefs.map((r) => ({ value: r.id, label: r.title }))]} placeholder="Choose a design" />
              <span className="text-xs text-ink-3">{own ? "AI picks built-in templates to suit each article, in the PressFlow house style." : "Its layouts, fonts and colours are copied page by page."}</span>
            </label>
            {existing.length > 0 && (
              <label className="flex items-start gap-2.5 text-sm">
                <Switch checked={rewrite} onCheckedChange={setRewrite} disabled={!!running} className="mt-0.5" />
                <span>
                  <span className="font-medium">Write new articles from the transcripts</span>
                  <span className="block text-xs text-ink-3">Off: uses this project&apos;s {existing.length} article{existing.length === 1 ? "" : "s"} from Content Studio.</span>
                </span>
              </label>
            )}
            <label className="flex items-start gap-2.5 text-sm">
              <Switch checked={aiImages} onCheckedChange={setAiImages} disabled={!!running} className="mt-0.5" />
              <span>
                <span className="font-medium">Fill empty photo frames with AI images</span>
                <span className="block text-xs text-ink-3">When the project runs out of photos. Labelled as AI-generated, no real people; up to 8 per build, each a paid image call.</span>
              </span>
            </label>
            <Button variant="primary" size="lg" disabled={!!blocked || !!running} loading={!!running} onClick={build} className="w-full">
              <Wand2 /> {pages.length ? "Build the magazine again" : "Build my magazine"}
            </Button>
            {blocked && <p className="-mt-2 text-xs text-ink-3">{blocked}</p>}
            {!blocked && !running && <p className="-mt-2 text-xs text-ink-3">Takes a few minutes. You can leave this page; the build carries on.</p>}
          </Panel>

          {running && (
            <Panel className="p-4">
              <div className="mb-2 flex items-center justify-between text-sm">
                <span className="font-medium">Building</span>
                <span className="tabular text-ink-3">{Math.round(running.progress)}%</span>
              </div>
              <Progress value={running.progress} />
              <ol className="mt-3 flex flex-col gap-1.5 text-sm">
                {STEPS.filter((_, i) => !running.checkOnly || i === STEPS.length - 1).map((s, i) => (
                  <li key={s.label} className={cn("flex items-center gap-2", !running.checkOnly && i > step && "text-ink-4")}>
                    {running.checkOnly ? <Loader2 className="size-4 animate-spin text-accent" /> : i < step ? <CircleCheck className="size-4 text-success" /> : i === step ? <Loader2 className="size-4 animate-spin text-accent" /> : <CircleDashed className="size-4" />}
                    {s.label}
                  </li>
                ))}
              </ol>
              {running.message && <p className="mt-2.5 text-xs text-ink-3">{running.message}</p>}
            </Panel>
          )}
        </div>

        <Panel className="min-h-[420px]">
          <PanelHeader
            title={pages.length ? `${project.title} · ${pages.length} pages` : "Your magazine"}
            description={done ?? (pages.length ? "The current pages of this project." : undefined)}
            actions={
              pages.length > 0 && !running ? (
                <div className="flex gap-1.5">
                  <Button variant="secondary" size="sm" onClick={check} title="An AI art director looks at every page, fixes what's broken and notes anything it couldn't">
                    <ShieldCheck /> Check and fix pages
                  </Button>
                  <Button variant="secondary" size="sm" onClick={() => router.push("/app/designer/editor")}>
                    <PanelsTopLeft /> Open in page editor
                  </Button>
                  <Button variant="primary" size="sm" asChild>
                    <Link href="/app/exports">
                      <Download /> Export
                    </Link>
                  </Button>
                </div>
              ) : undefined
            }
          />
          {pages.length && profile ? (
            <div className="grid grid-cols-2 gap-x-3 gap-y-4 px-4 pb-5 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-5">
              {pages.map((p, i) => (
                <button key={p.id} onClick={() => router.push(`/app/designer/editor?page=${p.id}`)} className="flex flex-col items-center gap-1.5 text-left" title="Open in the page editor">
                  <span className={cn("block shadow-sm outline-offset-2 hover:outline hover:outline-2 hover:outline-accent", running && "opacity-50")}>
                    <PageThumb page={p} profile={profile} assets={assetsById} ctx={data.ctxFor(i)} width={150} localUrls={data.localUrls} />
                  </span>
                  <span className="w-full truncate text-center text-[11px] text-ink-3">
                    <span className="tabular font-medium text-ink-2">{i + 1}</span> · {p.label}
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <EmptyState
              icon={running ? <Loader2 className="animate-spin" /> : <PackageOpen />}
              title={running ? "Your magazine is being built" : "Nothing built yet"}
              description={running ? "Pages appear here when the build finishes." : "Check the list on the left, then build. The cover, contents and every article page arrive here, ready to export or edit."}
              action={!running && !transcribed.length ? <Button variant="secondary" asChild><Link href="/app/media"><Clapperboard /> Add videos</Link></Button> : !running && !usableRefs.length && !own ? <Button variant="secondary" asChild><Link href="/app/references"><LibraryBig /> Add a reference</Link></Button> : undefined}
            />
          )}
          {pages.length > 0 && !running && (
            <div className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3 text-xs text-ink-3">
              <Badge tone="warning" dot>AI drafts</Badge>
              Articles are marked as written by the AI Builder. Review them in Content Studio before final print export. Anything the page check couldn't fix is listed in that page&apos;s notes in the page editor.
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}
