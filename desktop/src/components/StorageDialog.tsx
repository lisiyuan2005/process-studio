import { HardDrive, LoaderCircle, X } from "lucide-react";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import type { TidyResult, WorkspaceStorage } from "../types";

export function megabytes(bytes: number): string {
  if (bytes < 1_000_000) return `${Math.round(bytes / 1000)} kB`;
  if (bytes < 10_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  if (bytes < 1_000_000_000) return `${Math.round(bytes / 1_000_000)} MB`;
  return `${(bytes / 1_000_000_000).toFixed(2)} GB`;
}

const FILM = { detailed: "Detailed", simplified: "Simplified" } as Record<string, string>;

/**
 * What the workspace takes on disk, and cleaning it up.
 *
 * Each step keeps one result per film model; what goes without asking is
 * what nothing needs (files of results that were replaced or deleted,
 * layouts an import replaced). The 3D meshes and the other film model's
 * results cost a rebuild or a rerun, so they go only when ticked.
 */
export function StorageDialog({
  busy,
  load,
  tidy,
  onClose,
}: {
  busy: boolean;
  load: () => Promise<WorkspaceStorage>;
  tidy: (options: { meshes: boolean; otherFidelity: boolean }) => Promise<TidyResult>;
  onClose: () => void;
}) {
  const [storage, setStorage] = useState<WorkspaceStorage | null>(null);
  const [error, setError] = useState("");
  const [meshes, setMeshes] = useState(false);
  const [otherFidelity, setOtherFidelity] = useState(false);
  const [working, setWorking] = useState(false);
  const [freed, setFreed] = useState<number | null>(null);
  useEffect(() => {
    load().then(setStorage, (reason) => setError(String(reason?.message ?? reason)));
  }, [load]);

  const other = storage?.fidelity === "detailed" ? "Simplified" : "Detailed";
  const always = storage ? storage.leftovers + storage.retired + storage.unusedLayouts : 0;
  const chosen = storage ? always + (meshes ? storage.meshes : 0) + (otherFidelity ? storage.otherFidelity : 0) : 0;
  const run = async () => {
    setWorking(true);
    setError("");
    try {
      const result = await tidy({ meshes, otherFidelity });
      setStorage(result.after);
      setFreed(result.freed);
      setMeshes(false);
      setOtherFidelity(false);
    } catch (reason) {
      setError(String((reason as Error)?.message ?? reason));
    } finally {
      setWorking(false);
    }
  };

  const row = (label: string, bytes: number, note: string, control?: React.ReactNode) => (
    <div className="storage-row">
      <div>
        <strong>{label}</strong>
        <small>{note}</small>
      </div>
      <span className="storage-size">{megabytes(bytes)}</span>
      <span className="storage-control">{control}</span>
    </div>
  );

  return createPortal(
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Workspace storage">
      <div className="modal-card storage-modal">
        <header className="modal-header">
          <div>
            <span className="eyebrow">STORAGE</span>
            <h2>What this workspace takes on disk{storage ? `: ${megabytes(storage.total)}` : ""}</h2>
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>
        <div className="modal-body storage-body">
          {!storage && !error && (
            <p className="field-hint">
              <LoaderCircle className="spin" size={13} /> Measuring…
            </p>
          )}
          {storage && (
            <>
              {row(
                `Results (${FILM[storage.fidelity] ?? storage.fidelity} films)`,
                storage.results,
                `${storage.counts.results} stored: one per step, the latest run of it. Kept.`,
              )}
              {row(
                `Results of the other film model (${other})`,
                storage.otherFidelity,
                `${storage.counts.otherFidelity} stored, for switching back without a rerun.`,
                <label>
                  <input
                    type="checkbox"
                    checked={otherFidelity}
                    disabled={storage.otherFidelity === 0}
                    onChange={(event) => setOtherFidelity(event.target.checked)}
                  />{" "}
                  Delete
                </label>,
              )}
              {row(
                "3D view meshes",
                storage.meshes,
                `${storage.counts.meshes} kept beside the results so a 3D view opens at once; built again when needed.`,
                <label>
                  <input
                    type="checkbox"
                    checked={meshes}
                    disabled={storage.meshes === 0}
                    onChange={(event) => setMeshes(event.target.checked)}
                  />{" "}
                  Delete
                </label>,
              )}
              {row(
                "Layouts",
                storage.layouts,
                "The GDS file the project uses.",
              )}
              {row(
                "Not needed any more",
                always,
                [
                  storage.unusedLayouts ? `${storage.counts.unusedLayouts} layout(s) an import replaced` : "",
                  storage.leftovers ? `${storage.counts.leftovers} file(s) of results that were replaced or deleted` : "",
                  storage.retired ? "results of a film model there no longer is" : "",
                ]
                  .filter(Boolean)
                  .join("; ") || "Nothing.",
                <span className="field-hint">{always ? "Always removed" : ""}</span>,
              )}
              {row("Project database", storage.database, "The flow, the libraries, the log.")}
              <p className="field-hint">
                What is not needed is also removed each time the workspace is opened. A layout removed here
                cannot be brought back with Undo.
              </p>
              {freed !== null && <p className="storage-freed">Freed {megabytes(freed)}.</p>}
            </>
          )}
          {error && <p className="invalid-note">{error}</p>}
        </div>
        <div className="modal-actions">
          <button type="button" className="secondary-button" onClick={onClose}>
            Close
          </button>
          <button
            type="button"
            className="modal-save primary-button"
            disabled={!storage || working || busy || chosen === 0}
            title={busy ? "Wait for the run to finish" : undefined}
            onClick={() => void run()}
          >
            <HardDrive size={13} /> {working ? "Cleaning up…" : `Free ${megabytes(chosen)}`}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
