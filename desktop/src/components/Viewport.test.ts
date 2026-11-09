import { describe, expect, it } from "vitest";

import {
  MAX_TILES,
  restoredCamera,
  tileOffsets,
  type CameraMemory,
  type TilingState,
} from "./Viewport";

const tiling = (rest: Partial<TilingState> = {}): TilingState => ({
  on: true,
  countX: 3,
  countY: 3,
  pitchX: 0.3,
  pitchY: 0.3,
  ...rest,
});

describe("showing the cell as the array it stands for", () => {
  it("is one copy at the origin until it is turned on", () => {
    expect(tileOffsets(tiling({ on: false }))).toEqual([[0, 0]]);
  });

  it("centres the copies on the cell that was simulated", () => {
    // Odd counts put a copy exactly where the simulated cell is, so turning
    // tiling on does not move what is already on screen.
    const three = tileOffsets(tiling({ countX: 3, countY: 1 }));
    expect(three).toEqual([
      [-0.3, 0],
      [0, 0],
      [0.3, 0],
    ]);
    // An even count straddles it, which is what "centred" has to mean there.
    expect(tileOffsets(tiling({ countX: 2, countY: 1 }))).toEqual([
      [-0.15, 0],
      [0.15, 0],
    ]);
  });

  it("lays out both directions", () => {
    const grid = tileOffsets(tiling({ countX: 2, countY: 2, pitchX: 1, pitchY: 2 }));
    expect(grid).toHaveLength(4);
    expect(new Set(grid.map(([x, y]) => `${x},${y}`))).toEqual(
      new Set(["-0.5,-1", "0.5,-1", "-0.5,1", "0.5,1"]),
    );
  });

  it("takes whole copies only", () => {
    expect(tileOffsets(tiling({ countX: 2.6, countY: 1 }))).toHaveLength(3);
    expect(tileOffsets(tiling({ countX: 0, countY: 0 }))).toEqual([[0, 0]]);
    expect(tileOffsets(tiling({ countX: -4, countY: 1 }))).toEqual([[0, 0]]);
  });

  it("refuses to draw more copies than a view can carry", () => {
    const many = tiling({ countX: 100, countY: 100 });
    expect(100 * 100).toBeGreaterThan(MAX_TILES);
    expect(tileOffsets(many)).toEqual([[0, 0]]);
    // Right up to the limit it still lays them out.
    const edge = tiling({ countX: MAX_TILES, countY: 1 });
    expect(tileOffsets(edge)).toHaveLength(MAX_TILES);
  });
});


describe("keeping the view across a step", () => {
  const turned: CameraMemory = { position: [3, -3, 2.4], target: [0, 0, 0], span: 1.2 };

  it("puts the camera back exactly when the model is the same size", () => {
    const put = restoredCamera(turned, 1.2);
    expect(put.position).toEqual([3, -3, 2.4]);
    expect(put.target).toEqual([0, 0, 0]);
  });

  it("stands further back from a taller step, in the same direction", () => {
    const put = restoredCamera(turned, 2.4);
    expect(put.position).toEqual([6, -6, 4.8]);
    // The direction the user chose is what is kept; only the distance moves.
    expect(put.near).toBeCloseTo(2.4 * 0.02);
    expect(put.far).toBeCloseTo(2.4 * 40);
  });

  it("carries a panned target with it, so the same corner stays in the middle", () => {
    const panned: CameraMemory = { position: [3, -3, 2.4], target: [0.5, 0, 0], span: 1.2 };
    expect(restoredCamera(panned, 2.4).target).toEqual([1, 0, 0]);
  });

  it("does not divide by a span it never had", () => {
    const fresh: CameraMemory = { position: [3, -3, 2.4], target: [0, 0, 0], span: 0 };
    expect(restoredCamera(fresh, 2.4).position).toEqual([3, -3, 2.4]);
  });
});

describe("coordinates typed into the view", () => {
  it("puts a typed section position on the nearest plane there is", async () => {
    const { nearestIndex } = await import("./CoordinateField");
    const planes = [-0.5, -0.44, -0.38, 0, 0.38];
    expect(nearestIndex(planes, -0.44)).toBe(1);
    expect(nearestIndex(planes, -0.42)).toBe(1);
    expect(nearestIndex(planes, 0.1)).toBe(3);
    expect(nearestIndex(planes, -9)).toBe(0);
    expect(nearestIndex(planes, 9)).toBe(4);
    expect(nearestIndex([], 1)).toBe(0);
  });

  it("turns a typed clip position into the slider's fraction, kept inside the window", async () => {
    const { clipFraction } = await import("./Viewport");
    const bounds = { xMin: -1, xMax: 1, yMin: 0, yMax: 4, zMin: -0.5, zMax: 0.5 };
    expect(clipFraction({ axis: "x", fraction: 0.5, flip: false }, bounds, 0.5)).toBeCloseTo(0.75);
    expect(clipFraction({ axis: "y", fraction: 0.5, flip: false }, bounds, 1)).toBeCloseTo(0.25);
    expect(clipFraction({ axis: "z", fraction: 0.5, flip: false }, bounds, 7)).toBe(1);
    expect(clipFraction({ axis: "z", fraction: 0.5, flip: false }, bounds, -7)).toBe(0);
    expect(clipFraction({ axis: null, fraction: 0.3, flip: false }, bounds, 1)).toBe(0.3);
  });

  it("reads the view's azimuth and elevation off the camera", async () => {
    const { viewAngles } = await import("./Viewport");
    const front = viewAngles([1, -1, 0], [0, 0, 0]);
    expect(front.azimuth).toBeCloseTo(315);
    expect(front.elevation).toBeCloseTo(0);
    const above = viewAngles([0, 0, 5], [0, 0, 0]);
    expect(above.elevation).toBeCloseTo(90);
    const side = viewAngles([0, 2, 2], [0, 0, 0]);
    expect(side.azimuth).toBeCloseTo(90);
    expect(side.elevation).toBeCloseTo(45);
  });
});

describe("looking along an axis", () => {
  it("puts the camera on that axis, and above with +y up the screen", async () => {
    const { cameraAlong, viewAngles } = await import("./Viewport");
    const fromX = cameraAlong([1, 0, 0], [0, 0, 0], 5);
    expect(fromX[0]).toBeCloseTo(5);
    expect(viewAngles(fromX, [0, 0, 0]).azimuth).toBeCloseTo(0);
    const above = cameraAlong([0, 0, 1], [1, 2, 3], 4);
    expect(above[2]).toBeCloseTo(7);
    expect(above[1]).toBeLessThan(2); // a hair towards −y
    expect(viewAngles(above, [1, 2, 3]).elevation).toBeGreaterThan(89.9);
  });
});
