/** The workspaces this machine opened last, so a reload lands back in one.
 *
 * The browser keeps this list, not the worker: it is about this window's
 * habits, and a workspace moved or deleted on disk simply fails to open and
 * drops out when the user forgets it.
 */

export interface RecentWorkspace {
  root: string;
  name: string;
  openedAt: number;
}

const RECENT_KEY = "processStudio.recentWorkspaces";
const LAST_KEY = "processStudio.lastWorkspace";
const LIMIT = 6;

function read(): RecentWorkspace[] {
  try {
    const raw = window.localStorage.getItem(RECENT_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (item): item is RecentWorkspace =>
        typeof item === "object" &&
        item !== null &&
        typeof (item as RecentWorkspace).root === "string" &&
        typeof (item as RecentWorkspace).name === "string",
    );
  } catch {
    return [];
  }
}

function write(items: RecentWorkspace[]) {
  try {
    window.localStorage.setItem(RECENT_KEY, JSON.stringify(items));
  } catch {
    // Storage can be missing or full; the list is a convenience, not state.
  }
}

export function recentWorkspaces(): RecentWorkspace[] {
  return read();
}

/** The workspace to reopen on the next start, or null for the home page. */
export function lastWorkspace(): string | null {
  try {
    return window.localStorage.getItem(LAST_KEY);
  } catch {
    return null;
  }
}

export function clearLastWorkspace() {
  try {
    window.localStorage.removeItem(LAST_KEY);
  } catch {
    // ignore
  }
}

export function rememberWorkspace(root: string, name: string): RecentWorkspace[] {
  const items = [
    { root, name, openedAt: Date.now() },
    ...read().filter((item) => item.root !== root),
  ].slice(0, LIMIT);
  write(items);
  try {
    window.localStorage.setItem(LAST_KEY, root);
  } catch {
    // ignore
  }
  return items;
}

export function forgetWorkspace(root: string): RecentWorkspace[] {
  const items = read().filter((item) => item.root !== root);
  write(items);
  if (lastWorkspace() === root) clearLastWorkspace();
  return items;
}

const TABS_KEY = "process-studio.tabs";

export interface OpenTabs {
  /** One workspace directory per tab; "" is a tab showing the home page. */
  roots: string[];
  /** Which of them was on screen. */
  active: number;
}

/** The tabs to reopen on the next start.
 *
 * A build that only ever knew one workspace at a time left the one it had
 * open under its own key; that is where the first tab comes from, so
 * updating does not drop what was open.
 */
export function openTabs(): OpenTabs {
  try {
    const raw = window.localStorage.getItem(TABS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as OpenTabs;
      if (Array.isArray(parsed.roots)) {
        const roots = parsed.roots.filter((root) => typeof root === "string");
        const active = Number.isInteger(parsed.active) ? parsed.active : 0;
        if (roots.length > 0) return { roots, active: Math.min(Math.max(active, 0), roots.length - 1) };
      }
    }
  } catch {
    // a corrupt entry is one the user cannot see or fix: start fresh
  }
  const single = lastWorkspace();
  return { roots: [single ?? ""], active: 0 };
}

export function rememberOpenTabs(tabs: OpenTabs) {
  try {
    window.localStorage.setItem(TABS_KEY, JSON.stringify(tabs));
  } catch {
    // ignore
  }
}
