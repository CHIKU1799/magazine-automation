"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { BookOpen, ChevronLeft, ChevronRight, Columns2, Download, Eye, Frame, Grid3x3, Image as ImageIcon, ImagePlus, Layers, LayoutTemplate, Lock, PanelLeft, PackageOpen, PanelsTopLeft, Redo2, Save, Sparkles, Square, Trash2, Type, Undo2, ZoomIn, ZoomOut } from "lucide-react";
import { toast } from "sonner";
import { useWorkspace } from "@/lib/store/workspace";
import { useProjectData } from "@/lib/store/hooks";
import { Button } from "@/components/ui/button";
import { Badge, EmptyState, Panel } from "@/components/ui/display";
import { Dialog, DialogContent, Select, useConfirm } from "@/components/ui/overlays";
import { Tabs, TabsContent, TabsList, TabsTrigger, Tooltip } from "@/components/ui/primitives";
import { Field, Textarea } from "@/components/ui/input";
import { EditorCanvas } from "@/components/designer/editor-canvas";
import { AssetPicker, ElementPanel, LayersList, fitTextInDom } from "@/components/designer/editor-panels";
import { MagazineReader } from "@/components/designer/magazine-reader";
import { AiEditDialog } from "@/components/designer/ai-edit-dialog";
import { applyAiOperations } from "@/lib/designer/ai-edit";
import { PageThumb } from "@/components/magazine/page-thumb";
import { PageView } from "@/components/magazine/page-view";
import { TemplatePreview } from "@/components/designer/template-preview";
import { designStatus } from "@/lib/domain/status";
import type { Asset, DesignStatus, PageElement } from "@/lib/domain/types";
import { specsForPageType } from "@/lib/templates/specs";
import { pageTypeLabel } from "@/lib/templates/derive";
import { liveArea } from "@/lib/templates/geometry";
import { live } from "@/lib/api/live";
import { ensureTemplateLogos } from "@/lib/api/live-sync";
import { AddImages, aspectFor } from "@/components/media/add-images";
import { useApiStatus } from "@/lib/api/use-api-status";
import { uid } from "@/lib/utils/format";
import { cn } from "@/lib/utils/cn";

