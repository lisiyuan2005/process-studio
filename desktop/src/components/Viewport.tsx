import { OrbitControls } from "@react-three/drei";
import { Canvas, useThree } from "@react-three/fiber";
import {
  Box,
  Camera,
  CircleAlert,
  Eye,
  EyeOff,
  FileBox,
  Image as ImageIcon,
  LoaderCircle,
  PenLine,
  Plus,
  Ruler,
  Scissors,
  TriangleAlert,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ContextMenu, type MenuAnchor } from "./ContextMenu";
import { SectionLineEditor } from "./SectionLineEditor";
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
  /** The project's saved AA–BB lines, and the one the section follows. */
  sectionLines: SectionLine[];
  sectionLine: SectionLine | null;
  windowBounds: { xMin: number; xMax: number; yMin: number; yMax: number };
  onSelectLine: (lineId: string | null) => void;
  /** An empty id means a new line; the app names and numbers it. */
  onSaveLine: (line: SectionLine) => void;
  onRemoveLine: (lineId: string) => void;
  smoothSection: boolean;
  onSmoothSectionChange: (smooth: boolean) => void;
  materials: MaterialDefinition[];
  hiddenMaterials: string[];
  onToggleMaterial: (material: string) => void;
  /** Write the 3D surfaces of the shown step to a file the user picks. */
  onExportMesh: () => void;
  /** Write a PNG the user picks a place for; `image` is base64 without prefix. */
  onSaveImage: (kind: "3d" | "section" | "top", image: string) => void;
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

function decodeBytes(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function SurfaceMesh({
  surface,
  opacity,
  offset,
  order,
  showInterfaces,
}: {
  surface: SurfacePayload;
  opacity: number;
  offset: THREE.Vector3;
  /** The material's place in the draw order, for a deterministic depth bias. */
  order: number;
  /** Draw the faces that lie against another material, because one is hidden. */
  showInterfaces: boolean;
}) {
  const geometry = useMemo(() => {
    const buffer = new THREE.BufferGeometry();
    buffer.setAttribute("position", new THREE.BufferAttribute(decodeFloats(surface.positions), 3));
    let indices = decodeIndices(surface.indices);
    if (!showInterfaces && surface.interfaceFaces) {
      // A face between two materials is in both meshes at exactly the same
      // place. Drawn twice, the two copies fight for the pixels and shimmer
      // as the camera moves. While every material is shown such a face is
      // interior anyway, so it is drawn by neither; once a material is
      // hidden the faces come back to show the cavity it leaves.
      const flags = decodeBytes(surface.interfaceFaces);
      const kept = new Uint32Array(indices.length);
      let count = 0;
      for (let face = 0; face < flags.length; face += 1) {
        if (flags[face]) continue;
        kept[count] = indices[face * 3];
        kept[count + 1] = indices[face * 3 + 1];
        kept[count + 2] = indices[face * 3 + 2];
        count += 3;
      }
      indices = kept.subarray(0, count);
    }
    buffer.setIndex(new THREE.BufferAttribute(indices, 1));
    if (surface.normals) {
      buffer.setAttribute("normal", new THREE.BufferAttribute(decodeFloats(surface.normals), 3));
      return buffer;
    }
    // Exact slab geometry arrives welded and indexed, without normals: each
    // triangle gets its own corners here so a flat face shades flat and the
    // sharp edges between faces stay sharp.
    const flat = buffer.toNonIndexed();
    flat.computeVertexNormals();
    buffer.dispose();
    return flat;
  }, [surface.positions, surface.normals, surface.indices, surface.interfaceFaces, showInterfaces]);

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
        // Faces of different materials that still coincide (a level-set
        // interface, or an interface shown because a neighbour is hidden)
        // are settled by a small per-material depth bias instead of by
        // whichever triangle happens to rasterise closer this frame.
        polygonOffset
        polygonOffsetFactor={order}
        polygonOffsetUnits={order}
      />
    </mesh>
  );
}

/** Hands the parent a function that renders one frame and returns it as PNG. */
function Snapshot({ register }: { register: (capture: (() => string) | null) => void }) {
  const { gl, scene, camera } = useThree();
  useEffect(() => {
    register(() => {
      // The buffer is not preserved between frames, so draw one now and
      // read it back before anything else touches it.
      gl.render(scene, camera);
      return gl.domElement.toDataURL("image/png").split(",", 2)[1];
    });
    return () => register(null);
  }, [gl, scene, camera, register]);
  return null;
}

