import { CircleAlert, FileCode2, Layers, Play, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import type {
  MaskKeep,
  MaskSource,
  ParameterValue,
  ProcessStep,
  QuickSketch,
  Recipe,
  StepStatus,
} from "../types";

interface InspectorProps {
  step: ProcessStep | undefined;
  recipe: Recipe | undefined;
  status: StepStatus;
  sketches: QuickSketch[];
  gdsPath: string | null;
  solverOrders: number[];
  resolved: Record<string, ParameterValue>;
  busy: boolean;
  onRename: (name: string) => void;
  onOverride: (patch: Record<string, ParameterValue>) => void;
  onReplaceOverrides: (overrides: Record<string, ParameterValue>) => void;
  onMaskChange: (patch: {
    maskSource?: MaskSource;
    layer?: number | null;
    datatype?: number | null;
    keep?: MaskKeep;
  }) => void;
  onRunToHere: () => void;
  onRemove: () => void;
}

/** Fields the dedicated inputs own; anything else stays editable as raw JSON. */
const MANAGED_KEYS = new Set([
  "target",
  "thickness",
  "rate",
  "time_min",
  "temperature_c",
  "directional_fraction",
  "target_z",
  "removal_amount",
  "materials",
  "material",
  "mode",
  "base_z",
  "surface_z",
  "stop_materials",
  "solver_order",
  "tile_shape",
  "sketch_id",
]);

function NumberField({
  label,
  unit,
  hint,
  value,
  recipeValue,
  onCommit,
}: {
  label: string;
  unit?: string;
  hint?: string;
  value: ParameterValue | undefined;
  recipeValue: ParameterValue | undefined;
  onCommit: (value: number | null) => void;
}) {
  const [draft, setDraft] = useState(value === undefined || value === null ? "" : String(value));
  useEffect(() => {
    setDraft(value === undefined || value === null ? "" : String(value));
  }, [value]);

  const commit = () => {
    const text = draft.trim();
    if (!text) {
      onCommit(null);
      return;
    }
    const parsed = Number(text);
    if (Number.isFinite(parsed)) onCommit(parsed);
    else setDraft(value === undefined || value === null ? "" : String(value));
  };

  return (
    <label className="field-row">
      <span>{label}</span>
      <span className="number-input-wrap">
        <input
          type="text"
          inputMode="decimal"
          value={draft}
          placeholder={
            recipeValue === undefined || recipeValue === null ? "not set" : `${recipeValue}`
          }
          onChange={(event) => setDraft(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Enter") event.currentTarget.blur();
          }}
        />
        {unit && <span>{unit}</span>}
      </span>
      {hint && <small>{hint}</small>}
    </label>
  );
}

function TextField({
  label,
  hint,
  value,
  placeholder,
  onCommit,
}: {
  label: string;
  hint?: string;
  value: ParameterValue | undefined;
  placeholder?: string;
  onCommit: (value: string | null) => void;
}) {
  const [draft, setDraft] = useState(value === undefined || value === null ? "" : String(value));
  useEffect(() => {
    setDraft(value === undefined || value === null ? "" : String(value));
  }, [value]);
  return (
    <label className="field-row">
      <span>{label}</span>
      <input
        type="text"
        value={draft}
        placeholder={placeholder}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => onCommit(draft.trim() ? draft.trim() : null)}
        onKeyDown={(event) => {
          if (event.key === "Enter") event.currentTarget.blur();
        }}
      />
      {hint && <small>{hint}</small>}
    </label>
  );
}

