/** Units a number can be typed in, without changing what is stored.
 *
 * Every parameter keeps the unit the kernel reads: micrometres for lengths,
 * minutes for times, µm/min for rates. A recipe written in nanometres and
 * seconds is the same recipe -- so the unit is a property of the person
 * typing, not of the flow. It is remembered per parameter and applied
 * wherever that parameter is shown; the stored value, the flow file and the
 * step digest never see it, which is why switching a unit can never make a
 * step stale.
 */

export interface UnitOption {
  /** How it is written next to the field. */
  id: string;
  /** canonical = shown × factor. */
  factor: number;
}

/** Alternatives for a canonical unit, the canonical one included, or none. */
const FAMILIES: Record<string, UnitOption[]> = {
  "µm": [
    { id: "nm", factor: 0.001 },
    { id: "µm", factor: 1 },
  ],
  "min": [
    { id: "s", factor: 1 / 60 },
    { id: "min", factor: 1 },
    { id: "h", factor: 60 },
  ],
  "µm/min": [
    { id: "nm/min", factor: 0.001 },
    { id: "µm/min", factor: 1 },
    { id: "nm/s", factor: 0.06 },
    { id: "µm/h", factor: 1 / 60 },
  ],
  "µm/cycle": [
    { id: "nm/cycle", factor: 0.001 },
    { id: "µm/cycle", factor: 1 },
  ],
};

/** The units this value can be typed in; empty when there is only one. */
export function unitsFor(canonical: string | undefined): UnitOption[] {
  if (!canonical) return [];
  return FAMILIES[canonical] ?? [];
}

export function unitOption(canonical: string | undefined, id: string): UnitOption | undefined {
  return unitsFor(canonical).find((unit) => unit.id === id);
}

/** What a stored value reads as in ``unit``, short of trailing zeros. */
export function inUnit(canonical: number, unit: UnitOption): number {
  const shown = canonical / unit.factor;
  // 0.05 µm in nm is 50.000000000000004 in binary floating point; a field
  // that shows that is worse than one that rounds at the twelfth digit.
  return Number(shown.toPrecision(12));
}

/** What a typed number means in the canonical unit. */
export function fromUnit(shown: number, unit: UnitOption): number {
  return Number((shown * unit.factor).toPrecision(12));
}

const STORAGE_KEY = "process-studio:units";

function readAll(): Record<string, string> {
  try {
    const raw = globalThis.localStorage?.getItem(STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    return parsed && typeof parsed === "object" ? (parsed as Record<string, string>) : {};
  } catch {
    return {};
  }
}

/** The unit this parameter was last typed in, or the canonical one. */
export function preferredUnit(field: string, canonical: string | undefined): UnitOption | undefined {
  const options = unitsFor(canonical);
  if (options.length === 0) return undefined;
  const chosen = readAll()[`${field}:${canonical}`];
  return options.find((unit) => unit.id === chosen) ?? options.find((unit) => unit.id === canonical);
}

/** Remember a choice for every field that shows this parameter. */
export function rememberUnit(field: string, canonical: string | undefined, unit: string): void {
  if (unitsFor(canonical).length === 0) return;
  try {
    const all = readAll();
    all[`${field}:${canonical}`] = unit;
    globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(all));
  } catch {
    // A browser with storage blocked keeps the canonical unit; nothing else
    // depends on the choice.
  }
}
