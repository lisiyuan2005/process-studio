import { describe, expect, it } from "vitest";
import type { SketchShape } from "../types";
import { crowdedAxes, footprint } from "./sketch";

function shape(partial: Partial<SketchShape>): SketchShape {
  return {
    kind: "rectangle",
    operation: "merge",
    parameters: {},
    array: [1, 1, 0, 0],
    ...partial,
  };
}

describe("footprint", () => {
  it("measures each kind of shape by what it covers", () => {
    expect(footprint(shape({ kind: "rectangle", parameters: { size: [0.4, 0.2] } }))).toEqual({
      width: 0.4,
      height: 0.2,
    });
    expect(footprint(shape({ kind: "circle", parameters: { radius: 0.11 } }))).toEqual({
      width: 0.22,
      height: 0.22,
    });
    expect(
      footprint(shape({ kind: "polygon", parameters: { points: [[0, 0], [0.3, 0], [0.3, 0.1]] } })),
    ).toEqual({ width: 0.3, height: 0.1 });
  });

  it("counts a path's own width, since a polyline is that wide on the wafer", () => {
    const line = shape({ kind: "path", parameters: { points: [[0, 0], [0.5, 0]], width: 0.04 } });
    expect(footprint(line)).toEqual({ width: 0.54, height: 0.04 });
  });

  it("is zero for a shape that has not been given its numbers yet", () => {
    expect(footprint(shape({ kind: "polygon", parameters: {} }))).toEqual({ width: 0, height: 0 });
  });
});

describe("crowdedAxes", () => {
  it("names the axis whose pitch is under the feature size", () => {
    const holes = shape({ kind: "circle", parameters: { radius: 0.11 }, array: [4, 2, 0.3, 0.15] });
    expect(crowdedAxes(holes)).toEqual({ x: false, y: true });
  });

  it("says nothing about an axis with a single copy", () => {
    const one = shape({ kind: "circle", parameters: { radius: 0.11 }, array: [1, 1, 0, 0] });
    expect(crowdedAxes(one)).toEqual({ x: false, y: false });
  });

  it("reads a negative pitch by its distance", () => {
    const left = shape({ kind: "rectangle", parameters: { size: [0.4, 0.4] }, array: [3, 1, -0.2, 0] });
    expect(crowdedAxes(left).x).toBe(true);
  });
});
