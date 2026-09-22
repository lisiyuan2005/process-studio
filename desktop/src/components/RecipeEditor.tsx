import { FileSpreadsheet, Plus, Trash2, Upload, X } from "lucide-react";
import { useMemo, useState } from "react";
import { groupRecipes, newId, type RecipeGroup } from "../domain/project";
import { ToolPicker } from "./ToolPicker";
import type { MaterialDefinition, ParameterValue, ProcessType, Recipe, ToolDefinition } from "../types";
import { NumberField } from "./NumberField";
import { ParameterEditor } from "./ParameterRows";
import { PARAMETER_SPECS, defaultParameters, defaultsForNewTool, depositThickness } from "../domain/parameters";

interface RecipeEditorProps {
  recipes: Recipe[];
  /** Deposition modes the kernel offers, for the mode row. */
  depositionModes?: string[];
  materials: MaterialDefinition[];
  tools: ToolDefinition[];
  busy: boolean;
  onSave: (recipe: Recipe) => void;
  onDelete: (recipeId: string) => void;
  onExport: () => void;
  onImport: () => void;
  onClose: () => void;
}

const PROCESS_TYPES: ProcessType[] = ["deposit", "etch", "cmp", "no_geometry", "oxidation"];
const TYPE_TITLES: Record<ProcessType, string> = {
  deposit: "Deposition",
  etch: "Etch",
  cmp: "CMP",
  no_geometry: "No geometry change",
  oxidation: "Oxidation",
};

/** One group of the library list, its recipes and then its subgroups, indented. */
function GroupBranch({
  group,
  depth,
  selectedId,
  onSelect,
}: {
  group: RecipeGroup;
  depth: number;
  selectedId: string | undefined;
  onSelect: (recipeId: string) => void;
}) {
  return (
    <>
      {group.path && (
        <span className="list-group-title" style={{ paddingLeft: 8 + depth * 12 }}>
          {group.label}
        </span>
      )}
      {group.recipes.map((recipe) => (
        <button
          key={recipe.id}
          type="button"
          className={recipe.id === selectedId ? "active" : ""}
          style={{ paddingLeft: 10 + (group.path ? depth + 1 : depth) * 12 }}
          onClick={() => onSelect(recipe.id)}
        >
          <span>
            {recipe.name}
            <small>{recipe.tool || "no tool"}</small>
          </span>
        </button>
      ))}
      {group.children.map((child) => (
        <GroupBranch key={child.path} group={child} depth={depth + 1} selectedId={selectedId} onSelect={onSelect} />
      ))}
    </>
  );
}

/** What a deposition recipe's film works out to, the way the kernel reads it. */
function ThicknessNote({ parameters }: { parameters: Record<string, ParameterValue> }) {
  const resolved = depositThickness(parameters);
  if (!resolved || resolved.from === "target") return null;
  const nm = Number((resolved.um * 1000).toPrecision(6));
  return (
    <p className="numerics-note">
      {resolved.from === "cycles" ? "Cycles × rate per cycle" : "Time × rate"}: {nm} nm of film.
      A target thickness, if the recipe has one, wins over this.
    </p>
  );
}

