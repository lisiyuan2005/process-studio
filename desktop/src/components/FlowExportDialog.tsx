import { FileSpreadsheet, X } from "lucide-react";
import { useState } from "react";
import type { FlowColumn, FlowExportFormat } from "../types";

const STORAGE_KEY = "process-studio:flow-columns";

/** Columns a flow table starts with: the step and what it does to the wafer. */
const DEFAULT_COLUMNS = [
  "index",
  "name",
  "type",
  "tool",
  "material",
  "mode",
  "target",
  "directional_fraction",
  "mask",
  "keep",
  "rates",
  "stops",
  "other",
  "enabled",
  "status",
  "loop",
];

function remembered(): string[] | null {
  try {
    const raw = globalThis.localStorage?.getItem(STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    return Array.isArray(parsed) && parsed.every((item) => typeof item === "string")
      ? (parsed as string[])
      : null;
  } catch {
    return null;
  }
}

function remember(columns: string[]) {
  try {
    globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(columns));
  } catch {
    // Without storage the choice simply starts from the default next time.
  }
}

interface FlowExportDialogProps {
  format: FlowExportFormat;
  /** What this build can write, from `describe`. */
  columns: FlowColumn[];
  busy: boolean;
  onExport: (columns: string[]) => void;
  onClose: () => void;
}

/**
 * Which details a flow table carries.
 *
 * An export has a reader: a run sheet for the cleanroom wants the tool and
 * the recipe numbers, a review wants the mask and the status, a tool list
 * wants neither. So the columns are chosen here rather than fixed, and the
 * choice is remembered for the next export.
 */
export function FlowExportDialog({ format, columns, busy, onExport, onClose }: FlowExportDialogProps) {
  const offered = columns.length > 0 ? columns : DEFAULT_COLUMNS.map((id) => ({ id, label: id }));
  const [chosen, setChosen] = useState<string[]>(() => {
    const saved = remembered();
    const known = new Set(offered.map((column) => column.id));
    const kept = saved?.filter((id) => known.has(id)) ?? [];
    return kept.length > 0 ? kept : offered.map((column) => column.id);
  });

  const toggle = (id: string) =>
    setChosen((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );

  const ordered = offered.filter((column) => chosen.includes(column.id)).map((column) => column.id);

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Export the flow">
      <div className="modal-card export-modal">
        <header className="modal-header">
          <div>
            <span className="eyebrow">EXPORT · {format.toUpperCase()}</span>
            <h2>Which details to export</h2>
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <div className="modal-body">
          <p className="numerics-note">
            One row per step, in flow order. Columns are written in the order below, whichever ones
            you pick.
          </p>
          <div className="column-grid">
            {offered.map((column) => (
              <label key={column.id} className="column-choice">
                <input
                  type="checkbox"
                  checked={chosen.includes(column.id)}
                  onChange={() => toggle(column.id)}
                />
                <span>{column.label}</span>
              </label>
            ))}
          </div>
          <div className="chip-row">
            <button type="button" onClick={() => setChosen(offered.map((column) => column.id))}>
              All
            </button>
            <button type="button" onClick={() => setChosen(["index", "name", "type", "tool", "target"])}>
              Run sheet
            </button>
            <button type="button" onClick={() => setChosen(["index", "name", "type", "mask", "keep", "status"])}>
              Review
            </button>
          </div>
        </div>

        <div className="modal-actions">
          <span>
            {ordered.length === 0
              ? "Pick at least one column"
              : `${ordered.length} of ${offered.length} columns`}
          </span>
          <button type="button" className="secondary-button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="primary-button modal-save"
            disabled={busy || ordered.length === 0}
            onClick={() => {
              remember(ordered);
              onExport(ordered);
            }}
          >
            <FileSpreadsheet size={13} />
            Choose a file…
          </button>
        </div>
      </div>
    </div>
  );
}
