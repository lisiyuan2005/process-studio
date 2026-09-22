import { describe, expect, it } from "vitest";
import { cyclic, defaultParameters, defaultsForNewTool, depositThickness } from "./parameters";

describe("cyclic tools", () => {
  it("recognises the machines whose recipes are written in cycles", () => {
    expect(cyclic("ALD")).toBe(true);
    expect(cyclic("Savannah ALD")).toBe(true);
    expect(cyclic("Deposition/ALD/Oxides")).toBe(true);
    expect(cyclic("ale-01")).toBe(true);
  });

  it("is not fooled by a word that merely contains the letters", () => {
    expect(cyclic("Scalded")).toBe(false);
    expect(cyclic("Wet Bench")).toBe(false);
    expect(cyclic("")).toBe(false);
    expect(cyclic(null)).toBe(false);
  });
});

describe("defaultParameters", () => {
  it("gives a cyclic deposition its cycles and a plain one its thickness", () => {
    expect(defaultParameters("deposit", "ALD")).toEqual({ cycles: 100, rate_per_cycle: 0.0001 });
    expect(defaultParameters("deposit", "Sputter")).toEqual({ target: 0.05 });
    expect(defaultParameters("deposit")).toEqual({ target: 0.05 });
  });

  it("has nothing cyclic to offer the other process types", () => {
    expect(defaultParameters("etch", "ALE")).toEqual({ target: 0.1, directional_fraction: 1 });
    expect(defaultParameters("cmp", "ALD")).toEqual({ target_z: 0 });
  });
});

describe("defaultsForNewTool", () => {
  it("swaps a default set over when the tool or the type changes", () => {
    expect(defaultsForNewTool("deposit", "ALD", { target: 0.05 })).toEqual({
      cycles: 100,
      rate_per_cycle: 0.0001,
    });
    // An etch turned into a deposition still holds the etch's own defaults.
    expect(defaultsForNewTool("deposit", "", { target: 0.1, directional_fraction: 1 })).toEqual({
      target: 0.05,
    });
  });

  it("leaves numbers somebody typed alone", () => {
    expect(defaultsForNewTool("deposit", "ALD", { target: 0.2 })).toBeNull();
    expect(defaultsForNewTool("deposit", "ALD", { target: 0.05, rate: 0.01 })).toBeNull();
    expect(defaultsForNewTool("deposit", "Sputter", { cycles: 240, rate_per_cycle: 0.0001 })).toBeNull();
  });

  it("says nothing when the defaults are already the right ones", () => {
    expect(defaultsForNewTool("deposit", "ALD", { cycles: 100, rate_per_cycle: 0.0001 })).toBeNull();
    expect(defaultsForNewTool("deposit", "Sputter", { target: 0.05 })).toBeNull();
  });
});

describe("depositThickness", () => {
  it("reads the three spellings the kernel reads, in the kernel's order", () => {
    expect(depositThickness({ target: 0.02 })).toEqual({ um: 0.02, from: "target" });
    expect(depositThickness({ cycles: 240, rate_per_cycle: 0.0009 })).toEqual({
      um: 0.216,
      from: "cycles",
    });
    expect(depositThickness({ time_min: 2, rate: 0.01 })).toEqual({ um: 0.02, from: "time" });
    // A target wins, as it does in the kernel.
    expect(depositThickness({ target: 0.05, cycles: 10, rate_per_cycle: 0.001 })?.from).toBe("target");
  });

  it("has nothing to say about half a specification", () => {
    expect(depositThickness({ cycles: 240 })).toBeNull();
    expect(depositThickness({ rate: 0.01, temperature_c: 300 })).toBeNull();
    expect(depositThickness({ mode: "conformal" })).toBeNull();
  });
});