export function RecipeEditor({
  recipes,
  depositionModes = ["conformal", "planar"],
  materials,
  tools,
  busy,
  onSave,
  onDelete,
  onExport,
  onImport,
  onClose,
}: RecipeEditorProps) {
  const [selectedId, setSelectedId] = useState(recipes[0]?.id ?? "");
  const selected = recipes.find((recipe) => recipe.id === selectedId) ?? recipes[0];
  // The library is arranged by process type first, then by the group path
  // each recipe carries; a group exists as soon as one recipe names it.
  const byType = useMemo(
    () => PROCESS_TYPES.map((type) => [type, groupRecipes(recipes.filter((r) => r.processType === type))] as const),
    [recipes],
  );
  const groupNames = useMemo(
    () => [...new Set(recipes.map((recipe) => recipe.group).filter(Boolean))].sort(),
    [recipes],
  );

  const update = (patch: Partial<Recipe>) => {
    if (!selected) return;
    onSave({ ...selected, ...patch });
  };

  const addRecipe = () => {
    const recipe: Recipe = {
      id: newId("recipe"),
      name: "New recipe",
      processType: selected?.processType ?? "deposit",
      tool: "",
      group: selected?.group ?? "",
      outputMaterial: materials[0]?.name ?? null,
      parameters: defaultParameters(selected?.processType ?? "deposit", selected?.tool),
      materialResponses: {},
    };
    onSave(recipe);
    setSelectedId(recipe.id);
  };

  const setResponse = (material: string, patch: { rateUmPerMin?: number; stopLayer?: boolean }) => {
    if (!selected) return;
    const current = selected.materialResponses[material] ?? {
      material,
      rateUmPerMin: 0,
      stopLayer: false,
    };
    update({
      materialResponses: { ...selected.materialResponses, [material]: { ...current, ...patch } },
    });
  };

  const removeResponse = (material: string) => {
    if (!selected) return;
    const responses = { ...selected.materialResponses };
    delete responses[material];
    update({ materialResponses: responses });
  };

  const unusedMaterials = materials.filter(
    (material) => !selected || !(material.name in selected.materialResponses),
  );

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Recipes">
      <div className="modal-card recipe-modal">
        <header className="modal-header">
          <div>
            <span className="eyebrow">LIBRARY &middot; SHARED BY EVERY PROJECT</span>
            <h2>Recipes</h2>
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <div className="recipe-editor-body">
          <div className="recipe-list grouped-list">
            {byType.map(([type, tree]) =>
              tree.recipes.length || tree.children.length ? (
                <div key={type} className="list-group">
                  <span className="list-type-title">{TYPE_TITLES[type]}</span>
                  <GroupBranch group={tree} depth={0} selectedId={selected?.id} onSelect={setSelectedId} />
                </div>
              ) : null,
            )}
            <button type="button" className="add-material" onClick={addRecipe}>
              <Plus size={13} />
              Add recipe
            </button>
          </div>

          {selected && (
            <div className="recipe-form">
              <label className="field-row">
                <span>Name</span>
                <input value={selected.name} onChange={(event) => update({ name: event.target.value })} />
              </label>

              <label className="field-row">
                <span>Process type</span>
                <select
                  value={selected.processType}
                  onChange={(event) => {
                    const processType = event.target.value as ProcessType;
                    const swap = defaultsForNewTool(processType, selected.tool, selected.parameters);
                    update(
                      swap
                        ? { processType, parameters: swap }
                        : { processType },
                    );
                  }}
                >
                  {PROCESS_TYPES.map((type) => (
                    <option key={type} value={type}>
                      {type}
                    </option>
                  ))}
                </select>
              </label>

              <label className="field-row">
                <span>Group</span>
                <input
                  list="recipe-groups"
                  value={selected.group}
                  placeholder="none: directly under the process type"
                  onChange={(event) => update({ group: event.target.value })}
                />
                <datalist id="recipe-groups">
                  {groupNames.map((group) => (
                    <option key={group} value={group} />
                  ))}
                </datalist>
                <small>
                  Type a name to make a group; a slash makes a subgroup, as in ALD/Oxides. The
                  process type is always the top level.
                </small>
              </label>

              <label className="field-row">
                <span>Tool</span>
                <ToolPicker
                  value={selected.tool}
                  tools={tools}
                  onChange={(tool) => {
                    // A recipe for an ALD tool is written in cycles and a
                    // rate per cycle. Only untouched defaults are swapped
                    // over; a recipe with numbers in it keeps them.
                    const swap = defaultsForNewTool(
                      selected.processType,
                      tool,
                      selected.parameters,
                    );
                    update(swap ? { tool, parameters: swap } : { tool });
                  }}
                />
              </label>

              {selected.processType === "deposit" && (
                <label className="field-row">
                  <span>Output material</span>
                  <select
                    value={selected.outputMaterial ?? ""}
                    onChange={(event) =>
                      update({ outputMaterial: event.target.value || null })
                    }
                  >
                    <option value="">none</option>
                    {materials.map((material) => (
                      <option key={material.id} value={material.name}>
                        {material.name}
                      </option>
                    ))}
                  </select>
                </label>
              )}

              <div className="form-section">
                <span className="section-label">PARAMETERS</span>
                <ParameterEditor
                  key={selected.id}
                  specs={PARAMETER_SPECS[selected.processType]}
                  parameters={selected.parameters}
                  depositionModes={depositionModes}
                  onPatch={(patch) => {
                    const merged = { ...selected.parameters, ...patch };
                    for (const [key, value] of Object.entries(patch)) {
                      if (value === null || value === "") delete merged[key];
                    }
                    update({ parameters: merged });
                  }}
                />
                {selected.processType === "deposit" && <ThicknessNote parameters={selected.parameters} />}
              </div>

              <div className="form-section">
                <span className="section-label">MATERIAL RESPONSES</span>
                {Object.values(selected.materialResponses).map((response) => (
                  <div className="response-row" key={response.material}>
                    <input type="text" value={response.material} readOnly />
                    <NumberField
                      step={0.001}
                      min={0}
                      value={response.rateUmPerMin}
                      onChange={(rateUmPerMin) => setResponse(response.material, { rateUmPerMin })}
                    />
                    <label>
                      <input
                        type="checkbox"
                        checked={response.stopLayer}
                        onChange={(event) =>
                          setResponse(response.material, { stopLayer: event.target.checked })
                        }
                      />
                      stop
                    </label>
                    <button
                      type="button"
                      aria-label={`Remove ${response.material}`}
                      onClick={() => removeResponse(response.material)}
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                ))}
                <div className="chip-row">
                  {unusedMaterials.map((material) => (
                    <button
                      key={material.id}
                      type="button"
                      onClick={() => setResponse(material.name, {})}
                    >
                      + {material.name}
                    </button>
                  ))}
                </div>
                <p className="numerics-note">
                  Rates are in µm/min and only apply to etch recipes. A stop layer is a zero rate,
                  not a modelled reaction.
                </p>
              </div>

              <div className="modal-actions split-actions">
                <button
                  type="button"
                  className="danger-button"
                  title="Delete this library recipe. Existing steps keep their copied values."
                  onClick={() => {
                    onDelete(selected.id);
                    setSelectedId(recipes.find((item) => item.id !== selected.id)?.id ?? "");
                  }}
                >
                  <Trash2 size={13} />
                  Delete
                </button>
                <button type="button" className="secondary-button" disabled={busy} onClick={onImport}>
                  <Upload size={13} />
                  Import XLSX
                </button>
                <button type="button" className="secondary-button" disabled={busy} onClick={onExport}>
                  <FileSpreadsheet size={13} />
                  Export XLSX
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
