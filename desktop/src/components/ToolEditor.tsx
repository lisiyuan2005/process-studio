import { Plus, Trash2, Wrench, X } from "lucide-react";
import { useMemo, useState } from "react";
import { newId } from "../domain/project";
import type { ToolDefinition } from "../types";

interface ToolEditorProps {
  tools: ToolDefinition[];
  /** Tool names steps or recipes currently refer to, with how many use each. */
  usage: Map<string, number>;
  onSave: (tool: ToolDefinition) => void;
  onDelete: (toolId: string) => void;
  onClose: () => void;
}

/** The tool library: a grouped list, and a form for the chosen one. */
export function ToolEditor({ tools, usage, onSave, onDelete, onClose }: ToolEditorProps) {
  const [selectedId, setSelectedId] = useState(tools[0]?.id ?? "");
  const selected = tools.find((tool) => tool.id === selectedId) ?? tools[0];
  const groups = useMemo(() => {
    const byGroup = new Map<string, ToolDefinition[]>();
    const sorted = [...tools].sort(
      (a, b) => a.group.localeCompare(b.group) || a.name.localeCompare(b.name),
    );
    for (const tool of sorted) {
      const list = byGroup.get(tool.group) ?? [];
      list.push(tool);
      byGroup.set(tool.group, list);
    }
    return [...byGroup.entries()];
  }, [tools]);
  const groupNames = useMemo(
    () => [...new Set(tools.map((tool) => tool.group).filter(Boolean))].sort(),
    [tools],
  );
  const nameClash = selected
    ? tools.some((tool) => tool.id !== selected.id && tool.name === selected.name)
    : false;

  const update = (patch: Partial<ToolDefinition>) => {
    if (!selected) return;
    onSave({ ...selected, ...patch });
  };
  const addTool = () => {
    const existing = new Set(tools.map((tool) => tool.name));
    let name = "New tool";
    let suffix = 2;
    while (existing.has(name)) name = `New tool ${suffix++}`;
    const tool: ToolDefinition = {
      id: newId("tool"),
      name,
      group: selected?.group ?? "",
      notes: "",
      recipes: [],
    };
    onSave(tool);
    setSelectedId(tool.id);
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Tools">
      <div className="modal-card material-modal">
        <header className="modal-header">
          <div>
            <span className="eyebrow">LIBRARY &middot; SHARED BY EVERY PROJECT</span>
            <h2>Tools</h2>
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <div className="material-editor-body">
          <div className="material-list grouped-list">
            {groups.map(([group, members]) => (
              <div key={group || "(ungrouped)"} className="list-group">
                <span className="list-group-title">{group || "Ungrouped"}</span>
                {members.map((tool) => (
                  <button
                    key={tool.id}
                    type="button"
                    className={tool.id === selected?.id ? "active" : ""}
                    onClick={() => setSelectedId(tool.id)}
                  >
                    <Wrench size={12} />
                    <span>
                      {tool.name}
                      {usage.get(tool.name) ? <small> · used by {usage.get(tool.name)}</small> : null}
                    </span>
                  </button>
                ))}
              </div>
            ))}
            <button type="button" className="add-material" onClick={addTool}>
              <Plus size={13} />
              Add tool
            </button>
          </div>

          {selected && (
            <div className="material-form">
              <label className="field-row">
                <span>Name</span>
                <input value={selected.name} onChange={(event) => update({ name: event.target.value })} />
                {nameClash && <small className="field-error">Another tool already has this name.</small>}
              </label>
              <label className="field-row">
                <span>Group</span>
                <input
                  list="tool-groups"
                  value={selected.group}
                  placeholder="e.g. Etch/Dry"
                  onChange={(event) => update({ group: event.target.value })}
                />
                <datalist id="tool-groups">
                  {groupNames.map((group) => (
                    <option key={group} value={group} />
                  ))}
                </datalist>
                <small>
                  A new group is made by typing its name; a slash makes a subgroup, as in
                  Deposition/PVD. Empty means ungrouped.
                </small>
              </label>
              <label className="field-row">
                <span>Recipes on this machine</span>
                <textarea
                  rows={4}
                  value={(selected.recipes ?? []).join("\n")}
                  placeholder={"Siva_HZO_300C\nAl2O3_200C"}
                  onChange={(event) =>
                    update({
                      recipes: event.target.value
                        .split("\n")
                        .map((line) => line.trim())
                        .filter(Boolean),
                    })
                  }
                />
                <small>
                  One per line, by the name the machine shows. A step picks one of these in its
                  experiment values to record what it actually ran; they are names, not parameters
                  — the recipe itself lives on the tool.
                </small>
              </label>
              <label className="field-row">
                <span>Notes</span>
                <textarea
                  rows={3}
                  value={selected.notes}
                  onChange={(event) => update({ notes: event.target.value })}
                />
              </label>
              <div className="modal-actions split-actions">
                <button
                  type="button"
                  className="danger-button"
                  title="Remove this tool from the library. Steps and recipes that name it keep the name as text."
                  onClick={() => {
                    onDelete(selected.id);
                    setSelectedId(tools.find((item) => item.id !== selected.id)?.id ?? "");
                  }}
                >
                  <Trash2 size={13} />
                  Delete
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
