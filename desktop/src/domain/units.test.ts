import { beforeEach, describe, expect, it } from "vitest";
import { fromUnit, inUnit, joinMinutes, preferredUnit, rememberUnit, splitMinutes, unitOption, unitsFor } from "./units";

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
    // a value that is not a round number of minutes comes back as typed
    for (const typed of [70, 7, 45.5, 3599, 0.1]) {
      expect(inUnit(fromUnit(typed, seconds), seconds)).toBe(typed);
    }
  });

  it("splits a time into hours, minutes and seconds and back", () => {
    expect(splitMinutes(joinMinutes(0, 0, 70))).toEqual({ h: 0, min: 1, s: 10 });
    expect(splitMinutes(joinMinutes(1, 30, 5))).toEqual({ h: 1, min: 30, s: 5 });
    expect(splitMinutes(joinMinutes(0, 0, 2.5))).toEqual({ h: 0, min: 0, s: 2.5 });
    expect(splitMinutes(1.5)).toEqual({ h: 0, min: 1, s: 30 });
    expect(splitMinutes(fromUnit(70, unitOption("min", "s")!))).toEqual({ h: 0, min: 1, s: 10 });
    expect(joinMinutes(0, 1, 30)).toBe(1.5);
    expect(joinMinutes(2, 0, 0)).toBe(120);
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
    // A field can start in another unit than the canonical one (etch rates in nm/s)
    expect(preferredUnit("material_rate", "µm/min", "nm/s")?.id).toBe("nm/s");
    rememberUnit("material_rate", "µm/min", "nm/min");
    expect(preferredUnit("material_rate", "µm/min", "nm/s")?.id).toBe("nm/min");
    // A parameter with no alternatives has no unit to remember.
    rememberUnit("temperature_c", "°C", "K");
    expect(preferredUnit("temperature_c", "°C")).toBeUndefined();
  });
});
