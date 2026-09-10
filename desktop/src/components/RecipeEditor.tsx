import { CircleAlert, FileSpreadsheet, Plus, Trash2, Upload, X } from "lucide-react";
import { useEffect, useState } from "react";
import { newId } from "../domain/project";
import type { MaterialDefinition, ParameterValue, ProcessType, Recipe } from "../types";

interface RecipeEditorProps {
  recipes: Recipe[];
  materials: MaterialDefinition[];
  usedRecipeIds: Set<string>;
  busy: boolean;
  onSave: (recipe: Recipe) => void;
  onDelete: (recipeId: string) => void;
  onExport: () => void;
  onImport: () => void;
  onClose: () => void;
}

const PROCESS_TYPES: ProcessType[] = ["deposit", "etch", "cmp", "no_geometry"];

function ParameterField({
  recipe,
  onSave,
}: {
  recipe: Recipe;
  onSave: (recipe: Recipe) => void;
}) {
  const [draft, setDraft] = useState(JSON.stringify(recipe.parameters, null, 2));
  const [error, setError] = useState<string>();
  useEffect(() => {
    setDraft(JSON.stringify(recipe.parameters, null, 2));
    setError(undefined);
  }, [recipe.id, JSON.stringify(recipe.parameters)]);

  return (
    <label className="field-row json-field">
      <span>Parameters (JSON)</span>
      <textarea
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => {
          try {
            const parsed = draft.trim() ? JSON.parse(draft) : {};
            if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
              throw new Error("Parameters must be a JSON object.");
            }
            setError(undefined);
            onSave({ ...recipe, parameters: parsed as Record<string, ParameterValue> });
          } catch (reason) {
            setError(reason instanceof Error ? reason.message : String(reason));
          }
        }}
      />
      <small>
        target and time_min drive depth or thickness; rate, directional_fraction, mode, base_z,
        solver_order and tile_shape are read by the kernel when present.
      </small>
      {error && (
        <div className="error-box">
          <CircleAlert size={13} />
          <span>{error}</span>
        </div>
      )}
    </label>
  );
}

export function RecipeEditor({
  recipes,
  materials,
  usedRecipeIds,
  busy,
  onSave,
  onDelete,
  onExport,
  onImport,
  onClose,
}: RecipeEditorProps) {
  const [selectedId, setSelectedId] = useState(recipes[0]?.id ?? "");
  const selected = recipes.find((recipe) => recipe.id === selectedId) ?? recipes[0];

  const update = (patch: Partial<Recipe>) => {
    if (!selected) return;
    onSave({ ...selected, ...patch });
  };

  const addRecipe = () => {
    const recipe: Recipe = {
      id: newId("recipe"),
      name: "New recipe",
      processType: "deposit",
      tool: "",
      outputMaterial: materials[0]?.name ?? null,
      parameters: { target: 0.05, rate: 0.01 },
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
            <span className="eyebrow">LIBRARY</span>
            <h2>Recipes</h2>
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <div className="recipe-editor-body">
          <div className="recipe-list">
            {recipes.map((recipe) => (
              <button
                key={recipe.id}
                type="button"
                className={recipe.id === selected?.id ? "active" : ""}
                onClick={() => setSelectedId(recipe.id)}
              >
                <span>
                  {recipe.name}
                  <small>
                    {recipe.processType} · {recipe.tool || "no tool"}
                  </small>
                </span>
                {usedRecipeIds.has(recipe.id) && <small>in use</small>}
              </button>
            ))}
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
                  onChange={(event) => update({ processType: event.target.value as ProcessType })}
                >
                  {PROCESS_TYPES.map((type) => (
                    <option key={type} value={type}>
                      {type}
                    </option>
                  ))}
                </select>
              </label>

              <label className="field-row">
                <span>Tool</span>
                <input value={selected.tool} onChange={(event) => update({ tool: event.target.value })} />
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

              <ParameterField recipe={selected} onSave={onSave} />

              <div className="form-section">
                <span className="section-label">MATERIAL RESPONSES</span>
                {Object.values(selected.materialResponses).map((response) => (
                  <div className="response-row" key={response.material}>
                    <input type="text" value={response.material} readOnly />
                    <input
                      type="number"
                      step="0.001"
                      min="0"
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
                  disabled={usedRecipeIds.has(selected.id)}
                  title={
                    usedRecipeIds.has(selected.id)
                      ? "A step in this project still uses this recipe."
                      : "Delete this recipe"
                  }
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