function Editor() {
  const router = useRouter();
  const params = useSearchParams();
  const confirm = useConfirm();
  const data = useProjectData();
  const { project, profile, pages, assetsById } = data;
  const updateElement = useWorkspace((s) => s.updateElement);
  const updatePage = useWorkspace((s) => s.updatePage);
  const addElement = useWorkspace((s) => s.addElement);
  const removeElement = useWorkspace((s) => s.removeElement);
  const changeTemplate = useWorkspace((s) => s.changeTemplate);
  const pushHistory = useWorkspace((s) => s.pushHistory);
  const undo = useWorkspace((s) => s.undo);
  const redo = useWorkspace((s) => s.redo);
  const history = useWorkspace((s) => (project ? s.history[project.id] : undefined));
  const templates = useWorkspace((s) => s.templates);
  const mode = useWorkspace((s) => s.mode);
  const api = useApiStatus();
  const pageId = params.get("page") ?? pages[0]?.id;
  const index = pages.findIndex((p) => p.id === pageId);
  const page = pages[index];
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [zoom, setZoom] = React.useState<number | "fit">("fit");
  const [showGuides, setShowGuides] = React.useState(true);
  const [showBleed, setShowBleed] = React.useState(false);
  const [spread, setSpread] = React.useState(false);
  const [editingText, setEditingText] = React.useState<string | null>(null);
  const [cropping, setCropping] = React.useState<string | null>(null);
  const [overset, setOverset] = React.useState<Record<string, boolean>>({});
  const [picker, setPicker] = React.useState<null | "replace" | "add" | "background">(null);
  const [reader, setReader] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [exportingIdml, setExportingIdml] = React.useState(false);
  const [leftOpen, setLeftOpen] = React.useState(true);
  const [aiOpen, setAiOpen] = React.useState(false);
  const stage = React.useRef<HTMLDivElement>(null);
  const [stageSize, setStageSize] = React.useState({ w: 900, h: 700 });

  // On narrower windows the page needs the room; the panel stays one click away in the toolbar.
  React.useEffect(() => {
    if (window.innerWidth < 1280) setLeftOpen(false);
  }, []);

  React.useEffect(() => {
    const el = stage.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setStageSize({ w: entry.contentRect.width, h: entry.contentRect.height }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  React.useEffect(() => {
    setSelectedId(null);
    setEditingText(null);
    setCropping(null);
    setOverset({});
  }, [pageId]);

  const facingIndex = index <= 0 ? -1 : index % 2 === 1 ? index + 1 : index - 1;
  const facing = spread && facingIndex >= 0 ? pages[facingIndex] : undefined;
  const fitScale = profile ? Math.min((stageSize.h - 64) / profile.page.heightPt, (stageSize.w - 96) / (profile.page.widthPt * (facing ? 2 : 1))) : 1;
  const scale = zoom === "fit" ? Math.max(0.2, fitScale) : zoom;
  const selected = page?.elements.find((e) => e.id === selectedId);
  const go = React.useCallback((i: number) => pages[i] && router.replace(`/app/designer/editor?page=${pages[i].id}`), [pages, router]);

  const onOverset = React.useCallback((id: string, over: boolean) => setOverset((m) => (m[id] === over ? m : { ...m, [id]: over })), []);
  const change = React.useCallback((id: string, patch: Partial<PageElement>, withHistory = false) => page && updateElement(page.id, id, patch, { history: withHistory }), [page, updateElement]);

  const save = async () => {
    if (!project) return;
    if (mode === "live" && api.online) {
      setSaving(true);
      try {
        await live.savePages(project.id, pages);
        toast.success("Saved to PressFlow");
      } catch (err) {
        toast.error((err as Error).message);
      } finally {
        setSaving(false);
      }
    } else toast.success("Saved in this browser", { description: "Demo changes are stored locally and persist across reloads." });
  };

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!project || !page) return;
      const typing = (e.target as HTMLElement)?.closest("input,textarea,[contenteditable]");
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "s") {
        e.preventDefault();
        void save();
        return;
      }
      if (typing) return;
      if (mod && e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey) redo(project.id);
        else undo(project.id);
        return;
      }
      if (e.key === "Escape") {
        if (cropping) setCropping(null);
        else setSelectedId(null);
        return;
      }
      if (e.key === "=" || e.key === "+") setZoom((z) => Math.min(3, (z === "fit" ? scale : z) * 1.2));
      if (e.key === "-") setZoom((z) => Math.max(0.2, (z === "fit" ? scale : z) / 1.2));
      if (e.key === "0") setZoom("fit");
      if (e.key === "PageDown" || (e.key === "ArrowRight" && !selected)) go(index + 1);
      if (e.key === "PageUp" || (e.key === "ArrowLeft" && !selected)) go(index - 1);
      if (!selected || selected.locked) return;
      if (e.key === "Backspace" || e.key === "Delete") {
        e.preventDefault();
        removeElement(page.id, selected.id);
        setSelectedId(null);
      }
      if (mod && e.key.toLowerCase() === "d") {
        e.preventDefault();
        duplicate(selected);
      }
      const step = e.shiftKey ? 10 : 1;
      const nudge: Record<string, [number, number]> = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
      if (nudge[e.key]) {
        e.preventDefault();
        updateElement(page.id, selected.id, { x: selected.x + nudge[e.key][0], y: selected.y + nudge[e.key][1] });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (!project || !profile) {
    return (
      <div className="mx-auto max-w-[900px] px-6 py-16">
        <Panel>
          <EmptyState icon={<PanelsTopLeft />} title="Nothing to edit yet" description="This project has no design profile or pages. Plan pages in the flatplan first." action={<Button variant="primary" asChild><Link href="/app/designer">Open flatplan</Link></Button>} />
        </Panel>
      </div>
    );
  }
  if (!page) {
    return (
      <div className="mx-auto max-w-[900px] px-6 py-16">
        <Panel>
          <EmptyState icon={<PanelsTopLeft />} title="Page not found" action={<Button variant="secondary" asChild><Link href="/app/designer">Back to flatplan</Link></Button>} />
        </Panel>
      </div>
    );
  }

  const duplicate = (el: PageElement) => {
    const copy = { ...el, id: uid("el"), name: `${el.name} copy`, x: el.x + 12, y: el.y + 12, z: Math.max(...page.elements.map((e) => e.z)) + 1, locked: false, dynamic: undefined };
    addElement(page.id, copy);
    setSelectedId(copy.id);
  };
  const oversetCount = Object.values(overset).filter(Boolean).length;
  const article = data.articles.find((a) => a.id === page.articleId);
  // Starting points for "Generate with AI": described from the page's article, shaped like the selected frame.
  const imagePrompt = article
    ? `Editorial photograph for a magazine article titled "${article.headline.replace(/[“”"]/g, "")}". ${(article.intro ?? "").slice(0, 300)}`
    : page.pageType === "cover"
      ? `Striking cover image for "${data.project?.title ?? "the magazine"}", about ${data.project?.eventName ?? "the event"}. Leave calm space in the upper third for the masthead and cover lines.`
      : "";
  const imageAspect = selected?.kind === "image" ? aspectFor(selected.w, selected.h) : page.pageType === "cover" || page.pageType === "full_page_image" ? "portrait" : "landscape";
  const nextZ = () => Math.max(0, ...page.elements.map((e) => e.z)) + 1;
  const placeAsset = (a: Asset) => {
    if (selected && selected.id === background?.id) {
      placeBackground(a);
    } else if (selected?.kind === "image" && !selected.locked) {
      change(selected.id, { assetId: a.id, crop: { scale: 1, offsetX: 0, offsetY: 0 } }, true);
      toast.success(selected.assetId ? "Image replaced" : "Image placed");
    } else {
      const w = profile.page.widthPt / 3;
      const el: PageElement = { id: uid("el"), kind: "image", role: "image", name: "Image", x: (profile.page.widthPt - w) / 2, y: profile.page.heightPt / 3, w, h: w * 0.66, z: nextZ(), assetId: a.id, crop: { scale: 1, offsetX: 0, offsetY: 0 } };
      addElement(page.id, el);
      setSelectedId(el.id);
      toast.success("Image placed — drag to position");
    }
  };
  // The page background: a full-bleed picture under everything (frames copied from a reference have one).
  const background = page.elements.find((e) => e.kind === "image" && e.role === "background" && e.w >= profile.page.widthPt);
  const placeBackground = (a: Asset) => {
    const crop = { scale: 1, offsetX: 0, offsetY: 0 };
    if (background) {
      change(background.id, { assetId: a.id, crop, locked: false }, true);
    } else {
      const b = profile.page.bleedPt;
      addElement(page.id, { id: uid("el"), kind: "image", role: "background", name: "Background", x: -b, y: -b, w: profile.page.widthPt + 2 * b, h: profile.page.heightPt + 2 * b, z: Math.min(0, ...page.elements.map((e) => e.z)) - 1, assetId: a.id, crop });
    }
    toast.success(background ? "Background replaced" : "Background added", { description: "It fills the page and its bleed, under every other frame." });
  };
  const pick = (a: Asset) => (picker === "background" ? placeBackground(a) : placeAsset(a));
  const aiReady = mode === "live" && api.online;
  // The API writes the package from the saved pages, so unsaved edits are saved first.
  const exportIdml = async () => {
    const id = `idml-${project.id}`;
    setExportingIdml(true);
    try {
      await live.savePages(project.id, pages);
      toast.loading("Building the InDesign package…", { id, duration: Infinity });
      const rec = await live.createExport(project.id, "idml", undefined);
      const started = Date.now();
      let latest = rec;
      while (["queued", "running"].includes(latest.status) && Date.now() - started < 10 * 60_000) {
        await new Promise((r) => setTimeout(r, 2000));
        latest = (await live.listExports(project.id)).find((e) => e.id === rec.id) ?? latest;
      }
      if (latest.status !== "complete") throw new Error(latest.message ?? `The export ${latest.status === "failed" ? "failed" : "didn't finish"}.`);
      window.location.href = latest.downloadUrl ?? `/api/v1/exports/${latest.id}/download`;
      toast.success("InDesign package downloaded", { id, description: "Unzip it, open the .idml in InDesign and choose File › Save As to get the .indd. README.txt lists the fonts it uses.", duration: 12000 });
    } catch (err) {
      toast.error("Couldn't build the InDesign package", { id, description: (err as Error).message });
    } finally {
      setExportingIdml(false);
    }
  };
  // The page as it stands (unsaved edits included) goes to the API; the validated operations come back as one undo step.
  const runAiEdit = async (instruction: string): Promise<boolean> => {
    try {
      const res = await live.aiEditPage(project.id, { page, pageNumber: index + 1, instruction, selectedId: selected?.id });
      const current = useWorkspace.getState().pages.find((p) => p.id === page.id) ?? page;
      const { elements, changed } = applyAiOperations(current.elements, res.operations, profile, index + 1);
      const notes = res.notes.join(" ");
      if (!changed) {
        toast.info(res.summary || "No changes were needed.", { description: notes || undefined });
        return true;
      }
      updatePage(page.id, { elements, designStatus: page.designStatus === "not_started" ? "in_progress" : page.designStatus });
      if (selectedId && !elements.some((e) => e.id === selectedId)) setSelectedId(null);
      toast.success(res.summary || `Applied ${changed} change${changed === 1 ? "" : "s"}`, { description: notes || undefined, duration: 8000, action: { label: "Undo", onClick: () => undo(project.id) } });
      return true;
    } catch (err) {
      toast.error("Edit with AI failed", { description: (err as Error).message });
      return false;
    }
  };
  // New frames start centred in the live area, on top of everything else.
  const insertFrame = (kind: "text" | "shape") => {
    const la = liveArea(profile, index + 1);
    const w = kind === "text" ? Math.round(la.w / 2) : 120;
    const h = kind === "text" ? 96 : 80;
    const at = { x: Math.round(la.x + (la.w - w) / 2), y: Math.round(la.y + (la.h - h) / 2), w, h, z: nextZ() };
    const el: PageElement =
      kind === "text"
        ? { id: uid("el"), kind: "text", role: "body", styleRole: "body", name: "Text frame", text: "Type your text here.", ...at }
        : { id: uid("el"), kind: "shape", role: "accent_bar", name: "Shape", fill: profile.colors.accent, ...at };
    addElement(page.id, el);
    setSelectedId(el.id);
    if (kind === "text") setEditingText(el.id);
  };

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar */}
      <div className="flex h-11 shrink-0 items-center gap-1 border-b border-line bg-surface px-2">
        <Tooltip content="Back to flatplan">
          <Button variant="ghost" size="icon-sm" asChild>
            <Link href="/app/designer" aria-label="Back to flatplan">
              <PanelsTopLeft />
            </Link>
          </Button>
        </Tooltip>
        <Toggle on={leftOpen} onClick={() => setLeftOpen((v) => !v)} label="Pages, layers, templates & photos" icon={<PanelLeft />} />
        <div className="mx-1 h-5 w-px bg-line" />
        <Button variant="ghost" size="icon-sm" disabled={index <= 0} onClick={() => go(index - 1)} aria-label="Previous page">
          <ChevronLeft />
        </Button>
        <span className="tabular min-w-[96px] text-center text-sm">
          Page <strong>{index + 1}</strong> of {pages.length}
        </span>
        <Button variant="ghost" size="icon-sm" disabled={index >= pages.length - 1} onClick={() => go(index + 1)} aria-label="Next page">
          <ChevronRight />
        </Button>
        <span className="ml-1 hidden max-w-[220px] truncate text-xs text-ink-3 lg:inline">{page.label}</span>
        {page.locked && <Badge tone="accent" icon={<Lock />} className="ml-1">Locked</Badge>}
        <div className="mx-1 h-5 w-px bg-line" />
        <Tooltip content="Undo" shortcut="⌘Z">
          <Button variant="ghost" size="icon-sm" disabled={!history?.past.length} onClick={() => undo(project.id)} aria-label="Undo">
            <Undo2 />
          </Button>
        </Tooltip>
        <Tooltip content="Redo" shortcut="⇧⌘Z">
          <Button variant="ghost" size="icon-sm" disabled={!history?.future.length} onClick={() => redo(project.id)} aria-label="Redo">
            <Redo2 />
          </Button>
        </Tooltip>
        <div className="mx-1 h-5 w-px bg-line" />
        <Tooltip content="Add a text frame">
          <Button variant="ghost" size="sm" disabled={page.locked} onClick={() => insertFrame("text")}>
            <Type /> <span className="hidden xl:inline">Text</span>
          </Button>
        </Tooltip>
        <Tooltip content={selected?.kind === "image" && !selected.locked ? "Place a photo in the selected frame" : "Add a photo — upload, generate or pick from the library"}>
          <Button variant="ghost" size="sm" disabled={page.locked} onClick={() => setPicker("add")}>
            <ImagePlus /> <span className="hidden xl:inline">Photo</span>
          </Button>
        </Tooltip>
        <Tooltip content="Add a shape">
          <Button variant="ghost" size="sm" disabled={page.locked} onClick={() => insertFrame("shape")}>
            <Square /> <span className="hidden xl:inline">Shape</span>
          </Button>
        </Tooltip>
        <div className="mx-1 h-5 w-px bg-line" />
        <Button variant="ghost" size="icon-sm" onClick={() => setZoom((z) => Math.max(0.2, (z === "fit" ? scale : z) / 1.2))} aria-label="Zoom out">
          <ZoomOut />
        </Button>
        <button className="tabular w-12 rounded px-1 py-0.5 text-center text-xs hover:bg-hover" onClick={() => setZoom("fit")} title="Fit to window (0)">
          {Math.round(scale * 100)}%
        </button>
        <Button variant="ghost" size="icon-sm" onClick={() => setZoom((z) => Math.min(3, (z === "fit" ? scale : z) * 1.2))} aria-label="Zoom in">
          <ZoomIn />
        </Button>
        <div className="mx-1 h-5 w-px bg-line" />
        <Toggle on={showGuides} onClick={() => setShowGuides((v) => !v)} label="Margins & columns" icon={<Grid3x3 />} />
        <Toggle on={showBleed} onClick={() => setShowBleed((v) => !v)} label="Show bleed" icon={<Frame />} />
        <Toggle on={spread} onClick={() => setSpread((v) => !v)} label="Spread preview" icon={<Columns2 />} />
        <div className="ml-auto flex items-center gap-1.5">
          {oversetCount > 0 && (
            <Badge tone="danger" dot>
              {oversetCount} overset
            </Badge>
          )}
          {page.editorialStatus !== "approved" && <Badge tone="warning" dot>Copy not approved</Badge>}
          <Button variant="ghost" size="sm" onClick={() => setReader(true)}>
            <Eye /> Preview
          </Button>
          <Button variant="secondary" size="sm" loading={saving} onClick={save}>
            <Save /> Save
          </Button>
          <Tooltip content={aiReady ? "Download every page as an InDesign package (.idml + pictures): open it in InDesign and save as .indd" : "Needs the PressFlow API"}>
            <span>
              <Button variant="secondary" size="sm" disabled={!aiReady} loading={exportingIdml} onClick={exportIdml}>
                <PackageOpen /> InDesign
              </Button>
            </span>
          </Tooltip>
          <Button variant="primary" size="sm" asChild>
            <Link href="/app/exports">
              <Download /> Export
            </Link>
          </Button>
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
        {/* Left panel */}
        <aside className={cn("w-[248px] shrink-0 flex-col border-r border-line bg-surface", leftOpen ? "flex" : "hidden")}>
          <Tabs defaultValue="pages" className="flex min-h-0 flex-1 flex-col">
            <TabsList className="px-1.5">
              <TabsTrigger value="pages" title="Pages"><BookOpen /></TabsTrigger>
              <TabsTrigger value="layers" title="Layers"><Layers /></TabsTrigger>
              <TabsTrigger value="templates" title="Templates"><LayoutTemplate /></TabsTrigger>
              <TabsTrigger value="assets" title="Assets"><ImageIcon /></TabsTrigger>
            </TabsList>
            <TabsContent value="pages" className="scroll-thin min-h-0 flex-1 overflow-y-auto p-3">
              <div className="grid grid-cols-2 gap-x-2 gap-y-3">
                {pages.map((p, i) => (
                  <button key={p.id} onClick={() => go(i)} className={cn("flex flex-col items-center gap-1", i === 0 && "col-start-2")}>
                    <span className={cn("block outline-offset-2", p.id === page.id ? "outline outline-2 outline-accent" : "hover:outline hover:outline-1 hover:outline-ink-4")}>
                      <PageThumb page={p} profile={profile} assets={assetsById} ctx={data.ctxFor(i)} width={96} localUrls={data.localUrls} />
                    </span>
                    <span className={cn("tabular text-[11px]", p.id === page.id ? "font-semibold text-accent" : "text-ink-3")}>{i + 1}</span>
                  </button>
                ))}
              </div>
            </TabsContent>
            <TabsContent value="layers" className="scroll-thin min-h-0 flex-1 overflow-y-auto">
              <LayersList page={page} selectedId={selectedId} onSelect={setSelectedId} onToggle={(id, patch) => updateElement(page.id, id, patch)} />
            </TabsContent>
            <TabsContent value="templates" className="scroll-thin min-h-0 flex-1 overflow-y-auto p-3">
              <p className="mb-3 text-xs text-ink-3">Templates for {pageTypeLabel(page.pageType).toLowerCase()} pages. Applying one re-composes this page{page.articleId ? " and re-flows the article" : ""}.</p>
              <div className="flex flex-col gap-3">
                {[...specsForPageType(page.pageType).map((s) => ({ id: s.id, name: s.name })), ...templates.filter((t) => t.pageType === page.pageType).map((t) => ({ id: t.id, name: `${t.name}` }))].map((t) => (
                  <button
                    key={t.id}
                    disabled={page.locked}
                    onClick={async () => {
                      if (t.id === page.templateId) return;
                      if (!(await confirm({ title: `Apply “${t.name}”?`, description: "Manual changes to frames on this page are replaced. You can undo this.", confirmLabel: "Apply template" }))) return;
                      await ensureTemplateLogos(project.id, templates.find((x) => x.id === t.id)).catch((err) => toast.error("Logos couldn't be prepared", { description: (err as Error).message }));
                      changeTemplate(page.id, t.id);
                      setSelectedId(null);
                    }}
                    className={cn("flex items-center gap-3 rounded-md border p-2 text-left transition-colors disabled:opacity-50", t.id === page.templateId ? "border-accent bg-accent-soft" : "border-line hover:border-line-strong")}
                  >
                    <TemplatePreview templateId={t.id} profile={profile} pageNumber={index + 1} width={56} />
                    <span className="text-xs font-medium leading-snug">{t.name}</span>
                  </button>
                ))}
              </div>
            </TabsContent>
            <TabsContent value="assets" className="scroll-thin min-h-0 flex-1 overflow-y-auto p-3">
              <div className="mb-3">
                <AddImages projectId={page.projectId} size="xs" defaultPrompt={imagePrompt} defaultAspect={imageAspect} sessionId={article?.sessionIds?.[0]} onAdded={placeAsset} />
              </div>
              <AssetPicker assets={data.assets} sessionHint={article?.sessionIds} onPick={placeAsset} />
            </TabsContent>
          </Tabs>
        </aside>

        {/* Canvas */}
        <div ref={stage} className="canvas-backdrop scroll-thin relative min-w-0 flex-1 overflow-auto" onPointerDown={() => { setSelectedId(null); setCropping(null); }}>
          <div className="flex min-h-full min-w-max items-center justify-center p-8">
            <div className="flex items-start gap-0">
              {facing && facingIndex < index && (
                <div className="pointer-events-none opacity-80">
                  <PageView page={facing} profile={profile} assets={assetsById} ctx={data.ctxFor(facingIndex)} scale={scale} localUrls={data.localUrls} className="bg-white shadow-page" />
                </div>
              )}
              <EditorCanvas
                page={page}
                profile={profile}
                assets={assetsById}
                ctx={data.ctxFor(index)}
                scale={scale}
                localUrls={data.localUrls}
                selectedId={selectedId}
                showGuides={showGuides}
                showBleed={showBleed}
                onSelect={setSelectedId}
                onBeginChange={() => pushHistory(project.id)}
                onChange={(id, patch) => change(id, patch)}
                onOverset={onOverset}
                editingText={editingText}
                setEditingText={setEditingText}
                cropping={cropping}
                setCropping={setCropping}
                onPlaceImage={(id) => {
                  setSelectedId(id);
                  setPicker("replace");
                }}
              />
              {facing && facingIndex > index && (
                <div className="pointer-events-none opacity-80">
                  <PageView page={facing} profile={profile} assets={assetsById} ctx={data.ctxFor(facingIndex)} scale={scale} localUrls={data.localUrls} className="bg-white shadow-page" />
                </div>
              )}
            </div>
          </div>
          <p className="pointer-events-none absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full bg-surface/90 px-3 py-1 text-[11px] text-ink-3 shadow-sm">
            HTML preview · “InDesign” downloads an .idml package to open in InDesign and save as .indd
          </p>
        </div>

        {/* Right panel */}
        <aside className="scroll-thin hidden w-[300px] shrink-0 overflow-y-auto border-l border-line bg-surface md:block">
          <div className="border-b border-line p-3">
            <Tooltip content={!aiReady ? "Needs the PressFlow API with an AI provider" : page.locked ? "Unlock the page to edit it" : selected ? `Describe a change to “${selected.name}” or the page` : "Describe a change to this page in plain words"}>
              <span className="block">
                <Button variant="primary" size="sm" className="w-full" disabled={!aiReady || page.locked} onClick={() => setAiOpen(true)}>
                  <Sparkles /> Edit with AI
                </Button>
              </span>
            </Tooltip>
          </div>
          {selected ? (
            <ElementPanel
              el={selected}
              page={page}
              profile={profile}
              assets={assetsById}
              overset={!!overset[selected.id]}
              onChange={(patch, withHistory = true) => change(selected.id, patch, withHistory)}
              onDelete={() => {
                removeElement(page.id, selected.id);
                setSelectedId(null);
              }}
              onDuplicate={() => duplicate(selected)}
              onReplaceImage={() => setPicker("replace")}
              onZ={(dir) => {
                const sorted = [...page.elements].sort((a, b) => a.z - b.z);
                const i = sorted.findIndex((e) => e.id === selected.id);
                const other = sorted[i + dir];
                if (!other) return;
                pushHistory(project.id);
                updateElement(page.id, selected.id, { z: other.z }, { history: false });
                updateElement(page.id, other.id, { z: selected.z }, { history: false });
              }}
              onFit={() => {
                const fit = fitTextInDom(page.id, selected, profile);
                if (!fit) return toast.error("Can't fit within 85% of the type size", { description: "Shorten the text, enlarge the frame, or add a page." });
                change(selected.id, { style: { ...selected.style, size: fit.size, leading: fit.leading } }, true);
                toast.success(`Fitted at ${fit.size} pt`);
              }}
            />
          ) : (
            <div className="flex flex-col gap-4 p-4">
              <div>
                <p className="text-sm font-semibold">Page {index + 1}</p>
                <p className="text-xs text-ink-3">{pageTypeLabel(page.pageType)} · {page.elements.length} frames</p>
              </div>
              <Field label="Design status">
                <Select value={page.designStatus} onValueChange={(v: DesignStatus) => updatePage(page.id, { designStatus: v })} options={(Object.keys(designStatus) as DesignStatus[]).map((k) => ({ value: k, label: designStatus[k].label }))} />
              </Field>
              {article && (
                <div className="rounded-md border border-line p-3">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-ink-4">Article</p>
                  <p className="mt-1 text-sm font-medium">{article.headline.replace(/[“”]/g, "")}</p>
                  <Button variant="link" size="xs" asChild>
                    <Link href={`/app/studio/${article.id}`}>Edit copy in Content Studio</Link>
                  </Button>
                </div>
              )}
              <div>
                <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-ink-4">Add to page</p>
                <div className="grid grid-cols-3 gap-1.5">
                  {(
                    [
                      ["Text", <Type key="t" />, () => insertFrame("text")],
                      ["Photo", <ImagePlus key="p" />, () => setPicker("add")],
                      ["Shape", <Square key="s" />, () => insertFrame("shape")],
                    ] as const
                  ).map(([label, icon, onClick]) => (
                    <button
                      key={label}
                      disabled={page.locked}
                      onClick={onClick}
                      className="flex flex-col items-center gap-1 rounded-md border border-line py-2.5 text-xs text-ink-2 transition-colors hover:border-line-strong hover:bg-hover disabled:opacity-50 [&_svg]:size-4 [&_svg]:text-ink-3"
                    >
                      {icon}
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-ink-4">Background</p>
                <div className="flex items-center gap-2">
                  {background ? (
                    <button onClick={() => setSelectedId(background.id)} className="h-12 w-9 shrink-0 overflow-hidden rounded-sm border border-line" title="Select the background">
                      {background.assetId && assetsById[background.assetId]?.url ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={assetsById[background.assetId]!.url} alt="" className="h-full w-full object-cover" />
                      ) : null}
                    </button>
                  ) : null}
                  <Button variant="secondary" size="sm" className="flex-1" disabled={page.locked} onClick={() => setPicker("background")}>
                    <ImageIcon /> {background ? "Replace background" : "Add background image"}
                  </Button>
                  {background && (
                    <Tooltip content="Remove the background">
                      <Button variant="ghost" size="icon-sm" disabled={page.locked} onClick={() => removeElement(page.id, background.id)} aria-label="Remove the background">
                        <Trash2 />
                      </Button>
                    </Tooltip>
                  )}
                </div>
              </div>
              <Field label="Notes" optional>
                <Textarea rows={3} value={page.notes ?? ""} onChange={(e) => updatePage(page.id, { notes: e.target.value }, { history: false })} onKeyDown={(e) => e.stopPropagation()} />
              </Field>
              <div className="rounded-md bg-subtle p-3 text-xs leading-relaxed text-ink-3">
                <p className="font-medium text-ink-2">Editing</p>
                Click a frame to select · drag to move (snaps to margins and columns; hold ⌥ to disable) · drag handles to resize · double-click text to edit · double-click an empty photo frame to place a photo, or a photo to crop it · arrows nudge (⇧ ×10) · ⌫ deletes.
              </div>
              <Button variant="secondary" size="sm" onClick={() => updatePage(page.id, { locked: !page.locked })}>
                <Lock /> {page.locked ? "Unlock page" : "Lock page"}
              </Button>
            </div>
          )}
        </aside>
      </div>

      <Dialog open={picker !== null} onOpenChange={(o) => !o && setPicker(null)}>
        <DialogContent
          title={picker === "background" ? (background ? "Replace the background" : "Add a background") : selected?.kind === "image" && !selected.locked ? (selected.assetId ? "Replace photograph" : "Place a photograph") : "Add a photograph"}
          description={picker === "background" ? "The picture fills the whole page and its bleed, under every other frame. Upload one, generate one with AI, or pick from this project's library." : "Upload your own, generate one with AI, or pick from this project's library."}
          size="lg"
        >
          <div className="mb-3">
            <AddImages
              projectId={page.projectId}
              defaultPrompt={imagePrompt}
              defaultAspect={picker === "background" ? "portrait" : imageAspect}
              sessionId={article?.sessionIds?.[0]}
              onAdded={(a) => {
                pick(a);
                setPicker(null);
              }}
            />
          </div>
          <AssetPicker
            assets={data.assets}
            sessionHint={picker === "background" ? undefined : article?.sessionIds}
            onPick={(a) => {
              pick(a);
              setPicker(null);
            }}
          />
        </DialogContent>
      </Dialog>

      <MagazineReader open={reader} onOpenChange={setReader} startIndex={index} />
      <AiEditDialog open={aiOpen} onOpenChange={setAiOpen} selectedName={selected?.name} onRun={runAiEdit} />
    </div>
  );
}

function Toggle({ on, onClick, label, icon }: { on: boolean; onClick: () => void; label: string; icon: React.ReactNode }) {
  return (
    <Tooltip content={label}>
      <Button variant={on ? "subtle" : "ghost"} size="icon-sm" onClick={onClick} aria-label={label} aria-pressed={on} className={on ? "text-accent" : undefined}>
        {icon}
      </Button>
    </Tooltip>
  );
}

export default function EditorPage() {
  return (
    <React.Suspense>
      <Editor />
    </React.Suspense>
  );
}

