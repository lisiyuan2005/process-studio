import { useRef } from "react";
import type { PointerEvent } from "react";

interface PanelResizerProps {
  /** Which panel edge this handle moves. */
  side: "left" | "right";
  width: number;
  onResize: (width: number) => void;
  onReset: () => void;
}

/**
 * A grab strip on the inner edge of a side panel. Dragging it resizes the
 * panel and the viewport takes up the difference; a double-click puts the
 * panel back to its default width.
 */
export function PanelResizer({ side, width, onResize, onReset }: PanelResizerProps) {
  const start = useRef<{ x: number; width: number } | null>(null);

  const down = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    start.current = { x: event.clientX, width };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  };
  const move = (event: PointerEvent<HTMLDivElement>) => {
    if (!start.current) return;
    const delta = event.clientX - start.current.x;
    onResize(start.current.width + (side === "left" ? delta : -delta));
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
