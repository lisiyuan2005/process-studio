import { useMemo, useState } from "react";
import type { ToolDefinition } from "../types";

const CUSTOM = " custom";

/**
 * A tool name from the library, grouped, or typed by hand. A value that is
 * not in the library is kept as free text, so a workspace whose tools
 * changed still shows what a step or recipe was written with.
 */
export function ToolPicker({
  value,
  tools,
  onChange,
  placeholder = "none",
}: {
  value: string;
  tools: ToolDefinition[];
  onChange: (tool: string) => void;
  placeholder?: string;
}) {
  const known = tools.some((tool) => tool.name === value);
  const [custom, setCustom] = useState(!known && value !== "");
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
  const showCustom = custom || (!known && value !== "");

  return (
    <span className="tool-picker">
      <select
        value={showCustom ? CUSTOM : value}
        onChange={(event) => {
          if (event.target.value === CUSTOM) {
            setCustom(true);
            return;
          }
          setCustom(false);
          onChange(event.target.value);
        }}
      >
        <option value="">{placeholder}</option>
        {groups.map(([group, members]) =>
          group ? (
            <optgroup key={group} label={group}>
              {members.map((tool) => (
                <option key={tool.id} value={tool.name}>
                  {tool.name}
                </option>
              ))}
            </optgroup>
          ) : (
            members.map((tool) => (
              <option key={tool.id} value={tool.name}>
                {tool.name}
              </option>
            ))
          ),
        )}
        <option value={CUSTOM}>Other (type a name)…</option>
      </select>
      {showCustom && (
        <input
          type="text"
          value={value}
          placeholder="tool name"
          autoFocus={custom}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
    </span>
  );
}
