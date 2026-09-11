import { OrbitControls } from "@react-three/drei";
import { Canvas } from "@react-three/fiber";
import {
  Box,
  CircleAlert,
  Eye,
  EyeOff,
  Image as ImageIcon,
  LoaderCircle,
  PenLine,
  Ruler,
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

/** A section or top-view picture, scaled to fill the view without distortion. */
function FittedImage({ image, width, height, alt }: { image: string; width: number; height: number; alt: string }) {
  const container = useRef<HTMLDivElement>(null);
  const size = useFittedSize(container, width, height);
  return (
    <div className="image-view" ref={container}>
      <div className="image-frame" style={size ?? undefined}>
        <img src={`data:image/png;base64,${image}`} alt={alt} />
      </div>
    </div>
  );
}

/** The top view, with the AA–BB line over it and a way to draw a new one. */
function TopViewImage({
  topView,
  line,
  drawing,
  pendingStart,
  onPick,
}: {
  topView: TopViewDocument;
  line: SectionLine | null;
  drawing: boolean;
  pendingStart: [number, number] | null;
  onPick: (point: [number, number]) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLDivElement>(null);
  const size = useFittedSize(container, topView.width, topView.height);
  const extent: ImageExtent = topView.extent;
  const width = extent.horizontalMax - extent.horizontalMin;
  const height = extent.verticalMax - extent.verticalMin;
  // The picture is drawn top-down with y increasing upward, like the section;
  // positions on it are fractions of the frame, which is the picture exactly.
  const toFraction = ([x, y]: [number, number]): [number, number] => [
    (x - extent.horizontalMin) / width,
    (extent.verticalMax - y) / height,
  ];
  const pick = (event: React.MouseEvent<HTMLElement>) => {
    const box = frame.current?.getBoundingClientRect();
    if (!box || box.width === 0 || box.height === 0) return;
    const u = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
    const v = Math.min(1, Math.max(0, (event.clientY - box.top) / box.height));
    onPick([extent.horizontalMin + u * width, extent.verticalMax - v * height]);
  };
  const ends = line ? [toFraction(line.start), toFraction(line.end)] : null;
  const pending = pendingStart ? toFraction(pendingStart) : null;
  return (
    <div className="image-view" ref={container}>
      <div
        ref={frame}
        className={`image-frame ${drawing ? "drawing" : ""}`}
        style={size ?? undefined}
        onClick={drawing ? pick : undefined}
      >
        <img src={`data:image/png;base64,${topView.image}`} alt="Top view" />
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
      </div>
    </div>
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
        {mode === "top" && topView && (
          <div className="view-tabs draw-tools">
            <button
              type="button"
              className={drawing ? "active" : ""}
              title="Draw the AA–BB section line: click A, then B"
              onClick={() => (drawing ? stopDrawing() : setDrawing(true))}
            >
              <PenLine size={13} />
              {drawing ? (pendingStart ? "Click B" : "Click A") : "Draw AA–BB"}
            </button>
            {sectionLine && !drawing && (
              <button type="button" title="Remove the AA–BB line" onClick={() => onSectionLineChange(null)}>
                <X size={13} />
                Clear line
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
            <FittedImage
              image={section.image}
              width={section.width}
              height={section.height}
              alt={
                section.axis === "line"
                  ? "Section along the AA–BB line"
                  : `Section at ${section.axis} = ${section.position.toFixed(3)} µm`
              }
            />
          ) : (
            <div className="view-placeholder">
              <p>Run the flow to see a cut through the stack.</p>
            </div>
          )
        ) : topView ? (
          <TopViewImage
            topView={topView}
            line={sectionLine}
            drawing={drawing}
            pendingStart={pendingStart}
            onPick={pickPoint}
          />
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
