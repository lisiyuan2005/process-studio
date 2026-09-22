import {
  ArrowDown,
  ArrowUp,
  Circle,
  MousePointer2,
  Pentagon,
  Spline,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { NumberField } from "./NumberField";
import { crowdedAxes, footprint } from "../domain/sketch";
import type { GridDefinition, MaskKeep, MaskPreview, QuickSketch, SketchShape, TopViewDocument } from "../types";

type Tool = "select" | "rectangle" | "circle" | "polygon" | "path";
type Operation = SketchShape["operation"];
type Point = [number, number];

interface SketchEditorProps {
  sketch: QuickSketch;
  isNew: boolean;
  grid: GridDefinition;
  keep: MaskKeep;
  /** The wafer the step will see, drawn under the sketch when it is known. */
  backdrop?: TopViewDocument;
  busy: boolean;
  onPreview: (sketch: QuickSketch) => Promise<MaskPreview>;
  onSave: (sketch: QuickSketch) => void;
  onClose: () => void;
}

const OPERATION_LABELS: Record<Operation, string> = {
  merge: "Merge",
  subtract: "Subtract",
  intersect: "Intersect",
};

function round(value: number, snap: number) {
  if (!(snap > 0)) return value;
  return Math.round(value / snap) * snap;
}

function number(value: unknown, fallback = 0) {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function pointList(value: unknown): Point[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item) => Array.isArray(item) && item.length === 2)
    .map((item) => [number(item[0]), number(item[1])] as Point);
}

/** The outline of one shape as SVG path data in picture units (y down). */
function outline(shape: SketchShape, toPicture: (point: Point) => Point): string[] {
  const [countX, countY, pitchX, pitchY] = shape.array;
  const paths: string[] = [];
  for (let iy = 0; iy < countY; iy += 1) {
    for (let ix = 0; ix < countX; ix += 1) {
      const dx = (ix - (countX - 1) / 2) * pitchX;
      const dy = (iy - (countY - 1) / 2) * pitchY;
      const parameters = shape.parameters;
      if (shape.kind === "rectangle") {
        const [cx, cy] = pointList([parameters.center ?? [0, 0]])[0] ?? [0, 0];
        const [w, h] = pointList([parameters.size ?? [0, 0]])[0] ?? [0, 0];
        const corners: Point[] = [
          [cx + dx - w / 2, cy + dy - h / 2],
          [cx + dx + w / 2, cy + dy - h / 2],
          [cx + dx + w / 2, cy + dy + h / 2],
          [cx + dx - w / 2, cy + dy + h / 2],
        ];
        paths.push(`M${corners.map((p) => toPicture(p).join(",")).join("L")}Z`);
      } else if (shape.kind === "circle") {
        const [cx, cy] = pointList([parameters.center ?? [0, 0]])[0] ?? [0, 0];
        const r = number(parameters.radius);
        const [px, py] = toPicture([cx + dx, cy + dy]);
        paths.push(`M${px - r},${py}a${r},${r} 0 1,0 ${2 * r},0a${r},${r} 0 1,0 ${-2 * r},0`);
      } else {
        const points = pointList(parameters.points).map(([x, y]) => toPicture([x + dx, y + dy]));
        if (points.length === 0) continue;
        const data = `M${points.map((p) => p.join(",")).join("L")}`;
        paths.push(shape.kind === "polygon" ? `${data}Z` : data);
      }
    }
  }
  return paths;
}

/** Says when an array's pitch puts the copies inside one another. */
function CrowdedArrayNote({ shape }: { shape: SketchShape }) {
  const crowded = crowdedAxes(shape);
  if (!crowded.x && !crowded.y) return null;
  const size = footprint(shape);
  const axes: string[] = [];
  if (crowded.x) axes.push(`x: pitch ${shape.array[2]} µm under ${Number(size.width.toFixed(4))} µm wide`);
  if (crowded.y) axes.push(`y: pitch ${shape.array[3]} µm under ${Number(size.height.toFixed(4))} µm tall`);
  return (
    <p className="shape-warning">
      {axes.join(" · ")} — the copies overlap, so this array is one merged
      shape rather than separate features.
    </p>
  );
}

