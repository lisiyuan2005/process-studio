import { OrbitControls } from "@react-three/drei";
import { Canvas } from "@react-three/fiber";
import {
  Box,
  CircleAlert,
  Eye,
  EyeOff,
  Image as ImageIcon,
  LoaderCircle,
  Ruler,
  Scissors,
  TriangleAlert,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import type {
  ImageExtent,
  MaterialDefinition,
  SectionAxis,
  SectionDocument,
  SectionLine,
  SurfaceDocument,
  SurfacePayload,
  TopViewDocument,
} from "../types";

export type ViewMode = "surfaces" | "section" | "top";

interface ViewportProps {
  mode: ViewMode;
  onModeChange: (mode: ViewMode) => void;
  title: string;
  loading: boolean;
  error?: string;
  /** Shown over the view without hiding it, for a result that is out of date. */
  notice?: string;
  surfacesSupported: boolean;
  maximumInterpolation: number;
  interpolation: number;
  onInterpolationChange: (value: number) => void;
  sectionAxis: SectionAxis;
  onSectionAxisChange: (axis: SectionAxis) => void;
  sectionIndex: number;
  onSectionIndexChange: (index: number) => void;
  sectionLine: SectionLine | null;
  onSectionLineChange: (line: SectionLine | null) => void;
  materials: MaterialDefinition[];
  hiddenMaterials: string[];
  onToggleMaterial: (material: string) => void;
  surfaces?: SurfaceDocument;
  section?: SectionDocument;
  topView?: TopViewDocument;
}

function decodeFloats(value: string): Float32Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return new Float32Array(bytes.buffer);
}

function decodeIndices(value: string): Uint32Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return new Uint32Array(bytes.buffer);
}

function SurfaceMesh({
  surface,
  opacity,
  offset,
}: {
  surface: SurfacePayload;
  opacity: number;
  offset: THREE.Vector3;
}) {
  const geometry = useMemo(() => {
    const buffer = new THREE.BufferGeometry();
    buffer.setAttribute("position", new THREE.BufferAttribute(decodeFloats(surface.positions), 3));
    buffer.setAttribute("normal", new THREE.BufferAttribute(decodeFloats(surface.normals), 3));
    buffer.setIndex(new THREE.BufferAttribute(decodeIndices(surface.indices), 1));
    return buffer;
  }, [surface.positions, surface.normals, surface.indices]);

  // Marching-cubes buffers are large; release them when the step changes.
  useEffect(() => () => geometry.dispose(), [geometry]);

  return (
    <mesh geometry={geometry} position={[-offset.x, -offset.y, -offset.z]}>
      <meshStandardMaterial
        color={surface.color}
        transparent={opacity < 1}
        opacity={opacity}
        roughness={0.62}
        metalness={0.08}
        side={THREE.DoubleSide}
      />
    </mesh>
  );
}

function SurfaceScene({
  surfaces,
  materials,
  hiddenMaterials,
}: {
  surfaces: SurfaceDocument;
  materials: MaterialDefinition[];
  hiddenMaterials: string[];
}) {
  const { bounds } = surfaces;
  const center = new THREE.Vector3(
    (bounds.xMin + bounds.xMax) / 2,
    (bounds.yMin + bounds.yMax) / 2,
    (bounds.zMin + bounds.zMax) / 2,
  );
  const span = Math.max(
    bounds.xMax - bounds.xMin,
    bounds.yMax - bounds.yMin,
    bounds.zMax - bounds.zMin,
  );
  const distance = span * 2.1;

  return (
    <Canvas
      camera={{ position: [distance, -distance, distance * 0.85], fov: 38, up: [0, 0, 1] }}
      dpr={[1, 2]}
    >
      <color attach="background" args={["#f4f7f9"]} />
      <ambientLight intensity={0.72} />
      <directionalLight position={[span, -span, span * 1.6]} intensity={1.25} />
      <directionalLight position={[-span, span * 0.6, span]} intensity={0.45} />
      <group>
        {surfaces.surfaces
          .filter((surface) => !hiddenMaterials.includes(surface.material))
          .map((surface) => (
            <SurfaceMesh
              key={surface.material}
              surface={surface}
              opacity={
                materials.find((material) => material.name === surface.material)?.opacity ?? 1
              }
              offset={center}
            />
          ))}
        <gridHelper
          args={[span * 1.4, 14, "#c7d2db", "#dde5eb"]}
          rotation={[Math.PI / 2, 0, 0]}
          position={[0, 0, bounds.zMin - center.z]}
        />
      </group>
      <OrbitControls enablePan enableZoom makeDefault />
    </Canvas>
  );
}

