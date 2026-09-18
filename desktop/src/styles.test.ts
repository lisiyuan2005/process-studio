/**
 * The window must never be wider than the window.
 *
 * The side panels used to be cut off when the window was not wide: at
 * 1120px the inspector lost 52 of its 278 pixels, at 960px -- as narrow as
 * the window could be -- 212 of them, and there was no scrollbar to say so.
 * Three separate things had to be true for that, and none of them is
 * visible when reading the layout:
 *
 * 1. ``.app-shell`` is a grid with only its rows named, so it had one
 *    implicit column sized to its widest row: the topbar, whose min-content
 *    width is about 1170px. The workspace grid below it was stretched to
 *    that, whatever the window was.
 * 2. The topbar could not shrink, so it really did want 1170px.
 * 3. The middle track of the workspace grid had a pixel floor, so when the
 *    three tracks did not fit, the grid overflowed rather than the viewport
 *    giving way -- and what hangs over the edge is the panel on the end.
 *
 * Then ``overflow: hidden`` on the page hid the evidence. These are
 * assertions about the stylesheet because that is where the mistake was;
 * the widths themselves were checked in a browser from 1920px down to
 * 720px, where the inspector now keeps 236px and the viewport 268px.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// The files themselves, as text: the assertions are about what they say.
// (Not through vite's ``?raw``: it hands back the *processed* stylesheet,
// which is not what a reader of styles.css is looking at.)
const read = (name: string) =>
  readFileSync(fileURLToPath(new URL(name, import.meta.url)), "utf8");

const styles = read("./styles.css");
const tauri = JSON.parse(read("../src-tauri/tauri.conf.json")) as {
  app: { windows: { minWidth: number; minHeight: number }[] };
};

/** The declarations of one rule, by its exact selector. */
function rule(selector: string): string {
  const found = styles.match(
    new RegExp(`(^|\\})\\s*${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\{([^}]*)\\}`, "m"),
  );
  expect(found, `no rule for ${selector}`).not.toBeNull();
  return found![2];
}

describe("the layout at every window width", () => {
  it("names the shell's one column, so its rows cannot widen it", () => {
    expect(rule(".app-shell")).toMatch(/grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  });

  it("lets the topbar shrink, so it cannot decide how wide the window is", () => {
    expect(rule(".topbar")).toMatch(/min-width:\s*0/);
  });

  it("gives the viewport, not a side panel, as the column that yields", () => {
    // Anywhere a pixel floor on the middle track comes back -- including
    // inside a media query -- the panel on the end goes over the edge again.
    const tracks = [...styles.matchAll(/\.workspace-grid\s*\{[^}]*grid-template-columns:([^;]*);/g)];
    expect(tracks.length).toBeGreaterThan(0);
    for (const [, columns] of tracks) {
      expect(columns).toContain("minmax(0, 1fr)");
      expect(columns, `a floor on the middle track: ${columns.trim()}`).not.toMatch(
        /minmax\(\s*\d+px/,
      );
    }
  });

  it("is built for a page no wider than the window can be", () => {
    const floor = rule("html, body, #root").match(/min-width:\s*(\d+)px/);
    expect(floor, "the page says no minimum width").not.toBeNull();
    const window = tauri.app.windows[0];
    expect(Number(floor![1])).toBeLessThanOrEqual(window.minWidth);

    const height = rule("html, body, #root").match(/min-height:\s*(\d+)px/);
    expect(Number(height![1])).toBeLessThanOrEqual(window.minHeight);
  });
});
