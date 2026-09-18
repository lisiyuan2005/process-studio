import { describe, expect, it } from "vitest";

import { clampPanelShares, DEFAULT_PANELS, PANEL_LIMITS, percent } from "./layout";

describe("how the window's width is shared out", () => {
  it("leaves the viewport its share, however wide the panels are dragged", () => {
    const greedy = clampPanelShares({ steps: 0.9, inspector: 0.9 });
    expect(greedy.steps + greedy.inspector).toBeLessThanOrEqual(1 - PANEL_LIMITS.viewport + 1e-9);
    // And it takes it back from the inspector first, so a deliberately wide
    // steps panel is not the one silently undone.
    expect(greedy.steps).toBeGreaterThan(greedy.inspector);
  });

  it("holds each panel between its own limits", () => {
    const thin = clampPanelShares({ steps: 0, inspector: 0 });
    expect(thin.steps).toBe(PANEL_LIMITS.steps.min);
    expect(thin.inspector).toBe(PANEL_LIMITS.inspector.min);

    const wide = clampPanelShares({ steps: 1, inspector: 0.14 });
    expect(wide.steps).toBeLessThanOrEqual(PANEL_LIMITS.steps.max);
  });

  it("survives nonsense without collapsing the layout", () => {
    for (const bad of [NaN, Infinity, -Infinity] as number[]) {
      const shares = clampPanelShares({ steps: bad, inspector: bad });
      expect(Number.isFinite(shares.steps)).toBe(true);
      expect(Number.isFinite(shares.inspector)).toBe(true);
      expect(shares.steps).toBeGreaterThan(0);
    }
  });

  it("leaves a sensible default alone", () => {
    expect(clampPanelShares(DEFAULT_PANELS)).toEqual(DEFAULT_PANELS);
    expect(DEFAULT_PANELS.steps + DEFAULT_PANELS.inspector).toBeLessThan(1 - PANEL_LIMITS.viewport);
  });

  it("writes a share as a percentage the stylesheet can use", () => {
    expect(percent(0.21)).toBe("21.000%");
    expect(percent(1 / 3)).toBe("33.333%");
  });
});
