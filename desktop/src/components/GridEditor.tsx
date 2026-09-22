import { LoaderCircle, TriangleAlert, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { UnitNumber } from "./UnitNumber";
import type { GridDefinition, GridPlan, KernelDescription, WindowBounds } from "../types";

interface GridEditorProps {
  grid: GridDefinition;
  kernel?: KernelDescription;
  resolutionUm: number | null;
  /** The XY arc sagitta when it is set apart from the z step. */
  resolutionXyUm: number | null;
  presetsNm: number[];
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
  10: "Standard",
  2: "Accurate",
};

/**
 * The geometry resolution and the project window.
 *
 * The kernel keeps exact polygons, so neither number is a lattice: the
 * resolution is how finely a curve is walked, and the window is the piece of
 * wafer being built on -- including, below zero, the substrate itself. Both
 * discard every stored result, which is why the dialog says what would
 * change before it changes anything.
 */
export function GridEditor({
  grid,
  kernel,
  resolutionUm,
  resolutionXyUm,
  presetsNm,
  busy,
  onPlan,
  onApply,
  onClose,
}: GridEditorProps) {
  const currentNm = (resolutionUm ?? grid.spacingUm) * 1000;
  const [spacing, setSpacing] = useState(String(Number(currentNm.toFixed(4))));
  // The XY arc sagitta: empty follows the z step.
  const [spacingXy, setSpacingXy] = useState(
    resolutionXyUm === null ? "" : String(Number((resolutionXyUm * 1000).toFixed(4))),
  );
  const xyNm = useMemo((): number | null | undefined => {
    if (spacingXy.trim() === "") return null;
    const value = Number(spacingXy);
    return Number.isFinite(value) && value > 0 ? value : undefined;
  }, [spacingXy]);
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

  // What a value would mean is the worker's answer, not a guess here: it
  // knows whether this is the resolution and window already in use, and
  // applying one that changes nothing would still throw the results away.
  useEffect(() => {
    const value = Number(spacing);
    if (!Number.isFinite(value) || value <= 0) {
      setPlan(undefined);
      setError("Enter a resolution in nanometres.");
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

  const spacingNm = plan?.estimate.spacingNm;
  const applicable = !!plan && !!bounds && (!plan.unchanged || windowChanged) && !busy;
  // The substrate is the part of the window below the wafer surface, so its
  // thickness is z min -- the one number somebody asking for a 200 nm wafer
  // is actually after.
  const substrateUm = bounds ? -bounds.zMin : null;
  const zCrossesZero = bounds ? bounds.zMin < 0 && bounds.zMax > 0 : true;

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Geometry resolution">
      <div className="modal-card grid-modal">
        <header className="modal-header">
          <div>
            <span className="eyebrow">NUMERICS</span>
            <h2>Geometry resolution</h2>
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <div className="modal-body">
          <span className="section-label">TARGET RESOLUTION</span>
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
            <span>Resolution</span>
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
              {kernel?.name ?? "This kernel"} keeps exact geometry and has no grid. This is the z
              step it samples a conformal film or an isotropic etch in: the height of the staircase
              on a rounded shoulder. Planar deposition, vertical etching and CMP are identical to
              the last bit whatever it is, so a flow made of those will not change when this does.
            </small>
          </label>

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

          <label className="field-row">
            <span>Substrate thickness</span>
            <UnitNumber
              field="substrate_thickness"
              unit="µm"
              value={substrateUm}
              onCommit={(thickness) =>
                setWindow({ ...window_, zMin: String(Number((-Math.abs(thickness)).toPrecision(12))) })
              }
            />
            <small>
              The same number as z min, from the other side: the wafer is as thick as the window is
              deep. Type 200 nm here and z min becomes −0.2.
            </small>
          </label>

          <small className="window-note">
            The wafer surface is z = 0: below it is substrate, above it is room for what the flow
            builds. Make z max taller for a thicker stack.
          </small>

          <dl className="grid-summary">
            <dt>Current</dt>
            <dd>
              {`z ${currentNm.toFixed(3)} nm · XY ${((resolutionXyUm ?? currentNm / 1000) * 1000).toFixed(3)} nm`}
            </dd>
            <dt>Proposed</dt>
            <dd>
              {planning ? (
                <LoaderCircle className="spin" size={11} />
              ) : spacingNm === undefined ? (
                "—"
              ) : (
                `z ${spacingNm.toFixed(3)} nm · XY ${(
                  (plan?.estimate as { spacingXyNm?: number } | undefined)?.spacingXyNm ?? spacingNm
                ).toFixed(3)} nm`
              )}
            </dd>
            <dt>Substrate</dt>
            <dd>
              {substrateUm === null
                ? "—"
                : `${Number((substrateUm * 1000).toPrecision(6))} nm thick`}
            </dd>
          </dl>

          {error && (
            <div className="error-box">
              <TriangleAlert size={13} />
              <span>{error}</span>
            </div>
          )}

          {!zCrossesZero && (
            <div className="error-box">
              <TriangleAlert size={13} />
              <span>
                The z range has to cross zero: the substrate is what lies below the wafer surface,
                and the flow builds above it.
              </span>
            </div>
          )}

          {plan?.unchanged && !windowChanged && (
            <p className="numerics-note">This is the resolution already in use.</p>
          )}

          <div className="warning-box">
            <TriangleAlert size={13} />
            <span>
              Changing the resolution or the window discards every stored result: the flow is
              replayed from the bare wafer at the new resolution.
            </span>
          </div>
        </div>

        <div className="modal-actions">
          <span>{applicable ? "Ready to apply" : "Pick a different resolution or window"}</span>
          <button type="button" className="secondary-button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="primary-button modal-save"
            disabled={!applicable}
            onClick={() => bounds && xyNm !== undefined && onApply(Number(spacing), bounds, xyNm)}
          >
            Apply resolution
          </button>
        </div>
      </div>
    </div>
  );
}
