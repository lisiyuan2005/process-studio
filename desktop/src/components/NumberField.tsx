import { useState } from "react";
import type { KeyboardEvent } from "react";

/**
 * A number field you can actually type a number into.
 *
 * ``<input type="number">`` reports an empty string for anything it cannot
 * parse *yet* -- "-", "1e", "." -- so a controlled field that parses on
 * every keystroke reads the first minus sign as nothing, writes 0 back, and
 * erases it. That is why a negative coordinate could only be entered by
 * typing the digits and then going back for the sign.
 *
 * So this holds the text while it is being edited and reports only the
 * numbers it can parse; the value it shows goes back to the caller's as
 * soon as the field is left. Arrow keys still step, because these are
 * fields for dimensions and nudging one is worth keeping.
 */

/** The number the text means, or null while it does not mean one yet. */
export function typedNumber(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  // Number("") is 0 and Number(" ") is 0; neither is what was typed.
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

export function withinLimits(value: number, min?: number, max?: number): number {
  if (min !== undefined && value < min) return min;
  if (max !== undefined && value > max) return max;
  return value;
}

interface NumberFieldProps {
  value: number;
  onChange: (value: number) => void;
  step?: number;
  min?: number;
  max?: number;
  placeholder?: string;
  disabled?: boolean;
  title?: string;
  "aria-label"?: string;
  className?: string;
}

export function NumberField({
  value,
  onChange,
  step = 1,
  min,
  max,
  ...rest
}: NumberFieldProps) {
  const [draft, setDraft] = useState<string | null>(null);

  const nudge = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    const by = (event.shiftKey ? step * 10 : step) * (event.key === "ArrowUp" ? 1 : -1);
    // From what is on screen, so a nudge follows a half-typed number.
    const from = draft === null ? value : (typedNumber(draft) ?? value);
    setDraft(null);
    onChange(withinLimits(round(from + by), min, max));
  };

  return (
    <input
      {...rest}
      type="text"
      inputMode="decimal"
      value={draft ?? String(value)}
      onChange={(event) => {
        setDraft(event.target.value);
        const parsed = typedNumber(event.target.value);
        if (parsed !== null) onChange(withinLimits(parsed, min, max));
      }}
      onKeyDown={nudge}
      onBlur={() => setDraft(null)}
    />
  );
}

/** Keep a nudge from turning 0.3 into 0.30000000000000004. */
function round(value: number): number {
  return Number(value.toFixed(9));
}
