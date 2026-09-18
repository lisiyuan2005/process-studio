import { describe, expect, it } from "vitest";

import { MAX_TILES, tileOffsets, type TilingState } from "./Viewport";

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
