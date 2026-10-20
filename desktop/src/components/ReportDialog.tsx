import { Bug, FolderOpen, Globe, X } from "lucide-react";
import { useState } from "react";
import { createPortal } from "react-dom";
import type { ProcessStep } from "../types";
import { PROCESS_LABELS } from "./StepList";

/**
 * Report a problem: what went wrong and at which steps.
 *
 * The window packs the workspace into a report the user saves, then opens
 * the repository's issue page filled in, and the user drags the report in.
 * Nothing is sent from here: the issue is posted by the user, from their
 * own GitHub account, and they see everything before it goes.
 */
export function ReportDialog({
  steps,
  flow,
  initialIds,
  busy,
  onSubmit,
  onReveal,
  onReopen,
  onClose,
}: {
  steps: ProcessStep[];
  flow: "simulation" | "experiment";
  initialIds: string[];
  busy: boolean;
  /** Save the report and open the issue page; the saved path, or null when cancelled. */
  onSubmit: (description: string, stepIds: string[]) => Promise<string | null>;
  onReveal: (path: string) => void;
  onReopen: () => void;
  onClose: () => void;
}) {
  const [description, setDescription] = useState("");
  const [marked, setMarked] = useState<Set<string>>(() => new Set(initialIds));
  const [saved, setSaved] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const toggle = (id: string) =>
    setMarked((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const submit = async () => {
    setWorking(true);
    try {
      const path = await onSubmit(description, steps.filter((step) => marked.has(step.id)).map((step) => step.id));
      if (path) setSaved(path);
    } finally {
      setWorking(false);
    }
  };

  return createPortal(
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Report a problem">
      <div className="modal-card report-modal">
        <header className="modal-header">
          <div>
            <span className="eyebrow">REPORT</span>
            <h2>Report a problem</h2>
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>
        {saved ? (
          <div className="modal-body report-done">
            <p>
              The report is saved as <code>{saved}</code>, and the issue page is open in your browser with the
              problem filled in. <strong>Drag the report into the issue's text box</strong>, check what it says, and
              submit it.
            </p>
            <div className="report-again">
              <button type="button" className="secondary-button" onClick={() => onReveal(saved)}>
                <FolderOpen size={13} /> Show the report
              </button>
              <button type="button" className="secondary-button" onClick={onReopen}>
                <Globe size={13} /> Open the issue page again
              </button>
            </div>
          </div>
        ) : (
          <div className="modal-body report-form">
            <label className="field-row">
              <span>What went wrong</span>
              <textarea
                autoFocus
                rows={4}
                value={description}
                placeholder="What you expected, and what you got instead"
                onChange={(event) => setDescription(event.target.value)}
              />
            </label>
            <span className="section-label">
              STEPS WHERE IT GOES WRONG · {flow === "experiment" ? "EXPERIMENT RECORD" : "THIS BRANCH"}
            </span>
            <div className="report-steps">
              {steps.length === 0 && <p className="field-hint">This flow has no steps.</p>}
              {steps.map((step, index) => (
                <label key={step.id} className={marked.has(step.id) ? "marked" : ""}>
                  <input type="checkbox" checked={marked.has(step.id)} onChange={() => toggle(step.id)} />
                  <span className="step-index">{index + 1}</span>
                  <span className="report-step-name">{step.name}</span>
                  <span className="report-step-type">
                    {PROCESS_LABELS[step.processType] ?? step.processType}
                    {step.loop ? ` · ${step.loop.name} ${step.loop.iteration + 1}/${step.loop.repeat}` : ""}
                  </span>
                </label>
              ))}
            </div>
            <p className="report-notice">
              The report holds this workspace as a flow file (both flows, the materials, the sketches, the window
              and the resolution), the GDS layout if there is one, the worker log and this build's version. The
              issue and the file you attach to it are <strong>public</strong>: anyone can read them. Leave out a
              flow that must stay private. Posting needs a GitHub account.
            </p>
          </div>
        )}
        <div className="modal-actions">
          <button type="button" className="secondary-button" onClick={onClose}>
            {saved ? "Done" : "Cancel"}
          </button>
          {!saved && (
            <button
              type="button"
              className="modal-save primary-button"
              disabled={working || busy || (description.trim() === "" && marked.size === 0)}
              title={busy ? "Wait for the run to finish" : undefined}
              onClick={() => void submit()}
            >
              <Bug size={13} /> Save the report and open the issue page
            </button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
