import { BookDown, CircleAlert, Layers, PenLine, Play, Plus, Save, Trash2, Wrench, X } from "lucide-react";
import { ToolPicker } from "./ToolPicker";
import { useEffect, useMemo, useState } from "react";
import type {
  KernelDescription,
  MaskKeep,
  MaskSource,
  MaterialDefinition,
  MaterialResponse,
  ParameterValue,
  ProcessStep,
  ProcessType,
  QuickSketch,
  Recipe,
  StepStatus,
  ToolDefinition,
} from "../types";

interface InspectorProps {
  step: ProcessStep | undefined;
  recipes: Recipe[];
  tools: ToolDefinition[];
  onManageTools: () => void;
  materials: MaterialDefinition[];
  status: StepStatus;
  sketches: QuickSketch[];
  gdsPath: string | null;
  kernel?: KernelDescription;
  solverOrders: number[];
  busy: boolean;
  onRename: (name: string) => void;
  onProcessTypeChange: (type: ProcessType) => void;
  onDefinitionChange: (
    patch: Partial<Pick<ProcessStep, "tool" | "outputMaterial" | "materialResponses">>,
  ) => void;
  onParameter: (patch: Record<string, ParameterValue>) => void;
  onLoadRecipe: (recipe: Recipe) => void;
  onSaveRecipe: (name: string) => void;
  onMaskChange: (patch: {
    maskSource?: MaskSource;
    layer?: number | null;
    datatype?: number | null;
    keep?: MaskKeep;
  }) => void;
  /** Open the sketch editor on a sketch, or on a new one when null. */
  onEditSketch: (sketchId: string | null) => void;
  onRunToHere: () => void;
  onRemove: () => void;
}

interface ParameterSpec {
  key: string;
  label: string;
  unit?: string;
  kind?: "number" | "text" | "mode" | "solver";
  initial: ParameterValue;
  hint?: string;
  /** Kernels that read this parameter; absent means every kernel does. */
  kernels?: string[];
}

const PARAMETER_SPECS: Record<ProcessType, ParameterSpec[]> = {
  deposit: [
    {
      key: "target",
      label: "Target thickness",
      unit: "µm",
      initial: 0.05,
      hint: "Micrometres: 50 nm is 0.05.",
    },
    { key: "rate", label: "Deposition rate", unit: "µm/min", initial: 0.01 },
    { key: "time_min", label: "Time", unit: "min", initial: 1 },
    { key: "temperature_c", label: "Temperature", unit: "°C", initial: 25 },
    { key: "mode", label: "Deposition mode", kind: "mode", initial: "conformal" },
    {
      key: "base_z",
      label: "Base height",
      unit: "µm",
      initial: 0,
      hint: "Used by directional, evaporation and fill modes.",
      kernels: ["levelset"],
    },
  ],
  etch: [
    { key: "target", label: "Target depth", unit: "µm", initial: 0.1, hint: "Micrometres: 100 nm is 0.1." },
    { key: "time_min", label: "Time", unit: "min", initial: 1 },
    { key: "temperature_c", label: "Temperature", unit: "°C", initial: 25 },
    {
      key: "directional_fraction",
      label: "Directional fraction",
      initial: 1,
      hint: "1 is vertical; 0 is isotropic.",
    },
    { key: "surface_z", label: "Surface height", unit: "µm", initial: 0, kernels: ["levelset"] },
    {
      key: "solver_order",
      label: "Solver order",
      kind: "solver",
      initial: 1,
      kernels: ["levelset"],
    },
    {
      key: "tile_shape",
      label: "Tile size",
      unit: "nodes",
      initial: [24, 24, 24],
      hint: "Execution tiling changes memory use, not spatial resolution.",
      kernels: ["levelset"],
    },
  ],
  cmp: [
    { key: "target_z", label: "Planarize to z", unit: "µm", initial: 0 },
    { key: "removal_amount", label: "Removal amount", unit: "µm", initial: 0.05 },
    { key: "materials", label: "Materials", kind: "text", initial: "", kernels: ["levelset"] },
  ],
  no_geometry: [
    { key: "time_min", label: "Time", unit: "min", initial: 1 },
    { key: "temperature_c", label: "Temperature", unit: "°C", initial: 25 },
  ],
  oxidation: [
    { key: "target", label: "Consumed thickness", unit: "µm", initial: 0.02 },
    { key: "time_min", label: "Time", unit: "min", initial: 1 },
    { key: "temperature_c", label: "Temperature", unit: "°C", initial: 900 },
  ],
};

