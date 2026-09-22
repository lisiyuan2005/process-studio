import { beforeEach, describe, expect, it } from "vitest";
import { fromUnit, inUnit, preferredUnit, rememberUnit, unitOption, unitsFor } from "./units";

/** The tests run on node, which has no localStorage; this is the one part of
 * a browser these functions touch. */
function memoryStorage(): Storage {
  const entries = new Map<string, string>();
  return {
    get length() {
      return entries.size;
    },
    clear: () => entries.clear(),
    getItem: (key: string) => entries.get(key) ?? null,
    key: (index: number) => [...entries.keys()][index] ?? null,
    removeItem: (key: string) => void entries.delete(key),
    setItem: (key: string, value: string) => void entries.set(key, value),
  };
}

describe("units", () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, "localStorage", {
      value: memoryStorage(),
      configurable: true,
    });
  });

  it("offers alternatives only where there are some", () => {
    expect(unitsFor("µm").map((unit) => unit.id)).toEqual(["nm", "µm"]);
    expect(unitsFor("min").map((unit) => unit.id)).toEqual(["s", "min", "h"]);
    expect(unitsFor("°C")).toEqual([]);
    expect(unitsFor(undefined)).toEqual([]);
  });

  it("converts both ways without floating-point litter", () => {
    const nm = unitOption("µm", "nm")!;
    expect(inUnit(0.05, nm)).toBe(50);
    expect(fromUnit(50, nm)).toBe(0.05);
    const seconds = unitOption("min", "s")!;
    expect(inUnit(1, seconds)).toBe(60);
    expect(fromUnit(90, seconds)).toBe(1.5);
    const hours = unitOption("min", "h")!;
    expect(fromUnit(2, hours)).toBe(120);
  });

  it("reads a rate in the unit it was typed in", () => {
    const perSecond = unitOption("µm/min", "nm/s")!;
    // 1 nm/s is 60 nm/min is 0.06 µm/min.
    expect(fromUnit(1, perSecond)).toBe(0.06);
    expect(inUnit(0.06, perSecond)).toBe(1);
  });

  it("remembers a choice per parameter and falls back to the canonical unit", () => {
    expect(preferredUnit("target", "µm")?.id).toBe("µm");
    rememberUnit("target", "µm", "nm");
    expect(preferredUnit("target", "µm")?.id).toBe("nm");
    // Another parameter of the same family is untouched.
    expect(preferredUnit("rate_per_cycle", "µm/cycle")?.id).toBe("µm/cycle");
    // A parameter with no alternatives has no unit to remember.
    rememberUnit("temperature_c", "°C", "K");
    expect(preferredUnit("temperature_c", "°C")).toBeUndefined();
  });
});
