import { useEffect, useState } from "react";

/** The index of the value in `positions` closest to `um`; 0 for an empty list. */
export function nearestIndex(positions: number[], um: number): number {
  let best = 0;
  for (let index = 1; index < positions.length; index += 1) {
    if (Math.abs(positions[index] - um) < Math.abs(positions[best] - um)) best = index;
  }
  return best;
}

/**
 * A coordinate shown beside a slider and typed into as well.
 *
 * The slider stays for dragging; this is for putting the cut exactly where
 * a number says. What is typed is handed up as it is, and the field then
 * shows where the cut actually went (the nearest plane there is, or the
 * edge when the number is outside the window).
 */
export function CoordinateField({
  value,
  onCommit,
  unit = "µm",
  digits = 3,
  "aria-label": label,
}: {
  value: number;
  onCommit: (value: number) => void;
  unit?: string;
  digits?: number;
  "aria-label"?: string;
}) {
  const shown = value.toFixed(digits);
  const [draft, setDraft] = useState(shown);
  // follows the value (a drag of the slider), but not while typing
  useEffect(() => setDraft(shown), [shown]);
  const commit = () => {
    const parsed = Number(draft.trim());
    if (draft.trim() === "" || !Number.isFinite(parsed)) {
      setDraft(shown);
      return;
    }
    onCommit(parsed);
    // the parent answers with where the cut went; show that, not the typing
    setDraft(shown);
  };
  return (
    <span className="coordinate-field">
      <input
        type="text"
        inputMode="decimal"
        aria-label={label}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") event.currentTarget.blur();
          if (event.key === "Escape") {
            setDraft(shown);
            event.currentTarget.blur();
          }
        }}
      />
      <span>{unit}</span>
    </span>
  );
}