/**
 * The largest box inside its parent with the picture's own aspect ratio.
 *
 * A native-grid picture can be 57 pixels wide; left at its own size it is a
 * postage stamp, and CSS object-fit would letterbox it inside a box whose
 * edges no longer coincide with the picture, which breaks the overlay's
 * click mapping. Measuring the parent and sizing the frame to fit keeps the
 * frame and the picture the same rectangle.
 */
function useFittedSize(
  container: React.RefObject<HTMLDivElement | null>,
  pixelWidth: number,
  pixelHeight: number,
) {
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const fit = () => {
      const available = element.getBoundingClientRect();
      if (available.width === 0 || available.height === 0 || pixelWidth === 0) return;
      const scale = Math.min(available.width / pixelWidth, available.height / pixelHeight);
      setSize({ width: pixelWidth * scale, height: pixelHeight * scale });
    };
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(element);
    return () => observer.disconnect();
  }, [container, pixelWidth, pixelHeight]);
  return size;
}

/** Two points on a picture, in the picture's own units (µm). */
export interface Measurement {
  start: [number, number];
  end: [number, number];
}

function formatLength(um: number) {
  return Math.abs(um) < 1 ? `${(um * 1000).toFixed(1)} nm` : `${um.toFixed(3)} µm`;
}

/** The longest round length that fits in a quarter of the picture's width. */
function scaleBarLength(extentWidth: number) {
  const candidates = [0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10];
  let chosen = candidates[0];
  for (const candidate of candidates) if (candidate <= extentWidth / 4) chosen = candidate;
  return chosen;
}

/**
 * A section or top-view picture, scaled to fill the view without distortion,
 * with a scale bar, a live cursor readout and a two-click measurement.
 *
 * Positions are fractions of the frame, and the frame is the picture
 * exactly, so a click maps to µm through the extent alone.
 */
function PictureView({
  image,
  width,
  height,
  extent,
  axes,
  alt,
  measuring,
  measurement,
  pendingStart,
  onPoint,
  onHover,
  onClickCapture,
  children,
}: {
  image: string;
  width: number;
  height: number;
  extent: ImageExtent;
  /** Names of the horizontal and vertical axes, for the readout. */
  axes: [string, string];
  alt: string;
  measuring: boolean;
  measurement: Measurement | null;
  pendingStart: [number, number] | null;
  onPoint: (point: [number, number]) => void;
  onHover: (point: [number, number] | null) => void;
  /** Takes the click instead of the measurement when it returns true. */
  onClickCapture?: (point: [number, number]) => boolean;
  children?: React.ReactNode;
}) {
  const container = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLDivElement>(null);
  const size = useFittedSize(container, width, height);
  const spanH = extent.horizontalMax - extent.horizontalMin;
  const spanV = extent.verticalMax - extent.verticalMin;
  const toFraction = ([h, v]: [number, number]): [number, number] => [
    (h - extent.horizontalMin) / spanH,
    (extent.verticalMax - v) / spanV,
  ];
  const fromEvent = (event: React.MouseEvent<HTMLElement>): [number, number] | null => {
    const box = frame.current?.getBoundingClientRect();
    if (!box || box.width === 0 || box.height === 0) return null;
    const u = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
    const w = Math.min(1, Math.max(0, (event.clientY - box.top) / box.height));
    return [extent.horizontalMin + u * spanH, extent.verticalMax - w * spanV];
  };
  const click = (event: React.MouseEvent<HTMLElement>) => {
    const point = fromEvent(event);
    if (!point) return;
    if (onClickCapture?.(point)) return;
    if (measuring) onPoint(point);
  };
  const bar = scaleBarLength(spanH);
  const ends = measurement ? [toFraction(measurement.start), toFraction(measurement.end)] : null;
  const pending = pendingStart ? toFraction(pendingStart) : null;
  const delta = measurement
    ? [measurement.end[0] - measurement.start[0], measurement.end[1] - measurement.start[1]]
    : null;
  return (
    <div className="image-view" ref={container}>
      <div
        ref={frame}
        className={`image-frame ${measuring || onClickCapture ? "drawing" : ""}`}
        style={size ?? undefined}
        onClick={click}
        onMouseMove={(event) => onHover(fromEvent(event))}
        onMouseLeave={() => onHover(null)}
      >
        <img src={`data:image/png;base64,${image}`} alt={alt} />
        {children}
        {ends && delta && (
          <>
            <svg className="image-overlay" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">
              <line className="measure" x1={ends[0][0]} y1={ends[0][1]} x2={ends[1][0]} y2={ends[1][1]} />
            </svg>
            <i className="line-dot measure" style={percent(ends[0])} />
            <i className="line-dot measure" style={percent(ends[1])} />
            <span
              className="measure-label"
              style={percent([(ends[0][0] + ends[1][0]) / 2, (ends[0][1] + ends[1][1]) / 2])}
            >
              {formatLength(Math.hypot(delta[0], delta[1]))}
              <small>
                Δ{axes[0]} {formatLength(delta[0])} · Δ{axes[1]} {formatLength(delta[1])}
              </small>
            </span>
          </>
        )}
        {pending && <i className="line-dot measure pending" style={percent(pending)} />}
        <span className="scale-bar" style={{ width: `${(bar / spanH) * 100}%` }}>
          {formatLength(bar)}
        </span>
      </div>
    </div>
  );
}

