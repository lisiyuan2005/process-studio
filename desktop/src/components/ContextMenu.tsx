import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";

export interface MenuAnchor {
  x: number;
  y: number;
}

export interface MenuItem {
  label: string;
  icon?: ReactNode;
  action: () => void;
  disabled?: boolean;
  danger?: boolean;
  /** A thin rule above this item. */
  separated?: boolean;
  /** The keyboard shortcut, shown at the right. */
  shortcut?: string;
  /** A tick before the label, for a choice that is currently on. */
  checked?: boolean;
  /** A small heading above this item (with the rule when `separated`). */
  heading?: string;
}

/**
 * A menu at a point on screen. It keeps itself inside the window, closes
 * on Escape, on a click elsewhere or on a scroll, and closes before it
 * runs the chosen action.
 */
export function ContextMenu({
  anchor,
  title,
  items,
  onClose,
}: {
  anchor: MenuAnchor;
  title?: ReactNode;
  items: MenuItem[];
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState({ left: anchor.x, top: anchor.y });

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const box = element.getBoundingClientRect();
    setPlace({
      left: Math.max(8, Math.min(anchor.x, window.innerWidth - box.width - 8)),
      top: Math.max(8, Math.min(anchor.y, window.innerHeight - box.height - 8)),
    });
  }, [anchor]);

  useEffect(() => {
    const away = (event: Event) => {
      if (event.target instanceof Node && ref.current?.contains(event.target)) return;
      onClose();
    };
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("pointerdown", away, true);
    window.addEventListener("keydown", key);
    window.addEventListener("resize", onClose);
    window.addEventListener("scroll", onClose, true);
    return () => {
      window.removeEventListener("pointerdown", away, true);
      window.removeEventListener("keydown", key);
      window.removeEventListener("resize", onClose);
      window.removeEventListener("scroll", onClose, true);
    };
  }, [onClose]);

  return createPortal(
    <div
      ref={ref}
      className="context-menu"
      role="menu"
      style={{ left: place.left, top: place.top }}
      onContextMenu={(event) => event.preventDefault()}
    >
      {title && <div className="context-menu-title">{title}</div>}
      {items.map((item, index) => (
        <div key={index} className={item.separated ? "context-menu-group" : undefined}>
          {item.separated && <div className="context-menu-divider" />}
          {item.heading && <div className="context-menu-heading">{item.heading}</div>}
          <button
            type="button"
            role="menuitem"
            className={`${item.danger ? "danger" : ""} ${item.checked ? "checked" : ""}`}
            disabled={item.disabled}
            onClick={() => {
              onClose();
              item.action();
            }}
          >
            {item.checked !== undefined ? (
              <span className="context-menu-check">{item.checked ? "✓" : ""}</span>
            ) : (
              item.icon
            )}
            <span className="context-menu-label">{item.label}</span>
            {item.shortcut && <kbd>{item.shortcut}</kbd>}
          </button>
        </div>
      ))}
    </div>,
    document.body,
  );
}
