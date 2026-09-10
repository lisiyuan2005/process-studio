import { Palette, Plus, Trash2, X } from "lucide-react";
import { useState } from "react";
import type { MaterialDefinition } from "../types";
import { newId } from "../domain/project";

interface MaterialEditorProps {
  materials: MaterialDefinition[];
  usedNames: Set<string>;
  onSave: (material: MaterialDefinition) => void;
  onDelete: (materialId: string) => void;
  onClose: () => void;
}

const CATEGORIES = ["Semiconductor", "Dielectric", "Metal", "Mask", "Other"];

export function MaterialEditor({
  materials,
  usedNames,
  onSave,
  onDelete,
  onClose,
}: MaterialEditorProps) {
  const [selectedId, setSelectedId] = useState(materials[0]?.id ?? "");
  const selected = materials.find((material) => material.id === selectedId) ?? materials[0];

  const update = (patch: Partial<MaterialDefinition>) => {
    if (!selected) return;
    onSave({ ...selected, ...patch });
  };

  const addMaterial = () => {
    const existing = new Set(materials.map((material) => material.name));
    let name = "New material";
    let suffix = 2;
    while (existing.has(name)) name = `New material ${suffix++}`;
    const material: MaterialDefinition = {
      id: newId("material"),
      name,
      category: "Other",
      color: "#7c83a0",
      opacity: 1,
    };
    onSave(material);
    setSelectedId(material.id);
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Materials">
      <div className="modal-card material-modal">
        <header className="modal-header">
          <div>
            <span className="eyebrow">LIBRARY</span>
            <h2>Materials</h2>
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <div className="material-editor-body">
          <div className="material-list">
            {materials.map((material) => (
              <button
                key={material.id}
                type="button"
                className={material.id === selected?.id ? "active" : ""}
                onClick={() => setSelectedId(material.id)}
              >
                <i style={{ background: material.color }} />
                <span>
                  {material.name}
                  <small>{material.category}</small>
                </span>
                {usedNames.has(material.name) && <small>in use</small>}
              </button>
            ))}
            <button type="button" className="add-material" onClick={addMaterial}>
              <Plus size={13} />
              Add material
            </button>
          </div>

          {selected ? (
            <div className="material-form">
              <label className="field-row">
                <span>Name</span>
                <input
                  value={selected.name}
                  onChange={(event) => update({ name: event.target.value })}
                />
                <small>
                  Recipes reference materials by name, so renaming one here renames it everywhere.
                </small>
              </label>

              <label className="field-row">
                <span>Category</span>
                <select
                  value={selected.category}
                  onChange={(event) => update({ category: event.target.value })}
                >
                  {CATEGORIES.map((category) => (
                    <option key={category} value={category}>
                      {category}
                    </option>
                  ))}
                </select>
              </label>

              <label className="field-row color-field">
                <span>Colour</span>
                <div>
                  <input
                    type="color"
                    value={selected.color}
                    onChange={(event) => update({ color: event.target.value })}
                  />
                  <code>{selected.color}</code>
                </div>
              </label>

              <label className="field-row">
                <span>
                  Opacity <b>{selected.opacity.toFixed(2)}</b>
                </span>
                <input
                  type="range"
                  min={0.1}
                  max={1}
                  step={0.05}
                  value={selected.opacity}
                  onChange={(event) => update({ opacity: Number(event.target.value) })}
                />
                <small>Applies to the 3D view. The 2D views always draw the top material.</small>
              </label>

              <p className="material-note">
                Colours are display only. Material order in the kernel is set by the flow: a later
                deposition wins where two materials overlap.
              </p>

              <div className="modal-actions split-actions">
                <button
                  type="button"
                  className="danger-button"
                  disabled={usedNames.has(selected.name)}
                  title={
                    usedNames.has(selected.name)
                      ? "A recipe still references this material."
                      : "Delete this material"
                  }
                  onClick={() => {
                    onDelete(selected.id);
                    setSelectedId(materials.find((item) => item.id !== selected.id)?.id ?? "");
                  }}
                >
                  <Trash2 size={13} />
                  Delete
                </button>
                <button type="button" className="secondary-button" onClick={onClose}>
                  Done
                </button>
              </div>
            </div>
          ) : (
            <div className="material-form">
              <div className="empty-result">
                <Palette size={14} />
                <span>This project has no materials yet.</span>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