/** The AA–BB line drawn over the top view, as fractions of the frame. */
function SectionLineOverlay({
  extent,
  line,
  pendingStart,
}: {
  extent: ImageExtent;
  line: SectionLine | null;
  pendingStart: [number, number] | null;
}) {
  const width = extent.horizontalMax - extent.horizontalMin;
  const height = extent.verticalMax - extent.verticalMin;
  const toFraction = ([x, y]: [number, number]): [number, number] => [
    (x - extent.horizontalMin) / width,
    (extent.verticalMax - y) / height,
  ];
  const ends = line ? [toFraction(line.start), toFraction(line.end)] : null;
  const pending = pendingStart ? toFraction(pendingStart) : null;
  return (
    <>
      {ends && (
        <svg className="image-overlay" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">
          <line x1={ends[0][0]} y1={ends[0][1]} x2={ends[1][0]} y2={ends[1][1]} />
        </svg>
      )}
      {ends && (
        <>
          <i className="line-dot" style={percent(ends[0])} />
          <i className="line-dot" style={percent(ends[1])} />
          <span className="line-label" style={percent(ends[0])}>A</span>
          <span className="line-label" style={percent(ends[1])}>B</span>
        </>
      )}
      {pending && <i className="line-dot pending" style={percent(pending)} />}
    </>
  );
}

function percent([u, v]: [number, number]) {
  return { left: `${u * 100}%`, top: `${v * 100}%` };
}