function SurfaceScene({
  surfaces,
  materials,
  hiddenMaterials,
  registerSnapshot,
}: {
  surfaces: SurfaceDocument;
  materials: MaterialDefinition[];
  hiddenMaterials: string[];
  registerSnapshot: (capture: (() => string) | null) => void;
}) {
  const { bounds } = surfaces;
  const span = Math.max(
    bounds.xMax - bounds.xMin,
    bounds.yMax - bounds.yMin,
    bounds.zMax - bounds.zMin,
  );
  const distance = span * 2.1;
  // Stable objects: a new camera description on every render would reset
  // the view the user has turned to. The clip planes hug the model so the
  // depth buffer spends its precision where the geometry is.
  const center = useMemo(
    () =>
      new THREE.Vector3(
        (bounds.xMin + bounds.xMax) / 2,
        (bounds.yMin + bounds.yMax) / 2,
        (bounds.zMin + bounds.zMax) / 2,
      ),
    [bounds.xMin, bounds.xMax, bounds.yMin, bounds.yMax, bounds.zMin, bounds.zMax],
  );
  const camera = useMemo(
    () => ({
      position: [distance, -distance, distance * 0.85] as [number, number, number],
      fov: 38,
      up: [0, 0, 1] as [number, number, number],
      near: span * 0.02,
      far: span * 40,
    }),
    [distance, span],
  );
  const shown = surfaces.surfaces.filter((surface) => !hiddenMaterials.includes(surface.material));

  return (
    // Frames are drawn only when something changed: a still scene stays
    // still, and a driver that re-presents each frame differently has
    // nothing to flicker with.
    <Canvas camera={camera} dpr={[1, 2]} frameloop="demand">
      <color attach="background" args={["#f4f7f9"]} />
      <ambientLight intensity={0.72} />
      <directionalLight position={[span, -span, span * 1.6]} intensity={1.25} />
      <directionalLight position={[-span, span * 0.6, span]} intensity={0.45} />
      <group>
        {shown.map((surface, index) => (
          <SurfaceMesh
            key={surface.material}
            surface={surface}
            opacity={materials.find((material) => material.name === surface.material)?.opacity ?? 1}
            offset={center}
            order={index}
            showInterfaces={hiddenMaterials.length > 0}
          />
        ))}
        <gridHelper
          args={[span * 1.4, 14, "#c7d2db", "#dde5eb"]}
          rotation={[Math.PI / 2, 0, 0]}
          position={[0, 0, bounds.zMin - center.z]}
        />
      </group>
      <OrbitControls enablePan enableZoom makeDefault />
      <Snapshot register={registerSnapshot} />
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
  /** Takes the click instead of the measurement when it returns true. */
  onClickCapture?: (point: [number, number]) => boolean;
  children?: React.ReactNode;
}) {
  // The cursor readout floats over the picture rather than sitting in the
  // footer: text that appears only while the pointer is over the picture
  // would reflow the footer on every entry and exit, and the picture would
  // refit and jump each time.
  const container = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLDivElement>(null);
  const size = useFittedSize(container, width, height);
  const [hover, setHover] = useState<[number, number] | null>(null);
  const spanH = extent.horizontalMax - extent.horizontalMin;
  const spanV = extent.verticalMax - extent.verticalMin;

  // Zoom and pan are a transform on the fitted frame: the wheel scales it
  // about the pointer, a drag moves it, and it never leaves a gap inside
  // the area the unzoomed picture filled. Nothing is re-fetched; the
  // picture's own pixels are magnified, so raise Sampling for finer ones.
  const [zoom, setZoom] = useState({ scale: 1, x: 0, y: 0 });
  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;
  const clampZoom = (next: { scale: number; x: number; y: number }) => {
    const scale = Math.min(16, Math.max(1, next.scale));
    if (scale === 1 || !size) return { scale: 1, x: 0, y: 0 };
    return {
      scale,
      x: Math.min(0, Math.max(size.width - size.width * scale, next.x)),
      y: Math.min(0, Math.max(size.height - size.height * scale, next.y)),
    };
  };
  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const wheel = (event: WheelEvent) => {
      const box = frame.current?.getBoundingClientRect();
      if (!box || box.width === 0) return;
      event.preventDefault();
      const current = zoomRef.current;
      const factor = Math.exp(-event.deltaY * (event.deltaMode === 1 ? 0.05 : 0.0015));
      const scale = Math.min(16, Math.max(1, current.scale * factor));
      // Keep the point under the pointer where it is.
      const u = (event.clientX - box.left) / box.width;
      const v = (event.clientY - box.top) / box.height;
      const ratio = scale / current.scale;
      const left = box.left - current.x; // where the unzoomed frame's origin sits
      const top = box.top - current.y;
      const nx = event.clientX - left - u * box.width * ratio;
      const ny = event.clientY - top - v * box.height * ratio;
      setZoom(clampZoom({ scale, x: nx, y: ny }));
    };
    element.addEventListener("wheel", wheel, { passive: false });
    return () => element.removeEventListener("wheel", wheel);
    // The clamp reads the fitted size; a new size means new limits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size]);
  const drag = useRef<{ x: number; y: number; zx: number; zy: number; moved: boolean } | null>(null);
  const pointerDown = (event: React.PointerEvent<HTMLElement>) => {
    const pan = event.button === 1 || (event.button === 0 && !measuring && !onClickCapture);
    if (!pan || zoom.scale === 1) return;
    drag.current = { x: event.clientX, y: event.clientY, zx: zoom.x, zy: zoom.y, moved: false };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  };
  const pointerMove = (event: React.PointerEvent<HTMLElement>) => {
    setHover(fromEvent(event));
    const start = drag.current;
    if (!start) return;
    const dx = event.clientX - start.x;
    const dy = event.clientY - start.y;
    if (Math.hypot(dx, dy) > 3) start.moved = true;
    setZoom(clampZoom({ scale: zoomRef.current.scale, x: start.zx + dx, y: start.zy + dy }));
  };
  const pointerUp = (event: React.PointerEvent<HTMLElement>) => {
    if (!drag.current) return;
    event.currentTarget.releasePointerCapture(event.pointerId);
    // A drag is not a click; the click handler checks this flag right after.
    const moved = drag.current.moved;
    drag.current = null;
    if (moved) suppressClick.current = true;
  };
  const suppressClick = useRef(false);
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
    if (suppressClick.current) {
      suppressClick.current = false;
      return;
    }
    const point = fromEvent(event);
    if (!point) return;
    if (onClickCapture?.(point)) return;
    if (measuring) onPoint(point);
  };
  // The bar shows a round length at the current magnification.
  const bar = scaleBarLength(spanH / zoom.scale);
  const barPixels = size ? (bar / spanH) * size.width * zoom.scale : 0;
  const ends = measurement ? [toFraction(measurement.start), toFraction(measurement.end)] : null;
  const pending = pendingStart ? toFraction(pendingStart) : null;
  const delta = measurement
    ? [measurement.end[0] - measurement.start[0], measurement.end[1] - measurement.start[1]]
    : null;
  return (
    <div className="image-view" ref={container}>
      <div
        ref={frame}
        className={`image-frame ${measuring || onClickCapture ? "drawing" : ""} ${
          zoom.scale > 1 && !measuring && !onClickCapture ? "pannable" : ""
        }`}
        style={
          size
            ? ({
                ...size,
                transform: `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.scale})`,
                transformOrigin: "0 0",
                "--inv-scale": String(1 / zoom.scale),
              } as React.CSSProperties)
            : undefined
        }
        onClick={click}
        onPointerDown={pointerDown}
        onPointerMove={pointerMove}
        onPointerUp={pointerUp}
        onPointerCancel={pointerUp}
        onMouseLeave={() => setHover(null)}
      >
        <img src={`data:image/png;base64,${image}`} alt={alt} draggable={false} />
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
      </div>
      {hover && (
        <code className="cursor-readout">
          {axes[0]} {hover[0].toFixed(3)} · {axes[1]} {hover[1].toFixed(3)} µm
        </code>
      )}
      {size && (
        <span className="scale-bar" style={{ width: `${barPixels}px` }}>
          {formatLength(bar)}
        </span>
      )}
      {zoom.scale > 1 && (
        <button
          type="button"
          className="zoom-reset"
          title="Back to the whole picture (wheel to zoom, drag to pan)"
          onClick={() => setZoom({ scale: 1, x: 0, y: 0 })}
        >
          {zoom.scale.toFixed(1)}× · fit
        </button>
      )}
    </div>
  );
}

