import { describe, expect, it } from "vitest";

import {
  claimRoot,
  closeTab,
  openTab,
  reportTab,
  stepTab,
  tabName,
  tabsFrom,
  type TabHandle,
} from "./tabs";

const handle = (root: string, rest: Partial<TabHandle> = {}): TabHandle => ({
  root,
  name: tabName(root),
  busy: false,
  unsaved: false,
  ...rest,
});

describe("the tabs in the window", () => {
  it("starts from what was open, and always has a tab", () => {
    expect(tabsFrom([], 0).tabs).toHaveLength(1);
    expect(tabsFrom([], 0).tabs[0].handle.root).toBe("");
    const three = tabsFrom(["/a", "/b", "/c"], 2);
    expect(three.tabs.map((tab) => tab.initialRoot)).toEqual(["/a", "/b", "/c"]);
    expect(three.active).toBe(2);
    // A stored index that no longer fits is brought back inside.
    expect(tabsFrom(["/a"], 7).active).toBe(0);
    expect(tabsFrom(["/a"], -3).active).toBe(0);
  });

  it("names a tab after the folder, not the path", () => {
    expect(tabName("/home/someone/Projects/3D-DRAM")).toBe("3D-DRAM");
    expect(tabName("C:\\Users\\someone\\3D-DRAM\\")).toBe("3D-DRAM");
    expect(tabName("")).toBe("New project");
  });

  it("opens a workspace once: the tab that has it comes forward", () => {
    let state = tabsFrom(["/a"], 0);
    state = openTab(state, "/b", 2);
    expect(state.tabs).toHaveLength(2);
    expect(state.active).toBe(1);

    state = openTab(state, "/a", 3);
    expect(state.tabs).toHaveLength(2);
    expect(state.active).toBe(0);

    // Home pages are not workspaces, so they do not collapse into one.
    state = openTab(state, "", 4);
    state = openTab(state, "", 5);
    expect(state.tabs).toHaveLength(4);
  });

  it("refuses a workspace another tab is holding and shows that tab", () => {
    const state = tabsFrom(["/a", "/b"], 1);
    const asked = claimRoot(state, state.tabs[1].key, "/a");
    expect(asked.allowed).toBe(false);
    expect(asked.state.active).toBe(0);

    // Its own workspace, and one nobody holds, are fine.
    expect(claimRoot(state, state.tabs[0].key, "/a").allowed).toBe(true);
    expect(claimRoot(state, state.tabs[1].key, "/c").allowed).toBe(true);
  });

  it("keeps the workspace on screen when a tab to its left closes", () => {
    let state = tabsFrom(["/a", "/b", "/c"], 2);
    state = closeTab(state, state.tabs[0].key, 9);
    expect(state.tabs.map((tab) => tab.initialRoot)).toEqual(["/b", "/c"]);
    expect(state.tabs[state.active].initialRoot).toBe("/c");

    // Closing the current one shows its neighbour.
    state = closeTab(state, state.tabs[1].key, 9);
    expect(state.tabs[state.active].initialRoot).toBe("/b");

    // And the window never ends up with none.
    state = closeTab(state, state.tabs[0].key, 9);
    expect(state.tabs).toHaveLength(1);
    expect(state.tabs[0].handle.root).toBe("");
  });

  it("leaves the state alone when a tab reports nothing new", () => {
    const state = tabsFrom(["/a"], 0);
    const key = state.tabs[0].key;
    expect(reportTab(state, key, handle("/a"))).toBe(state);
    expect(reportTab(state, 404, handle("/zzz"))).toBe(state);

    const busy = reportTab(state, key, handle("/a", { busy: true }));
    expect(busy).not.toBe(state);
    expect(busy.tabs[0].handle.busy).toBe(true);
    // A tab that opened a different workspace says so, and keeps its key.
    const moved = reportTab(state, key, handle("/b"));
    expect(moved.tabs[0].handle.root).toBe("/b");
    expect(moved.tabs[0].key).toBe(key);
  });

  it("steps round the strip", () => {
    const state = tabsFrom(["/a", "/b", "/c"], 2);
    expect(stepTab(state, 1).active).toBe(0);
    expect(stepTab(state, -1).active).toBe(1);
    expect(stepTab(tabsFrom(["/a"], 0), 1).active).toBe(0);
  });
});