const MODE_LABELS: Record<string, string> = {
  conformal: "Conformal",
  planar: "Planar",
  directional: "Directional prism",
  evaporation: "Evaporation",
  fill: "Fill",
};

const TYPE_LABELS: Record<ProcessType, string> = {
  deposit: "Deposition",
  etch: "Etch",
  cmp: "CMP",
  no_geometry: "No geometry change",
  oxidation: "Oxidation",
};

function NumberInput({
  value,
  unit,
  onCommit,
}: {
  value: ParameterValue | undefined;
  unit?: string;
  onCommit: (value: number) => void;
}) {
  const scalar = Array.isArray(value) ? value[0] : value;
  const [draft, setDraft] = useState(scalar === undefined || scalar === null ? "" : String(scalar));
  useEffect(() => {
    setDraft(scalar === undefined || scalar === null ? "" : String(scalar));
  }, [JSON.stringify(value)]);
  const commit = () => {
    const parsed = Number(draft);
    if (Number.isFinite(parsed)) onCommit(parsed);
    else setDraft(scalar === undefined || scalar === null ? "" : String(scalar));
  };
  return (
    <span className="number-input-wrap">
      <input
        type="text"
        inputMode="decimal"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => event.key === "Enter" && event.currentTarget.blur()}
      />
      {unit && <span>{unit}</span>}
    </span>
  );
}

function ParameterRow({
  spec,
  value,
  solverOrders,
  depositionModes,
  onChange,
  onRemove,
}: {
  spec: ParameterSpec;
  value: ParameterValue;
  solverOrders: number[];
  depositionModes: string[];
  onChange: (value: ParameterValue) => void;
  onRemove: () => void;
}) {
  return (
    <div className="field-row parameter-row">
      <div className="parameter-heading">
        {spec.label}
        <button type="button" title={`Remove ${spec.label}`} onClick={onRemove}>
          <X size={11} />
        </button>
      </div>
      {spec.kind === "mode" ? (
        <select value={String(value)} onChange={(event) => onChange(event.target.value)}>
          {depositionModes.map((mode) => (
            <option key={mode} value={mode}>
              {MODE_LABELS[mode] ?? mode}
            </option>
          ))}
        </select>
      ) : spec.kind === "solver" ? (
        <select value={String(value)} onChange={(event) => onChange(Number(event.target.value))}>
          {solverOrders.map((order) => (
            <option key={order} value={order}>
              {order === 1 ? "1 — upwind" : "2 — minmod + SSP-RK2"}
            </option>
          ))}
        </select>
      ) : spec.kind === "text" ? (
        <input value={String(value)} onChange={(event) => onChange(event.target.value)} />
      ) : (
        <NumberInput
          value={value}
          unit={spec.unit}
          onCommit={(next) => onChange(spec.key === "tile_shape" ? [next, next, next] : next)}
        />
      )}
      {spec.hint && <small>{spec.hint}</small>}
    </div>
  );
}

function UnknownParameterRow({
  name,
  value,
  onChange,
  onRemove,
}: {
  name: string;
  value: ParameterValue;
  onChange: (value: ParameterValue) => void;
  onRemove: () => void;
}) {
  const [draft, setDraft] = useState(JSON.stringify(value));
  const [error, setError] = useState(false);
  useEffect(() => setDraft(JSON.stringify(value)), [JSON.stringify(value)]);
  return (
    <div className="field-row parameter-row">
      <div className="parameter-heading">
        {name}
        <button type="button" title={`Remove ${name}`} onClick={onRemove}>
          <X size={11} />
        </button>
      </div>
      <input
        className={error ? "invalid-input" : ""}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => {
          try {
            onChange(JSON.parse(draft) as ParameterValue);
            setError(false);
          } catch {
            setError(true);
          }
        }}
      />
      {error && <small>Use JSON syntax: 1, true, "text", or [24,24,24].</small>}
    </div>
  );
}