export function SketchEditor({
  sketch: initial,
  isNew,
  grid,
  keep,
  backdrop,
  busy,
  onPreview,
  onSave,
  onClose,
}: SketchEditorProps) {
  const [sketch, setSketch] = useState<QuickSketch>(initial);
  const [tool, setTool] = useState<Tool>("rectangle");
  const [operation, setOperation] = useState<Operation>("merge");
  const [snapNm, setSnapNm] = useState(5);
  const [pathWidth, setPathWidth] = useState(0.1);
  const [selected, setSelected] = useState<number | null>(null);
  const [preview, setPreview] = useState<MaskPreview>();
  const [previewError, setPreviewError] = useState<string>();
  const [previewing, setPreviewing] = useState(false);
  const [showBackdrop, setShowBackdrop] = useState(true);
  // What the pointer is in the middle of drawing.
  const [anchor, setAnchor] = useState<Point | null>(null);
  const [hover, setHover] = useState<Point | null>(null);
  const [vertices, setVertices] = useState<Point[]>([]);

  const snap = snapNm / 1000;
  const width = grid.xMax - grid.xMin;
  const height = grid.yMax - grid.yMin;
  const toPicture = ([x, y]: Point): Point => [x - grid.xMin, grid.yMax - y];

  // The fill is the worker's reading of the sketch, asked for after each
  // edit settles; drawing the shapes ourselves would only approximate the
  // CSG the run applies.
  useEffect(() => {
    let cancelled = false;
    const handle = window.setTimeout(async () => {
      setPreviewing(true);
      try {
        const next = await onPreview(sketch);
        if (!cancelled) {
          setPreview(next);
          setPreviewError(undefined);
        }
      } catch (reason) {
        if (!cancelled) setPreviewError(reason instanceof Error ? reason.message : String(reason));
      } finally {
        if (!cancelled) setPreviewing(false);
      }
    }, 200);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [sketch, keep, onPreview]);

  // The canvas is the largest box with the window's aspect, so the SVG's
  // user units are micrometres and a pointer position maps through the box.
  const container = useRef<HTMLDivElement>(null);
  const svg = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState<{ width: number; height: number }>();
  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const fit = () => {
      // The wrap's padding is where the axis labels live; the canvas fits
      // inside it, not under it.
      const style = getComputedStyle(element);
      const innerWidth =
        element.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      const innerHeight =
        element.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
      if (innerWidth <= 0 || innerHeight <= 0) return;
      const scale = Math.min(innerWidth / width, innerHeight / height);
      setSize({ width: width * scale, height: height * scale });
    };
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(element);
    return () => observer.disconnect();
  }, [width, height]);

  const pointFrom = (event: React.MouseEvent): Point | null => {
    const box = svg.current?.getBoundingClientRect();
    if (!box || box.width === 0) return null;
    const u = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
    const v = Math.min(1, Math.max(0, (event.clientY - box.top) / box.height));
    return [round(grid.xMin + u * width, snap), round(grid.yMax - v * height, snap)];
  };

  const addShape = (shape: SketchShape) => {
    setSketch((current) => ({ ...current, shapes: [...current.shapes, shape] }));
    setSelected(sketch.shapes.length);
  };
  const updateShape = (index: number, patch: Partial<SketchShape>) =>
    setSketch((current) => ({
      ...current,
      shapes: current.shapes.map((shape, at) => (at === index ? { ...shape, ...patch } : shape)),
    }));
  const updateParameters = (index: number, patch: Record<string, unknown>) =>
    setSketch((current) => ({
      ...current,
      shapes: current.shapes.map((shape, at) =>
        at === index ? { ...shape, parameters: { ...shape.parameters, ...patch } } : shape,
      ),
    }));
  const removeShape = (index: number) => {
    setSketch((current) => ({ ...current, shapes: current.shapes.filter((_, at) => at !== index) }));
    setSelected(null);
  };
  const moveShape = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= sketch.shapes.length) return;
    setSketch((current) => {
      const shapes = [...current.shapes];
      [shapes[index], shapes[target]] = [shapes[target], shapes[index]];
      return { ...current, shapes };
    });
    setSelected(target);
  };

  const finishVertices = () => {
    if (tool === "polygon" && vertices.length >= 3) {
      addShape({ kind: "polygon", operation, parameters: { points: vertices }, array: [1, 1, 0, 0] });
    } else if (tool === "path" && vertices.length >= 2) {
      addShape({ kind: "path", operation, parameters: { points: vertices, width: pathWidth }, array: [1, 1, 0, 0] });
    }
    setVertices([]);
  };

  const onMouseDown = (event: React.MouseEvent) => {
    const point = pointFrom(event);
    if (!point) return;
    if (tool === "rectangle" || tool === "circle") setAnchor(point);
    else if (tool === "polygon" || tool === "path") {
      const last = vertices[vertices.length - 1];
      if (last && last[0] === point[0] && last[1] === point[1]) return;
      setVertices((current) => [...current, point]);
    }
  };
  const onMouseUp = (event: React.MouseEvent) => {
    const point = pointFrom(event);
    if (!point || !anchor) return;
    setAnchor(null);
    if (tool === "rectangle") {
      const w = Math.abs(point[0] - anchor[0]);
      const h = Math.abs(point[1] - anchor[1]);
      if (w > 0 && h > 0) {
        addShape({
          kind: "rectangle",
          operation,
          parameters: { center: [(point[0] + anchor[0]) / 2, (point[1] + anchor[1]) / 2], size: [w, h] },
          array: [1, 1, 0, 0],
        });
      }
    } else if (tool === "circle") {
      const r = round(Math.hypot(point[0] - anchor[0], point[1] - anchor[1]), snap);
      if (r > 0) {
        addShape({ kind: "circle", operation, parameters: { center: anchor, radius: r }, array: [1, 1, 0, 0] });
      }
    }
  };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Enter") finishVertices();
      if (event.key === "Escape") {
        setVertices([]);
        setAnchor(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // What is being drawn right now, as an outline.
  const rubberBand = useMemo(() => {
    if (!hover) return null;
    if (anchor && tool === "rectangle") {
      const [ax, ay] = toPicture(anchor);
      const [hx, hy] = toPicture(hover);
      return `M${Math.min(ax, hx)},${Math.min(ay, hy)}h${Math.abs(hx - ax)}v${Math.abs(hy - ay)}h${-Math.abs(hx - ax)}Z`;
    }
    if (anchor && tool === "circle") {
      const r = Math.hypot(hover[0] - anchor[0], hover[1] - anchor[1]);
      const [cx, cy] = toPicture(anchor);
      return `M${cx - r},${cy}a${r},${r} 0 1,0 ${2 * r},0a${r},${r} 0 1,0 ${-2 * r},0`;
    }
    if (vertices.length > 0) {
      const points = [...vertices, hover].map(toPicture);
      return `M${points.map((p) => p.join(",")).join("L")}`;
    }
    return null;
  }, [anchor, hover, vertices, tool]);

  const instruction =
    tool === "rectangle"
      ? "Drag from one corner to the opposite corner."
      : tool === "circle"
        ? "Drag from the centre outward."
        : tool === "polygon"
          ? "Click each vertex; Enter closes it, Escape discards it."
          : tool === "path"
            ? "Click each point; Enter finishes it, Escape discards it."
            : "Pick a shape in the list to edit its numbers.";

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Quick Sketch">
      <div className="modal-card mask-modal">
        <header className="modal-header">
          <div>
            <span className="eyebrow">MASK</span>
            <h2>{isNew ? "New Quick Sketch" : `Quick Sketch · ${sketch.id}`}</h2>
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <div className="mask-toolbar">
          <div className="segmented-control">
            {(
              [
                ["select", MousePointer2, "Select"],
                ["rectangle", Square, "Rectangle"],
                ["circle", Circle, "Circle"],
                ["polygon", Pentagon, "Polygon"],
                ["path", Spline, "Path"],
              ] as const
            ).map(([id, Icon, label]) => (
              <button
                key={id}
                type="button"
                className={tool === id ? "active" : ""}
                onClick={() => {
                  setTool(id);
                  setVertices([]);
                  setAnchor(null);
                }}
              >
                <Icon size={12} />
                {label}
              </button>
            ))}
          </div>
          <div className="segmented-control operation-control">
            {(["merge", "subtract", "intersect"] as Operation[]).map((id) => (
              <button
                key={id}
                type="button"
                className={operation === id ? `active ${id}` : ""}
                title="How the next shape combines with the ones before it"
                onClick={() => setOperation(id)}
              >
                {OPERATION_LABELS[id]}
              </button>
            ))}
          </div>
          <label className="mask-instruction">
            Snap
            <NumberField
              className="mask-number"
              min={0}
              step={1}
              value={snapNm}
              onChange={setSnapNm}
            />
            nm
          </label>
          {tool === "path" && (
            <label className="mask-instruction">
              Width
              <NumberField
                className="mask-number"
                min={0}
                step={0.01}
                value={pathWidth}
                onChange={setPathWidth}
              />
              µm
            </label>
          )}
          <span className="mask-instruction">{instruction}</span>
          {backdrop && (
            <label className="mask-instruction">
              <input type="checkbox" checked={showBackdrop} onChange={(event) => setShowBackdrop(event.target.checked)} />
              Show wafer
            </label>
          )}
          <span className={`top-view-state ${previewError ? "stale" : ""}`}>
            {previewing
              ? "Previewing…"
              : previewError
                ? "Invalid"
                : preview
                  ? `${(preview.exposedFraction * 100).toFixed(1)}% exposed (keep ${keep})`
                  : "No preview"}
          </span>
        </div>

        <div className="mask-editor-body">
          <div className="mask-canvas-wrap" ref={container}>
            <svg
              ref={svg}
              className={`mask-canvas ${previewing ? "loading" : ""}`}
              style={size}
              viewBox={`0 0 ${width} ${height}`}
              preserveAspectRatio="none"
              onMouseDown={onMouseDown}
              onMouseUp={onMouseUp}
              onMouseMove={(event) => setHover(pointFrom(event))}
              onMouseLeave={() => setHover(null)}
              onDoubleClick={finishVertices}
            >
              {backdrop && showBackdrop && (
                <image
                  href={`data:image/png;base64,${backdrop.image}`}
                  x={0}
                  y={0}
                  width={width}
                  height={height}
                  preserveAspectRatio="none"
                  opacity={0.55}
                />
              )}
              {preview && (
                <image
                  href={`data:image/png;base64,${preview.image}`}
                  x={0}
                  y={0}
                  width={width}
                  height={height}
                  preserveAspectRatio="none"
                />
              )}
              {sketch.shapes.map((shape, index) =>
                outline(shape, toPicture).map((d, instance) => (
                  <path
                    key={`${index}-${instance}`}
                    d={d}
                    className={`shape-outline ${shape.operation} ${selected === index ? "selected" : ""}`}
                    onClick={(event) => {
                      if (tool !== "select") return;
                      event.stopPropagation();
                      setSelected(index);
                    }}
                  />
                )),
              )}
              {rubberBand && <path d={rubberBand} className="shape-outline drawing" />}
              {vertices.map((vertex, index) => {
                const [x, y] = toPicture(vertex);
                return <circle key={index} cx={x} cy={y} r={0.008} className="vertex" />;
              })}
            </svg>
            <span className="axis-label axis-x">x (µm) · {grid.xMin} to {grid.xMax}</span>
            <span className="axis-label axis-y">y (µm) · {grid.yMin} to {grid.yMax}</span>
            {hover && (
              <code className="mask-cursor">
                x {hover[0].toFixed(3)} · y {hover[1].toFixed(3)} µm
              </code>
            )}
            {previewError && <div className="mask-error">{previewError}</div>}
          </div>

          <aside className="shape-panel">
            <div className="shape-panel-heading">
              <span>Shapes, applied in order</span>
              <b>{sketch.shapes.length}</b>
            </div>
            <div className="shape-list">
              <label className="shape-name">
                Sketch name
                <input value={sketch.name} onChange={(event) => setSketch({ ...sketch, name: event.target.value })} />
              </label>
              {sketch.shapes.length === 0 && (
                <p>No shapes yet. Pick a tool and draw on the window; the fill shows what the kernel will expose.</p>
              )}
              {sketch.shapes.map((shape, index) => (
                <div
                  key={index}
                  className={`shape-card ${selected === index ? "selected" : ""}`}
                  onClick={() => setSelected(index)}
                >
                  <header>
                    <span>
                      {index + 1}. {shape.kind}
                    </span>
                    <span>
                      <button type="button" title="Apply earlier" onClick={(e) => { e.stopPropagation(); moveShape(index, -1); }}>
                        <ArrowUp size={12} />
                      </button>
                      <button type="button" title="Apply later" onClick={(e) => { e.stopPropagation(); moveShape(index, 1); }}>
                        <ArrowDown size={12} />
                      </button>
                      <button type="button" title="Delete shape" onClick={(e) => { e.stopPropagation(); removeShape(index); }}>
                        <Trash2 size={12} />
                      </button>
                    </span>
                  </header>
                  <label>
                    Operation
                    <select
                      value={shape.operation}
                      onChange={(event) => updateShape(index, { operation: event.target.value as Operation })}
                    >
                      {(["merge", "subtract", "intersect"] as Operation[]).map((id) => (
                        <option key={id} value={id}>{OPERATION_LABELS[id]}</option>
                      ))}
                    </select>
                  </label>
                  {(shape.kind === "rectangle" || shape.kind === "circle") && (
                    <label>
                      Centre µm
                      <span className="pair">
                        <NumberField
                          step={0.001}
                          value={pointList([shape.parameters.center ?? [0, 0]])[0]?.[0] ?? 0}
                          onChange={(x) =>
                            updateParameters(index, {
                              center: [x, pointList([shape.parameters.center ?? [0, 0]])[0]?.[1] ?? 0],
                            })
                          }
                        />
                        <NumberField
                          step={0.001}
                          value={pointList([shape.parameters.center ?? [0, 0]])[0]?.[1] ?? 0}
                          onChange={(y) =>
                            updateParameters(index, {
                              center: [pointList([shape.parameters.center ?? [0, 0]])[0]?.[0] ?? 0, y],
                            })
                          }
                        />
                      </span>
                    </label>
                  )}
                  {shape.kind === "rectangle" && (
                    <label>
                      Size µm
                      <span className="pair">
                        <NumberField
                          step={0.001}
                          value={pointList([shape.parameters.size ?? [0, 0]])[0]?.[0] ?? 0}
                          onChange={(width) =>
                            updateParameters(index, {
                              size: [width, pointList([shape.parameters.size ?? [0, 0]])[0]?.[1] ?? 0],
                            })
                          }
                        />
                        <NumberField
                          step={0.001}
                          value={pointList([shape.parameters.size ?? [0, 0]])[0]?.[1] ?? 0}
                          onChange={(height) =>
                            updateParameters(index, {
                              size: [pointList([shape.parameters.size ?? [0, 0]])[0]?.[0] ?? 0, height],
                            })
                          }
                        />
                      </span>
                    </label>
                  )}
                  {shape.kind === "circle" && (
                    <label>
                      Radius µm
                      <NumberField
                        step={0.001}
                        value={number(shape.parameters.radius)}
                        onChange={(radius) => updateParameters(index, { radius })}
                      />
                    </label>
                  )}
                  {shape.kind === "path" && (
                    <label>
                      Width µm
                      <NumberField
                        step={0.001}
                        value={number(shape.parameters.width)}
                        onChange={(width) => updateParameters(index, { width })}
                      />
                    </label>
                  )}
                  {(shape.kind === "polygon" || shape.kind === "path") && (
                    <label>
                      Points
                      <span className="pair-note">{pointList(shape.parameters.points).length} vertices</span>
                    </label>
                  )}
                  <label>
                    Array n
                    <span className="pair">
                      <NumberField
                        min={1}
                        step={1}
                        value={shape.array[0]}
                        onChange={(count) =>
                          updateShape(index, { array: [Math.round(count), shape.array[1], shape.array[2], shape.array[3]] })
                        }
                      />
                      <NumberField
                        min={1}
                        step={1}
                        value={shape.array[1]}
                        onChange={(count) =>
                          updateShape(index, { array: [shape.array[0], Math.round(count), shape.array[2], shape.array[3]] })
                        }
                      />
                    </span>
                  </label>
                  <label>
                    Pitch µm
                    <span className="pair">
                      <NumberField
                        step={0.001}
                        value={shape.array[2]}
                        onChange={(pitch) => updateShape(index, { array: [shape.array[0], shape.array[1], pitch, shape.array[3]] })}
                      />
                      <NumberField
                        step={0.001}
                        value={shape.array[3]}
                        onChange={(pitch) => updateShape(index, { array: [shape.array[0], shape.array[1], shape.array[2], pitch] })}
                      />
                    </span>
                  </label>
                  <CrowdedArrayNote shape={shape} />
                </div>
              ))}
            </div>
          </aside>
        </div>

        <div className="modal-actions">
          <span>
            {previewError
              ? "Fix the sketch before saving."
              : "Saving writes the sketch file; steps using it become stale."}
          </span>
          <button type="button" className="secondary-button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="primary-button modal-save"
            disabled={busy || !!previewError || sketch.shapes.length === 0}
            onClick={() => onSave(sketch)}
          >
            Save sketch
          </button>
        </div>
      </div>
    </div>
  );
}
