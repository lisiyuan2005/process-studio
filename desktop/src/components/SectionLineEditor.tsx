import { Trash, X } from "lucide-react";
import { useState } from "react";
import type { SectionLine } from "../types";

interface SectionLineEditorProps {
  line: SectionLine;
  /** The project window, so a point outside it can be flagged. */
  window: { xMin: number; xMax: number; yMin: number; yMax: number };
  isNew: boolean;
  onSave: (line: SectionLine) => void;
  onDelete: () => void;
  onClose: () => void;
}

/** Name and exact end points of one AA–BB line, in micrometres. */
export function SectionLineEditor({ line, window, isNew, onSave, onDelete, onClose }: SectionLineEditorProps) {
  const [name, setName] = useState(line.name);
  const [fields, setFields] = useState({
    ax: String(line.start[0]),
    ay: String(line.start[1]),
    bx: String(line.end[0]),
    by: String(line.end[1]),
  });
  const numbers = Object.fromEntries(
    Object.entries(fields).map(([key, value]) => [key, Number(value)]),
  ) as Record<keyof typeof fields, number>;
  const allNumbers = Object.values(numbers).every((value) => Number.isFinite(value));
  const distinct = allNumbers && (numbers.ax !== numbers.bx || numbers.ay !== numbers.by);
  const inside = (x: number, y: number) =>
    x >= window.xMin && x <= window.xMax && y >= window.yMin && y <= window.yMax;
  const outside =
    allNumbers && (!inside(numbers.ax, numbers.ay) || !inside(numbers.bx, numbers.by));
  const problem = !allNumbers
    ? "Every coordinate needs a number, in micrometres."
    : !distinct
      ? "A and B must be different points."
      : outside
        ? `A point lies outside the project window (x ${window.xMin}..${window.xMax}, y ${window.yMin}..${window.yMax} µm); the cut is clipped to the window.`
        : undefined;
  const length = distinct ? Math.hypot(numbers.bx - numbers.ax, numbers.by - numbers.ay) : 0;

  const field = (key: keyof typeof fields, label: string) => (
    <label className="field-row compact">
      <span>{label}</span>
      <span className="number-input-wrap">
        <input
          type="text"
          inputMode="decimal"
          value={fields[key]}
          onChange={(event) => setFields({ ...fields, [key]: event.target.value })}
        />
        <span>µm</span>
      </span>
    </label>
  );

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Section line">
      <div className="modal-card line-modal">
        <header className="modal-header">
          <div>
            <span className="eyebrow">SECTION LINE</span>
            <h2>{isNew ? "New AA–BB line" : line.name}</h2>
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>
        <div className="modal-body">
          <label className="field-row">
            <span>Name</span>
            <input value={name} onChange={(event) => setName(event.target.value)} />
          </label>
          <span className="section-label">POINT A</span>
          <div className="pair-grid">
            {field("ax", "x")}
            {field("ay", "y")}
          </div>
          <span className="section-label">POINT B</span>
          <div className="pair-grid">
            {field("bx", "x")}
            {field("by", "y")}
          </div>
          <p className="numerics-note">
            {distinct ? `Length ${length.toFixed(3)} µm. ` : ""}
            The cut runs from A to B; the section's horizontal axis is the distance from A.
          </p>
          {problem && !(distinct && outside) && (
            <div className="error-box">
              <span>{problem}</span>
            </div>
          )}
          {distinct && outside && (
            <div className="warning-box">
              <span>{problem}</span>
            </div>
          )}
        </div>
        <div className="modal-actions">
          {!isNew && (
            <button type="button" className="danger-button" onClick={onDelete}>
              <Trash size={13} />
              Delete
            </button>
          )}
          <span className="footer-spacer" />
          <button type="button" className="secondary-button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="primary-button modal-save"
            disabled={!distinct}
            onClick={() =>
              onSave({
                ...line,
                name: name.trim() || line.name,
                start: [numbers.ax, numbers.ay],
                end: [numbers.bx, numbers.by],
              })
            }
          >
            {isNew ? "Add line" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