export function Inspector({
  step,
  recipes,
  tools,
  onManageTools,
  materials,
  status,
  sketches,
  gdsPath,
  kernel,
  solverOrders,
  busy,
  onRename,
  onProcessTypeChange,
  onDefinitionChange,
  onParameter,
  onLoadRecipe,
  onSaveRecipe,
  onMaskChange,
  onEditSketch,
  onRunToHere,
  onRemove,
}: InspectorProps) {
  const [name, setName] = useState(step?.name ?? "");
  const [libraryRecipeId, setLibraryRecipeId] = useState("");
  const [saveName, setSaveName] = useState(step?.name ?? "");
  const [parameterToAdd, setParameterToAdd] = useState("");
  useEffect(() => {
    setName(step?.name ?? "");
    setSaveName(step?.name ?? "");
    setLibraryRecipeId("");
    setParameterToAdd("");
    // The name changes under the same id when a CLI command or a pasted
    // flow rewrites the step; the field must show what is stored.
  }, [step?.id, step?.name]);

  const matchingRecipes = useMemo(
    () => recipes.filter((recipe) => recipe.processType === step?.processType),
    [recipes, step?.processType],
  );
  // The template picker shows the library's groups as option groups.
  const groupedTemplates = useMemo(() => {
    const byGroup = new Map<string, Recipe[]>();
    const sorted = [...matchingRecipes].sort(
      (a, b) => a.group.localeCompare(b.group) || a.name.localeCompare(b.name),
    );
    for (const recipe of sorted) {
      const list = byGroup.get(recipe.group) ?? [];
      list.push(recipe);
      byGroup.set(recipe.group, list);
    }
    return [...byGroup.entries()];
  }, [matchingRecipes]);

  if (!step) {
    return (
      <aside className="inspector-panel empty-inspector">
        <Layers size={22} />
        <span>Select a step to define its process.</span>
      </aside>
    );
  }

  // A parameter another kernel reads would do nothing here, so it is not
  // offered: the fields are what this project's kernel actually uses.
  const kernelId = kernel?.id ?? "levelset";
  const depositionModes = kernel?.depositionModes ?? [
    "conformal",
    "directional",
    "evaporation",
    "fill",
  ];
  const specs = PARAMETER_SPECS[step.processType]
    .filter((spec) => !spec.kernels || spec.kernels.includes(kernelId))
    .map((spec) =>
      spec.key === "directional_fraction" && kernel && kernel.directionalFractions.length > 0
        ? {
            ...spec,
            hint: `${kernel.name} accepts ${kernel.directionalFractions
              .map((value) => (value === 1 ? "1 (vertical)" : value === 0 ? "0 (isotropic)" : String(value)))
              .join(" or ")}.`,
          }
        : spec,
    );
  const knownKeys = new Set(specs.map((spec) => spec.key));
  const activeSpecs = specs.filter((spec) => spec.key in step.parameters);
  const missingSpecs = specs.filter((spec) => !(spec.key in step.parameters));
  const unknownParameters = Object.entries(step.parameters).filter(
    ([key]) => !knownKeys.has(key) && key !== "sketch_id",
  );
  const unusedResponseMaterials = materials.filter(
    (material) => !(material.name in step.materialResponses),
  );

  const setResponse = (material: string, patch: Partial<MaterialResponse>) => {
    // A material added as a response is meant to etch, so it starts at a
    // rate of 1 µm/min rather than a zero that looks like a stop layer.
    const current = step.materialResponses[material] ?? {
      material,
      rateUmPerMin: patch.stopLayer ? 0 : 1,
      stopLayer: false,
    };
    onDefinitionChange({
      materialResponses: {
        ...step.materialResponses,
        [material]: { ...current, ...patch },
      },
    });
  };

  const removeResponse = (material: string) => {
    const next = { ...step.materialResponses };
    delete next[material];
    onDefinitionChange({ materialResponses: next });
  };

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
        <label className="field-row">
          <span>Step name</span>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            onBlur={() => onRename(name)}
            onKeyDown={(event) => event.key === "Enter" && event.currentTarget.blur()}
          />
          <small>The name is free text and is never taken from the Recipe Library.</small>
        </label>

        <label className="field-row">
          <span>Process type</span>
          <select
            value={step.processType}
            onChange={(event) => onProcessTypeChange(event.target.value as ProcessType)}
          >
            {(Object.keys(TYPE_LABELS) as ProcessType[]).map((type) => (
              <option key={type} value={type}>{TYPE_LABELS[type]}</option>
            ))}
          </select>
        </label>

        <div className="form-section recipe-template-section">
          <span className="section-label">RECIPE TEMPLATE</span>
          <p className="numerics-note">
            Loading copies values into this step. The step is not linked to the library afterward.
          </p>
          <div className="recipe-load-row">
            <select
              value={libraryRecipeId}
              onChange={(event) => setLibraryRecipeId(event.target.value)}
            >
              <option value="">Choose an existing {TYPE_LABELS[step.processType]} recipe…</option>
              {groupedTemplates.map(([group, members]) =>
                group ? (
                  <optgroup key={group} label={group}>
                    {members.map((recipe) => (
                      <option key={recipe.id} value={recipe.id}>{recipe.name}</option>
                    ))}
                  </optgroup>
                ) : (
                  members.map((recipe) => (
                    <option key={recipe.id} value={recipe.id}>{recipe.name}</option>
                  ))
                ),
              )}
            </select>
            <button
              type="button"
              className="secondary-button"
              disabled={!libraryRecipeId}
              onClick={() => {
                const recipe = matchingRecipes.find((item) => item.id === libraryRecipeId);
                if (recipe) onLoadRecipe(recipe);
              }}
            >
              <BookDown size={13} /> Load
            </button>
          </div>
          <div className="recipe-load-row save-recipe-row">
            <input value={saveName} onChange={(event) => setSaveName(event.target.value)} />
            <button
              type="button"
              className="secondary-button"
              disabled={!saveName.trim()}
              onClick={() => onSaveRecipe(saveName)}
            >
              <Save size={13} /> Save as recipe
            </button>
          </div>
        </div>

        <div className="form-section">
          <span className="section-label">PROCESS DEFINITION</span>
          <label className="field-row">
            <span>Tool</span>
            <span className="tool-row">
              <ToolPicker
                value={step.tool}
                tools={tools}
                placeholder="optional"
                onChange={(tool) => onDefinitionChange({ tool })}
              />
              <button type="button" className="secondary-button" title="Add, group or rename tools" onClick={onManageTools}>
                <Wrench size={13} /> Manage
              </button>
            </span>
          </label>
          {(step.processType === "deposit" || step.processType === "oxidation") && (
            <label className="field-row">
              <span>{step.processType === "oxidation" ? "Oxide it becomes" : "Output material"}</span>
              <select
                value={step.outputMaterial ?? ""}
                onChange={(event) =>
                  onDefinitionChange({ outputMaterial: event.target.value || null })
                }
              >
                <option value="">Choose material…</option>
                {materials.map((material) => (
                  <option key={material.id} value={material.name}>{material.name}</option>
                ))}
              </select>
            </label>
          )}

          {activeSpecs.map((spec) => (
            <ParameterRow
              key={spec.key}
              spec={spec}
              value={step.parameters[spec.key]}
              solverOrders={solverOrders}
              depositionModes={depositionModes}
              onChange={(value) => onParameter({ [spec.key]: value })}
              onRemove={() => onParameter({ [spec.key]: null })}
            />
          ))}
          {unknownParameters.map(([key, value]) => (
            <UnknownParameterRow
              key={key}
              name={key}
              value={value}
              onChange={(next) => onParameter({ [key]: next })}
              onRemove={() => onParameter({ [key]: null })}
            />
          ))}
          {missingSpecs.length > 0 && (
            <div className="add-parameter-row">
              <select value={parameterToAdd} onChange={(event) => setParameterToAdd(event.target.value)}>
                <option value="">Add a parameter…</option>
                {missingSpecs.map((spec) => (
                  <option key={spec.key} value={spec.key}>{spec.label}</option>
                ))}
              </select>
              <button
                type="button"
                disabled={!parameterToAdd}
                onClick={() => {
                  const spec = missingSpecs.find((item) => item.key === parameterToAdd);
                  if (spec) onParameter({ [spec.key]: spec.initial });
                  setParameterToAdd("");
                }}
              >Add</button>
            </div>
          )}
          {activeSpecs.length === 0 && unknownParameters.length === 0 && (
            <div className="empty-result">
              <CircleAlert size={14} />
              <span>This step currently has no process parameters.</span>
            </div>
          )}
        </div>

        {(step.processType === "etch" || step.processType === "oxidation") && (
          <div className="form-section">
            <span className="section-label">
              {step.processType === "oxidation" ? "MATERIALS THAT OXIDISE" : "MATERIAL ETCH RESPONSES"}
            </span>
            {Object.values(step.materialResponses).map((response) => (
              <div className="response-row" key={response.material}>
                <input value={response.material} readOnly />
                <input
                  type="number"
                  min="0"
                  step="0.001"
                  value={response.rateUmPerMin}
                  onChange={(event) =>
                    setResponse(response.material, {
                      rateUmPerMin: Math.max(0, Number(event.target.value) || 0),
                    })
                  }
                />
                <label>
                  <input
                    type="checkbox"
                    checked={response.stopLayer}
                    onChange={(event) =>
                      setResponse(response.material, { stopLayer: event.target.checked })
                    }
                  /> stop
                </label>
                <button type="button" onClick={() => removeResponse(response.material)}>
                  <Trash2 size={12} />
                </button>
              </div>
            ))}
            <select
              value=""
              onChange={(event) => event.target.value && setResponse(event.target.value, {})}
            >
              <option value="">Add material response…</option>
              {unusedResponseMaterials.map((material) => (
                <option key={material.id} value={material.name}>{material.name}</option>
              ))}
            </select>
            <small className="field-hint">
              {step.processType === "oxidation"
                ? "The exposed skin of each listed material, the consumed thickness deep (scaled by its rate), becomes the oxide in place; nothing swells."
                : "Rates are µm/min. A stop layer is stored as zero rate."}
            </small>
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
            <>
              <label className="field-row">
                <span>Sketch</span>
                <select
                  value={String(step.parameters.sketch_id ?? "default")}
                  onChange={(event) => onParameter({ sketch_id: event.target.value })}
                >
                  {sketches.map((sketch) => (
                    <option key={sketch.id} value={sketch.id}>
                      {sketch.name} · {sketch.shapes.length} shape{sketch.shapes.length === 1 ? "" : "s"}
                    </option>
                  ))}
                </select>
              </label>
              <div className="sketch-actions">
                <button
                  type="button"
                  className="secondary-button"
                  disabled={busy}
                  onClick={() => onEditSketch(String(step.parameters.sketch_id ?? "default"))}
                >
                  <PenLine size={13} />
                  Edit sketch
                </button>
                <button type="button" className="secondary-button" disabled={busy} onClick={() => onEditSketch(null)}>
                  <Plus size={13} />
                  New sketch
                </button>
              </div>
            </>
          )}
          {step.maskSource === "gds" && (
            <>
              <div className="layout-summary">
                {gdsPath ? `Layout: ${gdsPath.split(/[\\/]/).pop()}` : "Import a project GDS first."}
              </div>
              <div className="placement-grid">
                <label className="field-row">
                  <span>Layer</span>
                  <NumberInput value={step.layer} onCommit={(value) => onMaskChange({ layer: value })} />
                </label>
                <label className="field-row">
                  <span>Datatype</span>
                  <NumberInput value={step.datatype} onCommit={(value) => onMaskChange({ datatype: value })} />
                </label>
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
      </div>

      <div className="inspector-actions">
        <button type="button" className="secondary-button" disabled={busy} onClick={onRunToHere}>
          <Play size={13} /> Run to here
        </button>
        <button type="button" className="danger-button" disabled={busy} onClick={onRemove}>
          <Trash2 size={13} /> Delete step
        </button>
      </div>
    </aside>
  );
}
