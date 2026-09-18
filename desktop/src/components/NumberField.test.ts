import { describe, expect, it } from "vitest";

import { typedNumber, withinLimits } from "./NumberField";

describe("typing a number", () => {
  it("reports nothing for what is not a number yet", () => {
    // The bug: <input type="number"> hands back "" for each of these, and a
    // field that parsed on every keystroke read that as 0 and wrote it
    // back -- so the minus sign vanished as it was typed, and a negative
    // coordinate could only be entered digits-first.
    for (const partial of ["-", "", " ", ".", "-.", "1e", "1e-", "+", "abc"]) {
      expect(typedNumber(partial), partial).toBeNull();
    }
  });

  it("reports the number as soon as there is one", () => {
    expect(typedNumber("-1")).toBe(-1);
    expect(typedNumber("-0.5")).toBe(-0.5);
    expect(typedNumber("0.")).toBe(0);
    expect(typedNumber("-.5")).toBe(-0.5);
    expect(typedNumber(" 2.5 ")).toBe(2.5);
    expect(typedNumber("1e-3")).toBe(0.001);
  });

  it("never reports an infinity or a NaN, whatever is typed", () => {
    for (const odd of ["Infinity", "-Infinity", "NaN"]) {
      expect(typedNumber(odd), odd).toBeNull();
    }
  });

  it("holds a value between its limits without inventing one", () => {
    expect(withinLimits(-3, 0)).toBe(0);
    expect(withinLimits(-3)).toBe(-3);
    expect(withinLimits(12, 1, 10)).toBe(10);
    expect(withinLimits(-0.4)).toBe(-0.4);
  });
});
