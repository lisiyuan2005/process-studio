/**
 * The window, and the tabs in it.
 *
 * Every tab is a whole workspace -- its own document, selection, views and
 * undo history -- so switching tabs is not a reload: a tab that is not on
 * screen stays mounted and keeps everything it had. What it does not do is
 * ask the worker for anything (see ``Workspace``'s ``active``), because the
 * views are the expensive half of the worker's work and every tab shares
 * one worker.
 *
 * The rules about which workspace is in which tab are in ``domain/tabs``.
 * This file is the strip, the keys and the per-tab callbacks.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Plus, X } from "lucide-react";
import { Workspace, type WorkspaceProps } from "./Workspace";
import { openTabs, rememberOpenTabs } from "./domain/recent";
import {
  claimRoot,
  closeTab,
  openTab,
  reportTab,
  stepTab,
  tabsFrom,
  type TabState,
} from "./domain/tabs";

let nextKey = 1;
const freshKey = () => nextKey++;

export default function App() {
  const [state, setState] = useState<TabState>(() => {
    const stored = openTabs();
    const start = tabsFrom(stored.roots, stored.active, nextKey);
    nextKey += start.tabs.length;
    return start;
  });
  const { tabs, active } = state;

  // The state as it is, for callbacks that must not close over a stale one.
  const shown = useRef(state);
  shown.current = state;

  useEffect(() => {
    rememberOpenTabs({ roots: tabs.map((tab) => tab.handle.root), active });
  }, [tabs, active]);

  const open = useCallback((root: string) => {
    setState((current) => openTab(current, root, freshKey()));
  }, []);

  const close = useCallback((key: number) => {
    setState((current) => closeTab(current, key, freshKey()));
  }, []);

  // One stable callback set per tab: a Workspace reports through them from
  // an effect, and a fresh function on every render would make it loop.
  const perTab = useRef(
    new Map<number, Pick<WorkspaceProps, "onChanged" | "onClose" | "claimRoot">>(),
  );
  const callbacksFor = (key: number) => {
    let found = perTab.current.get(key);
    if (!found) {
      found = {
        onChanged: (handle) => setState((current) => reportTab(current, key, handle)),
        onClose: () => close(key),
        claimRoot: (root: string) => {
          const asked = claimRoot(shown.current, key, root);
          if (!asked.allowed) setState(asked.state);
          return asked.allowed;
        },
      };
      perTab.current.set(key, found);
    }
    return found;
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
      const key = event.key.toLowerCase();
      if (key === "t") {
        event.preventDefault();
        open("");
      } else if (key === "w") {
        event.preventDefault();
        const here = shown.current;
        close(here.tabs[here.active].key);
      } else if (key === "tab") {
        event.preventDefault();
        setState((current) => stepTab(current, event.shiftKey ? -1 : 1));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close]);

  return (
    <div className="window">
      {tabs.length > 1 && (
        <div className="tab-strip" role="tablist">
          {tabs.map((tab, index) => (
            <div
              key={tab.key}
              role="tab"
              aria-selected={index === active}
              className={`tab${index === active ? " current" : ""}`}
              title={tab.handle.root || "New project"}
              onMouseDown={(event) => {
                if (event.button === 1) {
                  event.preventDefault();
                  close(tab.key);
                } else if (event.button === 0) {
                  setState((current) => ({ ...current, active: index }));
                }
              }}
            >
              <span className="tab-name">{tab.handle.name}</span>
              {tab.handle.busy ? (
                <span className="tab-dot busy" title="Running" />
              ) : (
                tab.handle.unsaved && <span className="tab-dot unsaved" title="Unsaved changes" />
              )}
              <button
                type="button"
                className="tab-close"
                title="Close this tab (Ctrl+W)"
                onClick={(event) => {
                  event.stopPropagation();
                  close(tab.key);
                }}
              >
                <X size={12} />
              </button>
            </div>
          ))}
          <button
            type="button"
            className="tab-new"
            title="New tab (Ctrl+T)"
            onClick={() => open("")}
          >
            <Plus size={14} />
          </button>
        </div>
      )}
      {tabs.map((tab, index) => (
        <div key={tab.key} className="tab-page" hidden={index !== active}>
          <Workspace
            initialRoot={tab.initialRoot}
            active={index === active}
            onOpenInNewTab={open}
            {...callbacksFor(tab.key)}
          />
        </div>
      ))}
    </div>
  );
}
