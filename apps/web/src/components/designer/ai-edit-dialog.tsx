"use client";

import * as React from "react";
import { Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/input";
import { Dialog, DialogContent } from "@/components/ui/overlays";

const EXAMPLES_PAGE = [
  "Make the headline bigger and use the accent colour",
  "Move the photo to the top half and put the headline under it",
  "Add a caption under the photo",
  "Set the body text in two columns",
  "Keep everything inside the margins",
];
const EXAMPLES_FRAME = ["Make this bigger", "Centre this on the page", "Change this to the accent colour", "Shorten this text"];

/** "Edit with AI": the designer describes a change; `onRun` resolves true once it has been applied. */
export function AiEditDialog({ open, onOpenChange, selectedName, onRun }: { open: boolean; onOpenChange: (o: boolean) => void; selectedName?: string; onRun: (instruction: string) => Promise<boolean> }) {
  const [text, setText] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const run = async () => {
    if (text.trim().length < 2 || busy) return;
    setBusy(true);
    try {
      if (await onRun(text.trim())) {
        setText("");
        onOpenChange(false);
      }
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <DialogContent
        title="Edit with AI"
        description={selectedName ? `Describe the change. “This” means the selected frame, “${selectedName}”.` : "Describe the change to this page in plain words."}
        footer={
          <>
            <span className="mr-auto text-xs text-ink-4">Applied as one step. Undo with ⌘Z.</span>
            <Button variant="secondary" disabled={busy} onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button variant="primary" loading={busy} disabled={text.trim().length < 2} onClick={run}>
              <Sparkles /> Apply
            </Button>
          </>
        }
      >
        <Textarea
          autoFocus
          rows={4}
          value={text}
          placeholder="e.g. Make the headline navy, put the photo across the top and add a caption under it"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void run();
          }}
          aria-label="Instruction"
        />
        <div className="mt-3 flex flex-wrap gap-1.5">
          {(selectedName ? EXAMPLES_FRAME : EXAMPLES_PAGE).map((x) => (
            <button key={x} type="button" disabled={busy} onClick={() => setText(x)} className="rounded-full border border-line px-2.5 py-1 text-xs text-ink-3 transition-colors hover:border-line-strong hover:text-ink disabled:opacity-50">
              {x}
            </button>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}