/** The AA–BB line drawn over the top view, as fractions of the frame. */
function SectionLineOverlay({
  extent,
  lines,
  activeId,
  pendingStart,
}: {
  extent: ImageExtent;
  lines: SectionLine[];
  activeId: string | null;
  pendingStart: [number, number] | null;
}) {
  const width = extent.horizontalMax - extent.horizontalMin;
  const height = extent.verticalMax - extent.verticalMin;
  const toFraction = ([x, y]: [number, number]): [number, number] => [
    (x - extent.horizontalMin) / width,
    (extent.verticalMax - y) / height,
  ];
  // Every saved line is drawn faintly with its name; the one the section
  // follows is drawn strong, with its A and B.
  const active = lines.find((line) => line.id === activeId) ?? null;
  const others = lines.filter((line) => line.id !== activeId);
  const ends = active ? [toFraction(active.start), toFraction(active.end)] : null;
  const pending = pendingStart ? toFraction(pendingStart) : null;
  return (
    <>
      {(ends || others.length > 0) && (
        <svg className="image-overlay" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">
          {others.map((line) => {
            const [a, b] = [toFraction(line.start), toFraction(line.end)];
            return <line key={line.id} className="faint" x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} />;
          })}
          {ends && <line x1={ends[0][0]} y1={ends[0][1]} x2={ends[1][0]} y2={ends[1][1]} />}
        </svg>
      )}
      {others.map((line) => (
        <span key={line.id} className="line-name" style={percent(toFraction(line.start))}>
          {line.name}
        </span>
      ))}
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
  sectionLines,
  sectionLine,
  windowBounds,
  onSelectLine,
  onSaveLine,
  onRemoveLine,
  smoothSection,
  onSmoothSectionChange,
  materials,
  hiddenMaterials,
  onToggleMaterial,
  onExportMesh,
  onSaveImage,
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
    onSaveLine({ id: "", name: "", start: pendingStart, end: point });
    setPendingStart(null);
    setDrawing(false);
  };
  const stopDrawing = () => {
    setDrawing(false);
    setPendingStart(null);
  };
  // The dialog for exact coordinates: an existing line, or a fresh one
  // laid across the middle of the window to start from.
  const [lineEditor, setLineEditor] = useState<{ line: SectionLine; isNew: boolean } | null>(null);
  const openLineEditor = (line?: SectionLine) => {
    if (line) {
      setLineEditor({ line, isNew: false });
      return;
    }
    const width = windowBounds.xMax - windowBounds.xMin;
    const y = (windowBounds.yMin + windowBounds.yMax) / 2;
    setLineEditor({
      line: {
        id: "",
        name: "",
        start: [Number((windowBounds.xMin + width * 0.1).toFixed(4)), Number(y.toFixed(4))],
        end: [Number((windowBounds.xMax - width * 0.1).toFixed(4)), Number(y.toFixed(4))],
      },
      isNew: true,
    });
  };
  const linePicker = (
    <label className="line-picker">
      Line
      <select
        value={sectionLine?.id ?? ""}
        onChange={(event) => onSelectLine(event.target.value || null)}
      >
        <option value="">{sectionLines.length ? "none" : "none saved"}</option>
        {sectionLines.map((line) => (
          <option key={line.id} value={line.id}>
            {line.name}
          </option>
        ))}
      </select>
    </label>
  );

  // The export menu on the view, and the 3D canvas's own frame grabber.
  const [menu, setMenu] = useState<MenuAnchor | null>(null);
  const snapshot = useRef<(() => string) | null>(null);
  const registerSnapshot = useCallback((capture: (() => string) | null) => {
    snapshot.current = capture;
  }, []);
  const closeMenu = useCallback(() => setMenu(null), []);
  const menuItems = () => {
    if (mode === "surfaces") {
      const ready = !!surfaces && surfaces.surfaces.length > 0;
      return [
        {
          label: "Export mesh (GLB, OBJ, STL, PLY)…",
          icon: <FileBox size={13} />,
          action: onExportMesh,
          disabled: !ready,
        },
        {
          label: "Save this view as PNG…",
          icon: <Camera size={13} />,
          action: () => {
            const capture = snapshot.current;
            if (capture) onSaveImage("3d", capture());
          },
          disabled: !ready || !snapshot.current,
        },
      ];
    }
    const picture = mode === "section" ? section : topView;
    return [
      {
        label: "Save this picture as PNG…",
        icon: <Camera size={13} />,
        action: () => picture && onSaveImage(mode === "section" ? "section" : "top", picture.image),
        disabled: !picture,
      },
    ];
  };

  // The ruler: two clicks measure a distance on the picture. It resets when
  // the view changes; the cursor readout lives in the picture view itself.
  const [measuring, setMeasuring] = useState(false);
  const [measurement, setMeasurement] = useState<Measurement | null>(null);
  const [measureStart, setMeasureStart] = useState<[number, number] | null>(null);
  useEffect(() => {
    setMeasuring(false);
    setMeasurement(null);
    setMeasureStart(null);
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
            {mode === "top" && !drawing && (
              <button
                type="button"
                title="A new AA–BB line from exact coordinates"
                onClick={() => openLineEditor()}
              >
                <Plus size={13} />
                Line by coordinates
              </button>
            )}
            {mode === "top" && !drawing && sectionLines.length > 0 && linePicker}
            {mode === "top" && sectionLine && !drawing && (
              <button type="button" title="Edit or delete this line" onClick={() => openLineEditor(sectionLine)}>
                <PenLine size={13} />
                Edit line
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

      <div
        className="canvas-wrap"
        onContextMenu={(event) => {
          event.preventDefault();
          setMenu({ x: event.clientX, y: event.clientY });
        }}
      >
        {menu && <ContextMenu anchor={menu} items={menuItems()} onClose={closeMenu} />}
        {lineEditor && (
          <SectionLineEditor
            line={lineEditor.line}
            window={windowBounds}
            isNew={lineEditor.isNew}
            onSave={(line) => {
              setLineEditor(null);
              onSaveLine(line);
            }}
            onDelete={() => {
              setLineEditor(null);
              onRemoveLine(lineEditor.line.id);
            }}
            onClose={() => setLineEditor(null)}
          />
        )}
        {error ? (
          <div className="view-placeholder">
            <CircleAlert size={20} />
            <p>{error}</p>
          </div>
        ) : loading && !(mode === "surfaces" ? surfaces : mode === "section" ? section : topView) ? (
          // Nothing to show yet for this step, and the worker is on it: say
          // so, rather than the "run the flow" the empty view would show,
          // which reads as if the result were gone.
          <div className="view-placeholder">
            <LoaderCircle className="spin" size={20} />
            <p>
              {mode === "surfaces"
                ? "Building the 3D view… the first look at a step builds its mesh; later ones are instant."
                : mode === "section"
                  ? "Cutting the section…"
                  : "Drawing the top view…"}
            </p>
          </div>
        ) : mode === "surfaces" ? (
          surfaces && surfaces.surfaces.length > 0 ? (
            <SurfaceScene
              surfaces={surfaces}
              materials={materials}
              hiddenMaterials={hiddenMaterials}
              registerSnapshot={registerSnapshot}
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
            onClickCapture={
              drawing
                ? (point) => {
                    pickPoint(point);
                    return true;
                  }
                : undefined
            }
          >
            <SectionLineOverlay
              extent={topView.extent}
              lines={sectionLines}
              activeId={sectionLine?.id ?? null}
              pendingStart={pendingStart}
            />
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
                <option value="line" disabled={sectionLines.length === 0}>
                  AA–BB line{sectionLines.length ? "" : " (draw one on the top view)"}
                </option>
              </select>
            </label>
            {sectionAxis === "line" ? (
              <>
                {linePicker}
                {sectionLine && (
                  <code className="line-ends">
                    A ({sectionLine.start[0].toFixed(3)}, {sectionLine.start[1].toFixed(3)}) → B (
                    {sectionLine.end[0].toFixed(3)}, {sectionLine.end[1].toFixed(3)}) µm
                  </code>
                )}
                {sectionLine && (
                  <button
                    type="button"
                    className="footer-toggle"
                    title="Edit this line's coordinates"
                    onClick={() => openLineEditor(sectionLine)}
                  >
                    Edit
                  </button>
                )}
              </>
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
        {mode === "section" && section?.exact && (
          <button
            type="button"
            className={smoothSection ? "footer-toggle active" : "footer-toggle"}
            title={
              smoothSection
                ? "Sampled films are drawn as the surface they sample. Click to see the stored slabs, staircase included."
                : "The stored slabs are drawn as they are. Click to join the sampled bands into the surface they sample."
            }
            onClick={() => onSmoothSectionChange(!smoothSection)}
          >
            {smoothSection ? "Smooth steps" : "Exact slabs"}
          </button>
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