export function Viewport({
  mode,
  onModeChange,
  title,
  loading,
  error,
  notice,
  surfacesSupported,
  maximumInterpolation,
  interpolation,
  onInterpolationChange,
  sectionAxis,
  onSectionAxisChange,
  sectionIndex,
  onSectionIndexChange,
  sectionLine,
  onSectionLineChange,
  materials,
  hiddenMaterials,
  onToggleMaterial,
  surfaces,
  section,
  topView,
}: ViewportProps) {
  // Drawing the AA–BB line: two clicks on the top view, A then B.
  const [drawing, setDrawing] = useState(false);
  const [pendingStart, setPendingStart] = useState<[number, number] | null>(null);
  const pickPoint = (point: [number, number]) => {
    if (!pendingStart) {
      setPendingStart(point);
      return;
    }
    if (pendingStart[0] === point[0] && pendingStart[1] === point[1]) return;
    onSectionLineChange({ start: pendingStart, end: point });
    setPendingStart(null);
    setDrawing(false);
  };
  const stopDrawing = () => {
    setDrawing(false);
    setPendingStart(null);
  };

  // The ruler: two clicks measure a distance on the picture, and the cursor
  // position is read out in the footer. Both reset when the view changes.
  const [measuring, setMeasuring] = useState(false);
  const [measurement, setMeasurement] = useState<Measurement | null>(null);
  const [measureStart, setMeasureStart] = useState<[number, number] | null>(null);
  const [cursor, setCursor] = useState<[number, number] | null>(null);
  useEffect(() => {
    setMeasuring(false);
    setMeasurement(null);
    setMeasureStart(null);
    setCursor(null);
  }, [mode]);
  const measurePoint = (point: [number, number]) => {
    if (!measureStart) {
      setMeasureStart(point);
      return;
    }
    setMeasurement({ start: measureStart, end: point });
    setMeasureStart(null);
    setMeasuring(false);
  };
  const axesFor = (horizontal: string): [string, string] =>
    mode === "top" ? ["x", "y"] : [horizontal, "z"];
  const triangles = surfaces?.surfaces.reduce((total, item) => total + item.triangleCount, 0) ?? 0;
  const shownMaterials = (
    mode === "surfaces" ? surfaces?.surfaces.map((surface) => surface.material) : undefined
  ) ?? materials.map((material) => material.name);
  const sampled = mode === "top" ? undefined : surfaces?.sampledSpacingUm ?? section?.sampledSpacingUm;

  return (
    <section className="viewport-panel">
      <div className="viewport-toolbar">
        <div className="viewport-title">
          <Box size={14} />
          <span>{title}</span>
        </div>
        <div className="view-tabs">
          <button
            type="button"
            className={mode === "surfaces" ? "active" : ""}
            disabled={!surfacesSupported}
            title={
              surfacesSupported
                ? "Marching-cubes surfaces"
                : "Install the render extra for surface extraction"
            }
            onClick={() => onModeChange("surfaces")}
          >
            <Box size={13} />
            3D
          </button>
          <button
            type="button"
            className={mode === "section" ? "active" : ""}
            onClick={() => onModeChange("section")}
          >
            <Ruler size={13} />
            Section
          </button>
          <button
            type="button"
            className={mode === "top" ? "active" : ""}
            onClick={() => onModeChange("top")}
          >
            <ImageIcon size={13} />
            Top view
          </button>
        </div>
        {((mode === "top" && topView) || (mode === "section" && section)) && (
          <div className="view-tabs draw-tools">
            {mode === "top" && (
              <button
                type="button"
                className={drawing ? "active" : ""}
                title="Draw the AA–BB section line: click A, then B"
                onClick={() => {
                  if (drawing) stopDrawing();
                  else {
                    setMeasuring(false);
                    setMeasureStart(null);
                    setDrawing(true);
                  }
                }}
              >
                <Scissors size={13} />
                {drawing ? (pendingStart ? "Click B" : "Click A") : "Draw AA–BB"}
              </button>
            )}
            {mode === "top" && sectionLine && !drawing && (
              <button type="button" title="Remove the AA–BB line" onClick={() => onSectionLineChange(null)}>
                <X size={13} />
                Clear line
              </button>
            )}
            <button
              type="button"
              className={measuring ? "active" : ""}
              title="Measure a distance on the picture: click two points"
              onClick={() => {
                if (measuring) {
                  setMeasuring(false);
                  setMeasureStart(null);
                } else {
                  stopDrawing();
                  setMeasuring(true);
                }
              }}
            >
              <Ruler size={13} />
              {measuring ? (measureStart ? "Click the end" : "Click the start") : "Measure"}
            </button>
            {measurement && !measuring && (
              <button type="button" title="Remove the measurement" onClick={() => setMeasurement(null)}>
                <X size={13} />
                Clear measure
              </button>
            )}
          </div>
        )}
        <div className="viewport-badges">
          {loading && (
            <span className="viewport-badge">
              <LoaderCircle className="spin" size={11} />
              Loading
            </span>
          )}
          {mode === "surfaces" && triangles > 0 && (
            <span className="viewport-badge">{triangles.toLocaleString()} triangles</span>
          )}
          {sampled !== undefined && (
            <span className="viewport-badge">{(sampled * 1000).toFixed(2)} nm samples</span>
          )}
        </div>
      </div>

      <div className="canvas-wrap">
        {error ? (
          <div className="view-placeholder">
            <CircleAlert size={20} />
            <p>{error}</p>
          </div>
        ) : mode === "surfaces" ? (
          surfaces && surfaces.surfaces.length > 0 ? (
            <SurfaceScene
              surfaces={surfaces}
              materials={materials}
              hiddenMaterials={hiddenMaterials}
            />
          ) : (
            <div className="view-placeholder">
              <p>
                {surfacesSupported
                  ? "No surface crosses this state yet. Run the flow, then pick a step."
                  : 'Surface extraction needs the render extra: pip install -e ".[render]"'}
              </p>
            </div>
          )
        ) : mode === "section" ? (
          section ? (
            <PictureView
              image={section.image}
              width={section.width}
              height={section.height}
              extent={section.extent}
              axes={axesFor(section.horizontalAxis)}
              alt={
                section.axis === "line"
                  ? "Section along the AA–BB line"
                  : `Section at ${section.axis} = ${section.position.toFixed(3)} µm`
              }
              measuring={measuring}
              measurement={measurement}
              pendingStart={measureStart}
              onPoint={measurePoint}
              onHover={setCursor}
            />
          ) : (
            <div className="view-placeholder">
              <p>Run the flow to see a cut through the stack.</p>
            </div>
          )
        ) : topView ? (
          <PictureView
            image={topView.image}
            width={topView.width}
            height={topView.height}
            extent={topView.extent}
            axes={["x", "y"]}
            alt="Top view"
            measuring={measuring}
            measurement={measurement}
            pendingStart={measureStart}
            onPoint={measurePoint}
            onHover={setCursor}
            onClickCapture={
              drawing
                ? (point) => {
                    pickPoint(point);
                    return true;
                  }
                : undefined
            }
          >
            <SectionLineOverlay extent={topView.extent} line={sectionLine} pendingStart={pendingStart} />
          </PictureView>
        ) : (
          <div className="view-placeholder">
            <p>Run the flow to see the top view.</p>
          </div>
        )}

        {notice && !error && (
          <span className="viewport-notice">
            <TriangleAlert size={11} />
            {notice}
          </span>
        )}

        {mode !== "top" && (
          <span className="preview-disclaimer">
            Sampling density is a display setting. It does not change what the kernel computed.
          </span>
        )}
      </div>

      <div className="viewport-footer">
        {mode === "section" && (
          <>
            <label>
              Cut
              <select
                value={sectionAxis}
                onChange={(event) => onSectionAxisChange(event.target.value as SectionAxis)}
              >
                <option value="y">Along y</option>
                <option value="x">Along x</option>
                <option value="line" disabled={!sectionLine}>
                  AA–BB line{sectionLine ? "" : " (draw it on the top view)"}
                </option>
              </select>
            </label>
            {sectionAxis === "line" ? (
              sectionLine && (
                <code className="line-ends">
                  A ({sectionLine.start[0].toFixed(3)}, {sectionLine.start[1].toFixed(3)}) → B (
                  {sectionLine.end[0].toFixed(3)}, {sectionLine.end[1].toFixed(3)}) µm
                </code>
              )
            ) : (
              section && (
                <label>
                  Position
                  <input
                    type="range"
                    min={0}
                    max={Math.max(0, section.positions.length - 1)}
                    value={sectionIndex}
                    onChange={(event) => onSectionIndexChange(Number(event.target.value))}
                  />
                  <code>{section.position.toFixed(3)} µm</code>
                </label>
              )
            )}
          </>
        )}
        {mode !== "top" && (
          <label>
            Sampling
            <select
              value={interpolation}
              onChange={(event) => onInterpolationChange(Number(event.target.value))}
            >
              {Array.from({ length: maximumInterpolation }, (_, index) => index + 1).map((factor) => (
                <option key={factor} value={factor}>
                  {factor}× {factor === 1 ? "(native grid)" : "display"}
                </option>
              ))}
            </select>
          </label>
        )}
        {mode !== "surfaces" && cursor && (
          <code className="cursor-readout">
            {axesFor(section?.horizontalAxis ?? "x")[0]} {cursor[0].toFixed(3)} ·{" "}
            {axesFor(section?.horizontalAxis ?? "x")[1]} {cursor[1].toFixed(3)} µm
          </code>
        )}
        <div className="footer-spacer" />
        <div className="legend">
          {materials
            .filter((material) => shownMaterials.includes(material.name))
            .map((material) =>
              mode === "surfaces" ? (
                <button
                  key={material.id}
                  type="button"
                  className={hiddenMaterials.includes(material.name) ? "hidden-material" : ""}
                  title={
                    hiddenMaterials.includes(material.name)
                      ? `Show ${material.name} in the 3D view`
                      : `Hide ${material.name} in the 3D view`
                  }
                  onClick={() => onToggleMaterial(material.name)}
                >
                  <i style={{ background: material.color }} />
                  {material.name}
                  {hiddenMaterials.includes(material.name) ? (
                    <EyeOff size={11} />
                  ) : (
                    <Eye size={11} />
                  )}
                </button>
              ) : (
                <span key={material.id}>
                  <i style={{ background: material.color }} />
                  {material.name}
                </span>
              ),
            )}
        </div>
      </div>
    </section>
  );
}
