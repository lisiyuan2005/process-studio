import { LoaderCircle, TriangleAlert, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { GridDefinition, GridPlan } from "../types";

interface GridEditorProps {
  grid: GridDefinition;
  presetsNm: number[];
  maximumNodes: number;
  busy: boolean;
  onPlan: (targetSpacingNm: number) => Promise<GridPlan>;
  onApply: (targetSpacingNm: number) => void;
  onClose: () => void;
}

const PRESET_LABELS: Record<number, string> = {
  25: "Draft",
  12.5: "Standard",
  6.25: "Accurate",
};

function gigabytes(bytes: number) {
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

export function GridEditor({
  grid,
  presetsNm,
  maximumNodes,
  busy,
  onPlan,
  onApply,
  onClose,
}: GridEditorProps) {
  const currentNm = grid.spacingUm * 1000;
  const [spacing, setSpacing] = useState(String(Number(currentNm.toFixed(4))));
  const [plan, setPlan] = useState<GridPlan>();
  const [planning, setPlanning] = useState(false);
  const [error, setError] = useState<string>();

  // The spacing has to divide every project extent, so the worker searches for
  // the nearest lattice that does. Ask it on every edit instead of guessing.
  useEffect(() => {
    const value = Number(spacing);
    if (!Number.isFinite(value) || value <= 0) {
      setPlan(undefined);
      setError("Enter a spacing in nanometres.");
      return;
    }
    let cancelled = false;
    const handle = window.setTimeout(async () => {
      setPlanning(true);
      try {
        const result = await onPlan(value);
        if (cancelled) return;
        setPlan(result);
        setError(undefined);
      } catch (reason) {
        if (cancelled) return;
        setPlan(undefined);
        setError(reason instanceof Error ? reason.message : String(reason));
      } finally {
        if (!cancelled) setPlanning(false);
      }
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [spacing, onPlan]);

  const estimate = plan?.estimate;
  const applicable = !!plan && plan.withinLimit && !plan.unchanged && !busy;

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Simulation grid">
      <div className="modal-card grid-modal">
        <header className="modal-header">
          <div>
            <span className="eyebrow">NUMERICS</span>
            <h2>Simulation grid</h2>
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <div className="modal-body">
          <span className="section-label">TARGET SPACING</span>
          <div className="chip-row">
            {presetsNm.map((preset) => (
              <button
                key={preset}
                type="button"
                className={Number(spacing) === preset ? "active" : ""}
                onClick={() => setSpacing(String(preset))}
              >
                {PRESET_LABELS[preset] ?? "Preset"} — {preset} nm
              </button>
            ))}
          </div>

          <label className="field-row">
            <span>Spacing</span>
            <span className="number-input-wrap">
              <input
                autoFocus
                type="text"
                inputMode="decimal"
                value={spacing}
                onChange={(event) => setSpacing(event.target.value)}
              />
              <span>nm</span>
            </span>
            <small>
              Project bounds are preserved, so the closest spacing that divides every extent is
              used. This is the solver grid, not image resolution.
            </small>
          </label>

          <dl className="grid-summary">
            <dt>Current</dt>
            <dd>
              {currentNm.toFixed(3)} nm · {grid.nx}×{grid.ny}×{grid.nz}
            </dd>
            <dt>Proposed</dt>
            <dd>
              {planning ? (
                <LoaderCircle className="spin" size={11} />
              ) : estimate ? (
                `${estimate.spacingNm.toFixed(3)} nm · ${estimate.shape.join("×")}`
              ) : (
                "—"
              )}
            </dd>
            <dt>Nodes</dt>
            <dd>{estimate ? estimate.nodeCount.toLocaleString() : "—"}</dd>
            <dt>Saved state</dt>
            <dd>{estimate ? gigabytes(estimate.stateBytes) : "—"}</dd>
            <dt>Memory to run</dt>
            <dd>{estimate ? gigabytes(estimate.recommendedBytes) : "—"}</dd>
          </dl>

          {error && (
            <div className="error-box">
              <TriangleAlert size={13} />
              <span>{error}</span>
            </div>
          )}

          {plan && !plan.withinLimit && (
            <div className="error-box">
              <TriangleAlert size={13} />
              <span>
                This grid needs {plan.estimate.nodeCount.toLocaleString()} nodes; the ceiling is{" "}
                {maximumNodes.toLocaleString()}. Use a coarser spacing or smaller project bounds.
              </span>
            </div>
          )}

          {plan?.unchanged && <p className="numerics-note">This is the grid already in use.</p>}

          <div className="warning-box">
            <TriangleAlert size={13} />
            <span>
              Changing the grid discards every stored result: the flow is replayed from the bare
              wafer on the new grid rather than interpolating the old one.
            </span>
          </div>
        </div>

        <div className="modal-actions">
          <span>{applicable ? "Ready to apply" : "Pick a spacing that fits"}</span>
          <button type="button" className="secondary-button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="primary-button modal-save"
            disabled={!applicable}
            onClick={() => onApply(Number(spacing))}
          >
            Apply grid
          </button>
        </div>
      </div>
    </div>
  );
}
