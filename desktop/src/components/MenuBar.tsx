import { useState } from "react";
import { ContextMenu, type MenuItem } from "./ContextMenu";

export interface Menu {
  label: string;
  items: MenuItem[];
}

/**
 * The application menus: a row of titles, each opening its items below it.
 * Moving the pointer across the titles while one is open switches menus,
 * the way native menu bars do.
 */
export function MenuBar({ menus }: { menus: Menu[] }) {
  const [open, setOpen] = useState<{ index: number; x: number; y: number } | null>(null);
  const place = (element: HTMLElement, index: number) => {
    const box = element.getBoundingClientRect();
    setOpen({ index, x: box.left, y: box.bottom + 2 });
  };
  return (
    <nav className="menubar" aria-label="Application menu">
      {menus.map((menu, index) => (
        <button
          key={menu.label}
          type="button"
          className={open?.index === index ? "open" : ""}
          aria-haspopup="menu"
          aria-expanded={open?.index === index}
          onPointerDown={(event) => {
            event.preventDefault();
            if (open?.index === index) setOpen(null);
            else place(event.currentTarget, index);
          }}
          onPointerEnter={(event) => {
            if (open && open.index !== index) place(event.currentTarget, index);
          }}
        >
          {menu.label}
        </button>
      ))}
      {open && (
        <ContextMenu
          anchor={{ x: open.x, y: open.y }}
          items={menus[open.index].items}
          onClose={() => setOpen(null)}
        />
      )}
    </nav>
  );
}
