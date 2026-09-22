import { useEffect, useState } from "react";
import { fromUnit, inUnit, preferredUnit, rememberUnit, unitOption, unitsFor } from "../domain/units";

interface UnitNumberProps {
  /** The stored value, in the canonical unit. */
  value: number | null | undefined;
  /** The unit the stored value is in ("µm", "min", "µm/min", …). */
  unit?: string;
  /** Which parameter this is, so a chosen unit is remembered for it. */
  field: string;
  onCommit: (value: number) => void;
  autoFocus?: boolean;
  placeholder?: string;
  "aria-label"?: string;
}

/**
 * A number in whichever unit the user prefers to type it in.
 *
 * The value handed up is always canonical -- micrometres, minutes, µm/min --
 * so nothing downstream (the flow file, the kernel, the step digest) can
 * tell which unit was used, and switching the picker can never make a step
 * stale. A parameter with no alternative units shows its unit as a label,
 * exactly as before.
 */
export function UnitNumber({
  value,
  unit,
  field,
  onCommit,
  autoFocus,
  placeholder,
  ...rest
}: UnitNumberProps) {
  const options = unitsFor(unit);
  const [chosen, setChosen] = useState(
    () => preferredUnit(field, unit)?.id ?? unit ?? "",
  );
  const active = unitOption(unit, chosen);
  const shown = (canonical: number | null | undefined) =>
    canonical === null || canonical === undefined
      ? ""
      : String(active ? inUnit(canonical, active) : canonical);
  const [draft, setDraft] = useState(() => shown(value));
  // The field follows the value it was given -- another step selected, a
  // recipe loaded, a unit switched -- but not while it is being typed into.
  useEffect(() => setDraft(shown(value)), [value, chosen]);

  const commit = () => {
    const parsed = Number(draft.trim());
    if (draft.trim() === "" || !Number.isFinite(parsed)) {
      setDraft(shown(value));
      return;
    }
    const next = active ? fromUnit(parsed, active) : parsed;
    // Only a different number is reported. Leaving a field alone, or
    // switching the unit it is shown in, is not an edit -- and an edit is
    // what makes a step and everything after it stale.
    if (next !== value) onCommit(next);
  };

  return (
    <span className={options.length > 1 ? "number-input-wrap with-units" : "number-input-wrap"}>
      <input
        {...rest}
        type="text"
        inputMode="decimal"
        autoFocus={autoFocus}
        placeholder={placeholder}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => event.key === "Enter" && event.currentTarget.blur()}
      />
      {options.length > 1 ? (
        <select
          className="unit-picker"
          aria-label={`Unit for ${field}`}
          value={chosen}
          onChange={(event) => {
            // Commit what is on screen in the old unit first, so switching
            // converts the number instead of reinterpreting it.
            commit();
            setChosen(event.target.value);
            rememberUnit(field, unit, event.target.value);
          }}
        >
          {options.map((option) => (
            <option key={option.id} value={option.id}>
              {option.id}
            </option>
          ))}
        </select>
      ) : (
        unit && <span>{unit}</span>
      )}
    </span>
  );
}
