import { LoaderCircle, TriangleAlert, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { GridDefinition, GridEstimate, GridPlan, KernelDescription, WindowBounds } from "../types";

interface GridEditorProps {
  grid: GridDefinition;
  kernel?: KernelDescription;
  resolutionUm: number | null;
  /** The slab kernel's XY arc sagitta when set apart from the z step. */
  resolutionXyUm: number | null;
  presetsNm: number[];
  maximumNodes: number | null;
  busy: boolean;
  onPlan: (targetSpacingNm: number, bounds: WindowBounds, xyNm: number | null) => Promise<GridPlan>;
  onApply: (targetSpacingNm: number, bounds: WindowBounds, xyNm: number | null) => void;
  onClose: () => void;
}

const AXES: [keyof WindowBounds, keyof WindowBounds, string][] = [
  ["xMin", "xMax", "x"],
  ["yMin", "yMax", "y"],
  ["zMin", "zMax", "z"],
];

function boundsOf(grid: GridDefinition): WindowBounds {
  return { xMin: grid.xMin, xMax: grid.xMax, yMin: grid.yMin, yMax: grid.yMax, zMin: grid.zMin, zMax: grid.zMax };
}

function sameBounds(a: WindowBounds, b: WindowBounds) {
  return AXES.every(([low, high]) => a[low] === b[low] && a[high] === b[high]);
}

const PRESET_LABELS: Record<number, string> = {
  25: "Draft",
  12.5: "Standard",
  10: "Standard",
  6.25: "Accurate",
  2: "Accurate",
};

function gigabytes(bytes: number) {
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

/** A kernel without a field reports the spacing alone, and costs no nodes. */
function fieldEstimate(plan: GridPlan | undefined): GridEstimate | undefined {
  if (!plan || plan.spacingRole !== "grid") return undefined;
  return plan.estimate as GridEstimate;
}

export function GridEditor({
  grid,
  kernel,
  resolutionUm,
  resolutionXyUm,
  presetsNm,
  maximumNodes,
  busy,
  onPlan,
  onApply,
  onClose,
}: GridEditorProps) {
  // On a gridless kernel the same number is the length geometry is resolved
  // at, not a lattice: there is no node count and no ceiling to respect.
  const onGrid = (kernel?.spacingRole ?? "grid") === "grid";
  const currentNm = (onGrid ? grid.spacingUm : resolutionUm ?? grid.spacingUm) * 1000;
  const [spacing, setSpacing] = useState(String(Number(currentNm.toFixed(4))));
  // The XY arc sagitta of the slab kernel: empty follows the z step.
  const [spacingXy, setSpacingXy] = useState(
    resolutionXyUm === null ? "" : String(Number((resolutionXyUm * 1000).toFixed(4))),
  );
  const xyNm = useMemo((): number | null | undefined => {
    if (onGrid || spacingXy.trim() === "") return null;
    const value = Number(spacingXy);
    return Number.isFinite(value) && value > 0 ? value : undefined;
  }, [onGrid, spacingXy]);
  // The window as text, so a half-typed "-0." does not snap to a number.
  const [window_, setWindow] = useState<Record<keyof WindowBounds, string>>(() => {
    const current = boundsOf(grid);
    return Object.fromEntries(
      Object.entries(current).map(([key, value]) => [key, String(value)]),
    ) as Record<keyof WindowBounds, string>;
  });
  const bounds = useMemo((): WindowBounds | null => {
    const parsed = Object.fromEntries(
      Object.entries(window_).map(([key, value]) => [key, Number(value)]),
    ) as unknown as WindowBounds;
    return Object.values(parsed).every((value) => Number.isFinite(value)) ? parsed : null;
  }, [window_]);
  const windowChanged = bounds !== null && !sameBounds(bounds, boundsOf(grid));
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
    if (!bounds) {
      setPlan(undefined);
      setError("Every window bound needs a number, in micrometres.");
      return;
    }
    if (xyNm === undefined) {
      setPlan(undefined);
      setError("The XY arc value needs a positive number in nanometres, or nothing to follow the z step.");
      return;
    }
    let cancelled = false;
    const handle = globalThis.setTimeout(async () => {
      setPlanning(true);
      try {
        const result = await onPlan(value, bounds, xyNm);
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
      globalThis.clearTimeout(handle);
    };
  }, [spacing, bounds, xyNm, onPlan]);

  const estimate = fieldEstimate(plan);
  const spacingNm = plan?.estimate.spacingNm;
  const applicable = !!plan && !!bounds && plan.withinLimit && (!plan.unchanged || windowChanged) && !busy;

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Simulation grid">
      <div className="modal-card grid-modal">
        <header className="modal-header">
          <div>
            <span className="eyebrow">NUMERICS</span>
            <h2>{onGrid ? "Simulation grid" : "Geometry resolution"}</h2>
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <div className="modal-body">
          <span className="section-label">
            {onGrid ? "TARGET SPACING" : "TARGET RESOLUTION"}
          </span>
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
            <span>{onGrid ? "Spacing" : "Resolution"}</span>
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
              {onGrid
                ? "Project bounds are preserved, so the closest spacing that divides every extent is used. This is the solver grid, not image resolution."
                : `${kernel?.name ?? "This kernel"} keeps exact geometry and has no grid. This is the z step it samples a conformal film or an isotropic etch in: the height of the staircase on a rounded shoulder. Planar deposition, vertical etching and CMP are exact whatever it is.`}
            </small>
          </label>

          {!onGrid && (
            <label className="field-row">
              <span>XY arcs</span>
              <span className="number-input-wrap">
                <input
                  type="text"
                  inputMode="decimal"
                  placeholder="same as z"
                  value={spacingXy}
                  onChange={(event) => setSpacingXy(event.target.value)}
                />
                <span>nm</span>
              </span>
              <small>
                How far a rounded corner in plan may deviate from a true arc: the chord sagitta,
                which sets the vertex count of every ring. Leave it empty to follow the z step.
                A fine z step with a coarser XY value shrinks the staircase without making every
                ring more expensive.
              </small>
            </label>
          )}

          <span className="section-label">PROJECT WINDOW (µm)</span>
          <div className="window-grid">
            {AXES.map(([low, high, axis]) => (
              <label key={axis} className="field-row">
                <span>{axis} range</span>
                <span className="pair">
                  <input
                    type="text"
                    inputMode="decimal"
                    value={window_[low]}
                    onChange={(event) => setWindow({ ...window_, [low]: event.target.value })}
                  />
                  <input
                    type="text"
                    inputMode="decimal"
                    value={window_[high]}
                    onChange={(event) => setWindow({ ...window_, [high]: event.target.value })}
                  />
                </span>
              </label>
            ))}
          </div>
          <small className="window-note">
            The wafer surface is z = 0: below it is substrate down to z min, above it is room for
            what the flow builds. Make z max taller for a thicker stack.
            {onGrid ? "" : " The slab wafer is as thick as the window is deep."}
          </small>

          <dl className="grid-summary">
            <dt>Current</dt>
            <dd>
              {onGrid
                ? `${currentNm.toFixed(3)} nm · ${grid.nx}×${grid.ny}×${grid.nz}`
                : `z ${currentNm.toFixed(3)} nm · XY ${((resolutionXyUm ?? currentNm / 1000) * 1000).toFixed(3)} nm`}
            </dd>
            <dt>Proposed</dt>
            <dd>
              {planning ? (
                <LoaderCircle className="spin" size={11} />
              ) : spacingNm === undefined ? (
                "—"
              ) : estimate ? (
                `${estimate.spacingNm.toFixed(3)} nm · ${estimate.shape.join("×")}`
              ) : (
                `z ${spacingNm.toFixed(3)} nm · XY ${(
                  (plan?.estimate as { spacingXyNm?: number } | undefined)?.spacingXyNm ?? spacingNm
                ).toFixed(3)} nm`
              )}
            </dd>
            {onGrid && (
              <>
                <dt>Nodes</dt>
                <dd>{estimate ? estimate.nodeCount.toLocaleString() : "—"}</dd>
                <dt>Saved state</dt>
                <dd>{estimate ? gigabytes(estimate.stateBytes) : "—"}</dd>
                <dt>Memory to run</dt>
                <dd>{estimate ? gigabytes(estimate.recommendedBytes) : "—"}</dd>
              </>
            )}
          </dl>

          {error && (
            <div className="error-box">
              <TriangleAlert size={13} />
              <span>{error}</span>
            </div>
          )}

          {plan && !plan.withinLimit && estimate && (
            <div className="error-box">
              <TriangleAlert size={13} />
              <span>
                This grid needs {estimate.nodeCount.toLocaleString()} nodes; the ceiling is{" "}
                {(maximumNodes ?? 0).toLocaleString()}. Use a coarser spacing or smaller project
                bounds.
              </span>
            </div>
          )}

          {plan?.unchanged && !windowChanged && (
            <p className="numerics-note">
              This is the {onGrid ? "grid" : "resolution"} already in use.
            </p>
          )}

          <div className="warning-box">
            <TriangleAlert size={13} />
            <span>
              {onGrid
                ? "Changing the grid discards every stored result: the flow is replayed from the bare wafer on the new grid rather than interpolating the old one."
                : "Changing the resolution discards every stored result: the flow is replayed from the bare wafer at the new resolution."}
            </span>
          </div>
        </div>

        <div className="modal-actions">
          <span>
            {applicable
              ? "Ready to apply"
              : onGrid
                ? "Pick a spacing that fits"
                : "Pick a different resolution"}
          </span>
          <button type="button" className="secondary-button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="primary-button modal-save"
            disabled={!applicable}
            onClick={() => bounds && xyNm !== undefined && onApply(Number(spacing), bounds, xyNm)}
          >
            {onGrid ? "Apply grid" : "Apply resolution"}
          </button>
        </div>
      </div>
    </div>
  );
}
