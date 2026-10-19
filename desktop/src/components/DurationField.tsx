import { useEffect, useState } from "react";
import { joinMinutes, splitMinutes } from "../domain/units";

interface DurationFieldProps {
  /** The stored time, in minutes. */
  value: number | null | undefined;
  onCommit: (minutes: number) => void;
  "aria-label"?: string;
}

const PARTS = [
  { key: "h", label: "h" },
  { key: "min", label: "min" },
  { key: "s", label: "s" },
] as const;

type Drafts = Record<(typeof PARTS)[number]["key"], string>;

function draftsOf(value: number | null | undefined): Drafts {
  if (value === null || value === undefined || !Number.isFinite(value)) return { h: "", min: "", s: "" };
  const { h, min, s } = splitMinutes(value);
  return { h: String(h), min: String(min), s: String(s) };
}

/**
 * A time typed as hours, minutes and seconds. What is handed up is minutes,
 * the unit the kernel reads, so the flow file and the step digest never see
 * how it was typed. Any of the three may be left empty (it counts as 0) or
 * run over (90 s is 1 min 30 s once the field is left).
 */
export function DurationField({ value, onCommit, ...rest }: DurationFieldProps) {
  const [drafts, setDrafts] = useState(() => draftsOf(value));
  // follows the value it was given, but not while it is being typed into
  useEffect(() => setDrafts(draftsOf(value)), [value]);

  const commit = () => {
    const numbers = PARTS.map(({ key }) => (drafts[key].trim() === "" ? 0 : Number(drafts[key].trim())));
    if (numbers.some((number) => !Number.isFinite(number) || number < 0)) {
      setDrafts(draftsOf(value));
      return;
    }
    const next = joinMinutes(numbers[0], numbers[1], numbers[2]);
    // only a different time is an edit (an edit makes the step stale)
    if (next !== value) onCommit(next);
    else setDrafts(draftsOf(value));
  };

  return (
    <span className="duration-field" role="group" aria-label={rest["aria-label"]}>
      {PARTS.map(({ key, label }) => (
        <span key={key} className="number-input-wrap">
          <input
            type="text"
            inputMode="decimal"
            aria-label={`${rest["aria-label"] ?? "Time"} (${label})`}
            placeholder="0"
            value={drafts[key]}
            onChange={(event) => setDrafts((current) => ({ ...current, [key]: event.target.value }))}
            onBlur={commit}
            onKeyDown={(event) => event.key === "Enter" && event.currentTarget.blur()}
          />
          <span>{label}</span>
        </span>
      ))}
    </span>
  );
}
