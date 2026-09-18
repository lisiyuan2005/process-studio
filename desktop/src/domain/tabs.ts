/**
 * Which workspaces are open in which tab, and which one is on screen.
 *
 * Kept apart from the shell that renders it because the rules are worth
 * stating on their own: a window always has a tab, one workspace is only
 * ever in one tab (two tabs autosaving the same project would each
 * overwrite the other), and closing the tab left of the current one must
 * not move which workspace is on screen.
 */

export interface TabHandle {
  /** The workspace this tab holds, "" while it shows the home page. */
  root: string;
  name: string;
  busy: boolean;
  unsaved: boolean;
}

export interface Tab {
  /** Identity for React and for the per-tab callbacks; never reused. */
  key: number;
  /** The workspace to open on mount; where it went afterwards is ``handle``. */
  initialRoot: string;
  handle: TabHandle;
}

export interface TabState {
  tabs: Tab[];
  active: number;
}

/** What to call a tab before its workspace has answered. */
export function tabName(root: string): string {
  if (!root) return "New project";
  const parts = root.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? root;
}

export function makeTab(root: string, key: number): Tab {
  return {
    key,
    initialRoot: root,
    handle: { root, name: tabName(root), busy: false, unsaved: false },
  };
}

export function tabsFrom(roots: string[], active: number, firstKey = 1): TabState {
  const tabs = (roots.length > 0 ? roots : [""]).map((root, index) =>
    makeTab(root, firstKey + index),
  );
  return { tabs, active: Math.min(Math.max(active, 0), tabs.length - 1) };
}

export function holderOf(state: TabState, root: string, except = -1): number {
  if (!root) return -1;
  return state.tabs.findIndex((tab) => tab.key !== except && tab.handle.root === root);
}

/** Open a workspace: the tab that already holds it, or a new tab at the end. */
export function openTab(state: TabState, root: string, key: number): TabState {
  const already = holderOf(state, root);
  if (already >= 0) return { ...state, active: already };
  return { tabs: [...state.tabs, makeTab(root, key)], active: state.tabs.length };
}

/** Close a tab. The window keeps a tab: the last one leaves a home page. */
export function closeTab(state: TabState, key: number, freshKey: number): TabState {
  const index = state.tabs.findIndex((tab) => tab.key === key);
  if (index < 0) return state;
  const left = state.tabs.filter((tab) => tab.key !== key);
  if (left.length === 0) return { tabs: [makeTab("", freshKey)], active: 0 };
  // Closing a tab to the left of the current one must not change which
  // workspace is on screen; closing the current one shows its neighbour.
  const active = state.active > index ? state.active - 1 : Math.min(state.active, left.length - 1);
  return { tabs: left, active };
}

/** What a tab reports about itself. Returns the same state when nothing moved. */
export function reportTab(state: TabState, key: number, handle: TabHandle): TabState {
  const index = state.tabs.findIndex((tab) => tab.key === key);
  if (index < 0) return state;
  const was = state.tabs[index].handle;
  if (
    was.root === handle.root &&
    was.name === handle.name &&
    was.busy === handle.busy &&
    was.unsaved === handle.unsaved
  ) {
    return state;
  }
  const tabs = [...state.tabs];
  tabs[index] = { ...tabs[index], handle };
  return { ...state, tabs };
}

/**
 * Whether a tab may hold a workspace. When another tab already does, that
 * tab comes forward and the asking tab is told no.
 */
export function claimRoot(
  state: TabState,
  key: number,
  root: string,
): { allowed: boolean; state: TabState } {
  const taken = holderOf(state, root, key);
  if (taken < 0) return { allowed: true, state };
  return { allowed: false, state: { ...state, active: taken } };
}

/** Ctrl+Tab and Ctrl+Shift+Tab: round the strip, never off the end. */
export function stepTab(state: TabState, delta: number): TabState {
  const count = state.tabs.length;
  if (count < 2) return state;
  return { ...state, active: (((state.active + delta) % count) + count) % count };
}