function ExtraOverrides({
  overrides,
  onReplace,
}: {
  overrides: Record<string, ParameterValue>;
  onReplace: (value: Record<string, ParameterValue>) => void;
}) {
  const extra = Object.fromEntries(
    Object.entries(overrides).filter(([key]) => !MANAGED_KEYS.has(key)),
  );
  const [draft, setDraft] = useState(JSON.stringify(extra, null, 2));
  const [error, setError] = useState<string>();
  useEffect(() => {
    setDraft(JSON.stringify(extra, null, 2));
    setError(undefined);
    // Re-seed only when the step's own extra keys change.
  }, [JSON.stringify(extra)]);

  const commit = () => {
    try {
      const parsed = draft.trim() ? JSON.parse(draft) : {};
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("Overrides must be a JSON object.");
      }
      const managed = Object.fromEntries(
        Object.entries(overrides).filter(([key]) => MANAGED_KEYS.has(key)),
      );
      setError(undefined);
      onReplace({ ...managed, ...(parsed as Record<string, ParameterValue>) });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  return (
    <label className="field-row json-field">
      <span>Extra overrides (JSON)</span>
      <textarea value={draft} onChange={(event) => setDraft(event.target.value)} onBlur={commit} />
      <small>Anything the recipe accepts. The fields above own the keys they show.</small>
      {error && (
        <div className="error-box">
          <CircleAlert size={13} />
          <span>{error}</span>
        </div>
      )}
    </label>
  );
}

export function Inspector({
  step,
  recipe,
  status,
  sketches,
  gdsPath,
  solverOrders,
  resolved,
  busy,
  onRename,
  onOverride,
  onReplaceOverrides,
  onMaskChange,
  onRunToHere,
  onRemove,
}: InspectorProps) {
  const [name, setName] = useState(step?.name ?? "");
  useEffect(() => setName(step?.name ?? ""), [step?.id, step?.name]);

  if (!step) {
    return (
      <aside className="inspector-panel empty-inspector">
        <Layers size={22} />
        <span>Select a step to edit its recipe values.</span>
      </aside>
    );
  }

  const type = recipe?.processType;
  const number = (key: string) => (value: number | null) => onOverride({ [key]: value });

  return (
    <aside className="inspector-panel">
      <div className="panel-heading inspector-heading">
        <div>
          <span className="eyebrow">STEP</span>
          <h2>{step.name}</h2>
        </div>
        <span className={`status-chip status-${status}`}>{status}</span>
      </div>

      <div className="inspector-scroll">
        <p className="definition-copy">
          {recipe
            ? `${recipe.name} · ${recipe.processType} · ${recipe.tool || "no tool"}`
            : "This step points at a recipe that no longer exists."}
        </p>

        <label className="field-row">
          <span>Step name</span>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            onBlur={() => onRename(name)}
            onKeyDown={(event) => {
              if (event.key === "Enter") event.currentTarget.blur();
            }}
          />
        </label>

        <div className="form-section">
          <span className="section-label">PROCESS</span>
          {type === "deposit" && (
            <>
              <TextField
                label="Material"
                value={step.overrides.material}
                placeholder={recipe?.outputMaterial ?? "recipe output"}
                hint="Overrides the recipe's output material for this step."
                onCommit={(value) => onOverride({ material: value })}
              />
              <NumberField
                label="Target thickness"
                unit="µm"
                value={step.overrides.target}
                recipeValue={recipe?.parameters.target}
                onCommit={number("target")}
              />
              <NumberField
                label="Rate"
                unit="µm/min"
                value={step.overrides.rate}
                recipeValue={recipe?.parameters.rate}
                onCommit={number("rate")}
              />
              <label className="field-row">
                <span>Mode</span>
                <select
                  value={String(resolved.mode ?? "conformal")}
                  onChange={(event) =>
                    onOverride({
                      mode: event.target.value === "conformal" ? null : event.target.value,
                    })
                  }
                >
                  <option value="conformal">Conformal (equal thickness)</option>
                  <option value="directional">Directional prism</option>
                  <option value="evaporation">Evaporation</option>
                  <option value="fill">Fill</option>
                </select>
                <small>
                  Conformal grows off every surface. The other modes extrude the mask upward from
                  the base height.
                </small>
              </label>
              {resolved.mode !== undefined && resolved.mode !== "conformal" && (
                <NumberField
                  label="Base height"
                  unit="µm"
                  value={step.overrides.base_z}
                  recipeValue={recipe?.parameters.base_z}
                  hint="Empty means the current top surface."
                  onCommit={number("base_z")}
                />
              )}
            </>
          )}

          {type === "etch" && (
            <>
              <NumberField
                label="Target depth"
                unit="µm"
                value={step.overrides.target}
                recipeValue={recipe?.parameters.target}
                onCommit={number("target")}
              />
              <NumberField
                label="Time"
                unit="min"
                value={step.overrides.time_min}
                recipeValue={recipe?.parameters.time_min}
                hint="Used when no target depth is given; depth is time × the fastest rate."
                onCommit={number("time_min")}
              />
              <NumberField
                label="Directional fraction"
                value={step.overrides.directional_fraction}
                recipeValue={recipe?.parameters.directional_fraction}
                hint="1.0 is fully vertical, 0.0 is fully isotropic."
                onCommit={number("directional_fraction")}
              />
              <NumberField
                label="Surface height"
                unit="µm"
                value={step.overrides.surface_z}
                recipeValue={recipe?.parameters.surface_z}
                hint="Empty means the highest occupied node."
                onCommit={number("surface_z")}
              />
              <TextField
                label="Stop materials"
                value={step.overrides.stop_materials}
                placeholder="comma separated"
                hint="Forces these materials to a zero etch rate for this step."
                onCommit={(value) => onOverride({ stop_materials: value })}
              />
            </>
          )}

          {type === "cmp" && (
            <>
              <NumberField
                label="Planarize to z"
                unit="µm"
                value={step.overrides.target_z}
                recipeValue={recipe?.parameters.target_z}
                onCommit={number("target_z")}
              />
              <NumberField
                label="Removal amount"
                unit="µm"
                value={step.overrides.removal_amount}
                recipeValue={recipe?.parameters.removal_amount}
                hint="Used when no target height is given."
                onCommit={number("removal_amount")}
              />
              <TextField
                label="Materials"
                value={step.overrides.materials}
                placeholder={String(recipe?.parameters.materials ?? "all materials")}
                hint="Comma separated. Only these are removed."
                onCommit={(value) => onOverride({ materials: value })}
              />
            </>
          )}

          {type === "no_geometry" && (
            <div className="empty-result">
              <FileCode2 size={14} />
              <span>
                This recipe records a process step without changing geometry. It still takes part
                in the flow so the list matches the real run sheet.
              </span>
            </div>
          )}

          <NumberField
            label="Temperature"
            unit="°C"
            value={step.overrides.temperature_c}
            recipeValue={recipe?.parameters.temperature_c}
            onCommit={number("temperature_c")}
          />
        </div>

        {type === "etch" && (
          <div className="form-section">
            <span className="section-label">NUMERICS</span>
            <label className="field-row">
              <span>Solver order</span>
              <select
                value={String(resolved.solver_order ?? 1)}
                onChange={(event) =>
                  onOverride({
                    solver_order: Number(event.target.value) === 1 ? null : Number(event.target.value),
                  })
                }
              >
                {solverOrders.map((order) => (
                  <option key={order} value={order}>
                    {order === 1 ? "1 — Godunov upwind + Euler" : "2 — minmod + SSP-RK2"}
                  </option>
                ))}
              </select>
            </label>
            <NumberField
              label="Tile size"
              unit="nodes"
              value={
                Array.isArray(step.overrides.tile_shape)
                  ? (step.overrides.tile_shape as unknown as number[])[0]
                  : step.overrides.tile_shape
              }
              recipeValue={undefined}
              hint="Splits the solve into synchronised tiles. Empty solves the whole grid at once; tiling changes memory use, not the result."
              onCommit={(value) =>
                onOverride({
                  tile_shape: value === null ? null : (([value, value, value] as unknown) as ParameterValue),
                })
              }
            />
            <p className="numerics-note">
              Second order lowers discretisation error near smooth interfaces. The limiter drops
              back to first order at corners, so it is not uniformly more accurate.
            </p>
          </div>
        )}

        <div className="form-section mask-section">
          <span className="section-label">MASK</span>
          <label className="field-row">
            <span>Source</span>
            <select
              value={step.maskSource}
              onChange={(event) => onMaskChange({ maskSource: event.target.value as MaskSource })}
            >
              <option value="none">No mask (blanket exposure)</option>
              <option value="quick_sketch">Quick Sketch</option>
              <option value="gds">GDSII layer</option>
            </select>
          </label>

          {step.maskSource === "quick_sketch" && (
            <label className="field-row">
              <span>Sketch</span>
              <select
                value={String(resolved.sketch_id ?? "default")}
                onChange={(event) => onOverride({ sketch_id: event.target.value })}
              >
                {sketches.map((sketch) => (
                  <option key={sketch.id} value={sketch.id}>
                    {sketch.id} ({sketch.shapes.length} shapes)
                  </option>
                ))}
                {sketches.length === 0 && <option value="default">default</option>}
              </select>
            </label>
          )}

          {step.maskSource === "gds" && (
            <>
              <div className="layout-summary">
                {gdsPath ? (
                  <>
                    Layout: <strong>{gdsPath.split(/[\\/]/).pop()}</strong>
                  </>
                ) : (
                  "This project has no GDS file yet. Import one from the top bar."
                )}
              </div>
              <div className="placement-grid">
                <NumberField
                  label="Layer"
                  value={step.layer}
                  recipeValue={null}
                  onCommit={(value) => onMaskChange({ layer: value })}
                />
                <NumberField
                  label="Datatype"
                  value={step.datatype}
                  recipeValue={null}
                  onCommit={(value) => onMaskChange({ datatype: value })}
                />
              </div>
            </>
          )}

          {step.maskSource !== "none" && (
            <label className="field-row">
              <span>Keep</span>
              <select
                value={step.keep}
                onChange={(event) => onMaskChange({ keep: event.target.value as MaskKeep })}
              >
                <option value="inside">Inside the shapes</option>
                <option value="outside">Outside the shapes</option>
              </select>
            </label>
          )}
        </div>

        <div className="form-section">
          <span className="section-label">ADVANCED</span>
          <ExtraOverrides overrides={step.overrides} onReplace={onReplaceOverrides} />
        </div>
      </div>

      <div className="inspector-actions">
        <button type="button" className="secondary-button" disabled={busy} onClick={onRunToHere}>
          <Play size={13} />
          Run to here
        </button>
        <button type="button" className="danger-button" disabled={busy} onClick={onRemove}>
          <Trash2 size={13} />
          Delete step
        </button>
      </div>
    </aside>
  );
}
