import { useRef } from "react";
import type { PointerEvent } from "react";

interface PanelResizerProps {
  /** Which panel edge this handle moves. */
  side: "left" | "right";
  /** The panel's share of the window, 0 to 1. */
  share: number;
  onResize: (share: number) => void;
  onReset: () => void;
}

/**
 * A grab strip on the inner edge of a side panel. Dragging it resizes the
 * panel and the viewport takes up the difference; a double-click puts the
 * panel back to its default share.
 *
 * The drag is in pixels and the panel is a share, so the width to divide by
 * is read when the drag starts. That is also why it is not held in state:
 * the window may have been resized since the last render, and a drag has to
 * begin from what is on screen now.
 */
export function PanelResizer({ side, share, onResize, onReset }: PanelResizerProps) {
  const start = useRef<{ x: number; share: number; total: number } | null>(null);

  const down = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    const total = event.currentTarget.parentElement?.getBoundingClientRect().width ?? window.innerWidth;
    start.current = { x: event.clientX, share, total: total || 1 };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  };
  const move = (event: PointerEvent<HTMLDivElement>) => {
    if (!start.current) return;
    const delta = (event.clientX - start.current.x) / start.current.total;
    onResize(start.current.share + (side === "left" ? delta : -delta));
  };
  const up = (event: PointerEvent<HTMLDivElement>) => {
    if (!start.current) return;
    start.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
  };

  return (
    <div
      className={`panel-resizer panel-resizer-${side}`}
      role="separator"
      aria-orientation="vertical"
      aria-label={side === "left" ? "Resize the steps panel" : "Resize the inspector"}
      title="Drag to resize; double-click to reset"
      onPointerDown={down}
      onPointerMove={move}
      onPointerUp={up}
      onPointerCancel={up}
      onDoubleClick={onReset}
    />
  );
}
