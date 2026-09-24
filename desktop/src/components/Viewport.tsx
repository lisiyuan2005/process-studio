import { OrbitControls } from "@react-three/drei";
import type { OrbitControls as OrbitControlsImpl } from "three-stdlib";
import { Canvas, useThree } from "@react-three/fiber";
import {
  Box,
  Camera,
  CircleAlert,
  Eye,
  EyeOff,
  FileBox,
  FlipHorizontal2,
  Grid2x2,
  Image as ImageIcon,
  LoaderCircle,
  PenLine,
  Plus,
  Ruler,
  Scissors,
  TriangleAlert,
  X,
} from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { MutableRefObject } from "react";
import { createPortal } from "react-dom";
import { ContextMenu, type MenuAnchor } from "./ContextMenu";
import { NumberField } from "./NumberField";
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
  Triangulation,
  VectorPicture,
  VectorShape,
} from "../types";

function decodeInts(text: string): Int32Array {
  const bytes = Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
  return new Int32Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 4);
}

/** SVG path data for a shape's loops (closed) or lines (open). */
function pathData(shape: VectorShape, closed: boolean): string {
  const points = decodeFloats(shape.points);
  const starts = decodeInts(shape.starts);
  const count = points.length / 2;
  const parts: string[] = [];
  for (let i = 0; i < starts.length; i += 1) {
    const from = starts[i];
    const to = i + 1 < starts.length ? starts[i + 1] : count;
    if (to - from < 2) continue;
    let d = `M${points[2 * from].toFixed(4)} ${points[2 * from + 1].toFixed(4)}`;
    for (let j = from + 1; j < to; j += 1) {
      d += `L${points[2 * j].toFixed(4)} ${points[2 * j + 1].toFixed(4)}`;
    }
    parts.push(closed ? `${d}Z` : d);
  }
  return parts.join("");
}

const PICTURE_BACKGROUND = "#f7f9fc";

/** The outlines as SVG markup, for drawing on screen and into a PNG. */
function vectorMarkup(vector: VectorPicture): { fills: [string, string][]; lines: [string, string][] } {
  return {
    fills: vector.shapes.map((shape) => [shape.color, pathData(shape, true)]),
    lines: vector.lines.map((shape) => [shape.color, pathData(shape, false)]),
  };
}

/** A picture sent as outlines, drawn into a PNG (base64, no prefix). */
export async function vectorToPng(vector: VectorPicture, scale = 2): Promise<string> {
  const { fills, lines } = vectorMarkup(vector);
  const width = Math.round(vector.width * scale);
  const height = Math.round(vector.height * scale);
  const body =
    `<rect width="${vector.width}" height="${vector.height}" fill="${PICTURE_BACKGROUND}"/>` +
    fills
      .map(([color, d]) => `<path d="${d}" fill="${color}" fill-rule="evenodd" stroke="${color}" stroke-width="${0.6 / scale}" stroke-linejoin="round"/>`)
      .join("") +
    lines.map(([color, d]) => `<path d="${d}" fill="none" stroke="${color}" stroke-width="${1.5 / scale}"/>`).join("");
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${vector.width} ${vector.height}" preserveAspectRatio="none">${body}</svg>`;
  const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
  try {
    const image = new Image();
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve();
      image.onerror = () => reject(new Error("The picture could not be drawn."));
      image.src = url;
    });
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("No canvas to draw the picture in.");
    context.drawImage(image, 0, 0, width, height);
    return canvas.toDataURL("image/png").split(",", 2)[1];
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** Outlines drawn at whatever size the frame has: sharp at any zoom. */
function VectorImage({ vector, alt }: { vector: VectorPicture; alt: string }) {
  const { fills, lines } = useMemo(() => vectorMarkup(vector), [vector]);
  return (
    <svg
      className="vector-picture"
      viewBox={`0 0 ${vector.width} ${vector.height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={alt}
    >
      <rect width={vector.width} height={vector.height} fill={PICTURE_BACKGROUND} />
      {fills.map(([color, d], index) => (
        // A hairline in the fill's own colour closes the seams antialiasing
        // leaves where two materials meet.
        <path
          key={`f${index}`}
          d={d}
          fill={color}
          fillRule="evenodd"
          stroke={color}
          strokeWidth={0.6}
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
      ))}
      {lines.map(([color, d], index) => (
        <path key={`l${index}`} d={d} fill="none" stroke={color} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
      ))}
    </svg>
  );
}

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
  materials: MaterialDefinition[];
  hiddenMaterials: string[];
  onToggleMaterial: (material: string) => void;
  /** How the 3D view shows each material for now: a colour or opacity other than the library's. */
  looks: Record<string, MaterialLook>;
  /** Change a material's look in the view, or null to go back to the library's. */
  onLookChange: (material: string, look: MaterialLook | null) => void;
  /** Write the 3D surfaces of the shown step to a file the user picks. */
  onExportMesh: () => void;
  /** Write a PNG the user picks a place for; `image` is base64 without prefix. */
  onSaveImage: (kind: "3d" | "section" | "top", image: string) => void;
  surfaces?: SurfaceDocument;
  section?: SectionDocument;
  topView?: TopViewDocument;
  /** Top view: draw the line where one material meets itself at another height. */
  topSteps: boolean;
  onTopStepsChange: (steps: boolean) => void;
  /** 3D view: which triangulator builds the mesh. */
  triangulation: Triangulation;
  onTriangulationChange: (triangulation: Triangulation) => void;
}

/** A temporary colour or opacity for one material in the 3D view; nothing is saved. */
export interface MaterialLook {
  color?: string;
  opacity?: number;
}

/** The clipping plane of the 3D view: an axis, where along it (0..1 of the window) and which side stays. */
export interface ClipState {
  axis: "x" | "y" | "z" | null;
  fraction: number;
  /** False keeps the side towards +axis, true the side towards −axis. */
  flip: boolean;
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
  color,
  opacity,
  offset,
  order,
  exact,
  hiddenMaterials,
  clipPlane,
  tiles,
}: {
  surface: SurfacePayload;
  color: string;
  opacity: number;
  offset: THREE.Vector3;
  /** The plane everything is cut by, or null when the view is whole. */
  clipPlane: THREE.Plane | null;
  /** The material's place in the draw order, for a deterministic depth bias. */
  order: number;
  /** True when the faces are the geometry itself, so no two drawn faces coincide. */
  exact: boolean;
  /** Materials not drawn; faces lying against one of them are shown. */
  hiddenMaterials: string[];
  /** Where to draw this material, in µm; one entry per copy. */
  tiles: [number, number][];
}) {
  const hiddenKey = hiddenMaterials.join("\u0000");
  const geometry = useMemo(() => {
    const buffer = new THREE.BufferGeometry();
    buffer.setAttribute("position", new THREE.BufferAttribute(decodeFloats(surface.positions), 3));
    let indices = decodeIndices(surface.indices);
    // A face between two materials is in both meshes at exactly the same
    // place. Drawn twice, the two copies fight for the pixels and shimmer
    // as the camera moves; and a wafer top a few nanometres under a thin
    // film fights the film's top just the same once the window is wide.
    // So a face against another material is drawn only while that material
    // is hidden, when it is the cavity the material leaves; while it is
    // shown the face is interior anyway.
    const drop = faceFilter(surface, hiddenMaterials);
    if (drop) {
      const kept = new Uint32Array(indices.length);
      let count = 0;
      for (let face = 0; face < drop.length; face += 1) {
        if (drop[face]) continue;
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [surface, hiddenKey]);

  // Marching-cubes buffers are large; release them when the step changes.
  useEffect(() => () => geometry.dispose(), [geometry]);

  const at = (tile: [number, number]): [number, number, number] => [
    tile[0] - offset.x,
    tile[1] - offset.y,
    -offset.z,
  ];
  // A cut solid shows its inside: while the plane is on, the back faces
  // behind the cut are what the eye sees as the interior, so they are drawn.
  const side = exact && !clipPlane ? THREE.FrontSide : THREE.DoubleSide;
  const clippingPlanes = clipPlane ? [clipPlane] : [];
  const translucent = opacity < 1;
  return (
    <group>
      {translucent &&
        // A translucent solid shows what lies behind it, not its own inside:
        // this pass writes only depth, so the colour pass below keeps just
        // the nearest face of the material at each pixel. Without it the
        // trench floor of a film blends through the film's top wherever its
        // triangles happen to be drawn first. It sits in the transparent
        // queue so the opaque materials behind the film are already drawn
        // and still show through.
        tiles.map((tile) => (
          <mesh key={`depth ${tile[0]} ${tile[1]}`} geometry={geometry} position={at(tile)} renderOrder={2 * order}>
            <meshBasicMaterial transparent colorWrite={false} side={side} clippingPlanes={clippingPlanes} />
          </mesh>
        ))}
      {/* One geometry, drawn once per copy: the copies cost a draw each and
          no memory, which is the whole point of tiling a cell rather than
          simulating the array. */}
      {tiles.map((tile) => (
      <mesh key={`${tile[0]} ${tile[1]}`} geometry={geometry} position={at(tile)} renderOrder={translucent ? 2 * order + 1 : 0}>
        <meshStandardMaterial
          color={color}
          transparent={translucent}
          opacity={opacity}
          clippingPlanes={clippingPlanes}
          roughness={0.62}
          metalness={0.08}
          // Exact geometry is closed, so its back faces are never in view;
          // culling them also keeps a thin film's underside from fighting
          // its top for the same pixels in a window many micrometres wide.
          side={side}
          // Level-set interfaces of different materials coincide; they are
          // settled by a small per-material depth bias instead of by whichever
          // triangle happens to rasterise closer this frame. Exact geometry
          // never draws two coincident faces, and the bias would only push a
          // steep wall behind a neighbour's top at grazing angles.
          polygonOffset={!exact}
          polygonOffsetFactor={exact ? 0 : order}
          polygonOffsetUnits={exact ? 0 : order}
        />
      </mesh>
      ))}
    </group>
  );
}

/**
 * Which faces of a surface to leave out: one flag per triangle, or null when
 * every face is drawn. A face against a shown material is left out; a face
 * against a hidden one, or against nothing, is drawn.
 */
function faceFilter(surface: SurfacePayload, hiddenMaterials: string[]): Uint8Array | null {
  if (surface.neighbourFaces && surface.neighbourMaterials) {
    const against = decodeBytes(surface.neighbourFaces);
    const shownNeighbour = surface.neighbourMaterials.map((name) => !hiddenMaterials.includes(name));
    const drop = new Uint8Array(against.length);
    let any = false;
    for (let face = 0; face < against.length; face += 1) {
      const index = against[face];
      if (index !== 255 && shownNeighbour[index]) {
        drop[face] = 1;
        any = true;
      }
    }
    return any ? drop : null;
  }
  if (surface.interfaceFaces && hiddenMaterials.length === 0) {
    // An older payload only says that a face touches something; such faces
    // are interior while everything is shown.
    return decodeBytes(surface.interfaceFaces);
  }
  return null;
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

/**
 * Showing the cell as the array it stands for.
 *
 * A repeating structure is simulated once -- one cell, with the window as
 * its period -- because that is where the saving is: the same array as
 * geometry would be N times the polygons through every step, every
 * harmonise and every mesh. Copies of the finished mesh cost one draw
 * each and nothing at all to compute, so the picture can have the array
 * even though the simulation never did.
 *
 * It is a picture, not a result: a section, a top view or an exported mesh
 * is still the one cell.
 */
export interface TilingState {
  on: boolean;
  countX: number;
  countY: number;
  /** Centre-to-centre spacing, in µm. Defaults to the project window. */
  pitchX: number;
  pitchY: number;
}

/** No more copies than a view can draw without becoming a slideshow. */
export const MAX_TILES = 400;

/** Where each copy sits, in µm, centred on the cell that was simulated. */
export function tileOffsets(tiling: TilingState): [number, number][] {
  if (!tiling.on) return [[0, 0]];
  const countX = Math.max(1, Math.round(tiling.countX));
  const countY = Math.max(1, Math.round(tiling.countY));
  if (countX * countY > MAX_TILES) return [[0, 0]];
  const offsets: [number, number][] = [];
  for (let iy = 0; iy < countY; iy += 1) {
    for (let ix = 0; ix < countX; ix += 1) {
      offsets.push([
        (ix - (countX - 1) / 2) * tiling.pitchX,
        (iy - (countY - 1) / 2) * tiling.pitchY,
      ]);
    }
  }
  return offsets;
}

/**
 * Where the camera was left, so the next step opens on the same view.
 *
 * Switching steps clears the result before the next one arrives -- one
 * step's geometry must not sit on screen labelled as another's -- so the
 * canvas is unmounted and built again, and a new canvas starts at the
 * default three-quarter view. The direction the user turned to is theirs,
 * though, and having to turn back on every step is the thing that makes a
 * flow tedious to look through. So it is remembered out here, where it
 * survives the canvas, and put back when the next one mounts.
 *
 * The span it was stored at comes with it: a taller step is looked at from
 * further back, the same way ``KeepInView`` dollies while mounted.
 */
export interface CameraMemory {
  position: [number, number, number];
  target: [number, number, number];
  span: number;
}

/** A remembered view, put back in front of a model of this size. */
export function restoredCamera(memory: CameraMemory, span: number) {
  const scale = memory.span > 0 && span > 0 ? span / memory.span : 1;
  const scaled = (point: [number, number, number]) =>
    point.map((value) => value * scale) as [number, number, number];
  return {
    position: scaled(memory.position),
    target: scaled(memory.target),
    near: span * 0.02,
    far: span * 40,
  };
}

function RememberCamera({
  memory,
  span,
}: {
  memory: MutableRefObject<CameraMemory | null>;
  span: number;
}) {
  const { camera, controls, invalidate } = useThree();
  const orbit = controls as unknown as OrbitControlsImpl | null;

  useEffect(() => {
    if (!orbit) return;
    const stored = memory.current;
    if (stored && span > 0) {
      const put = restoredCamera(stored, span);
      camera.position.set(...put.position);
      orbit.target.set(...put.target);
      if (camera instanceof THREE.PerspectiveCamera) {
        camera.near = put.near;
        camera.far = put.far;
        camera.updateProjectionMatrix();
      }
      orbit.update();
      invalidate();
    }
    const remember = () => {
      memory.current = {
        position: camera.position.toArray() as [number, number, number],
        target: orbit.target.toArray() as [number, number, number],
        span,
      };
    };
    remember();
    orbit.addEventListener("change", remember);
    return () => orbit.removeEventListener("change", remember);
    // Restoring is for a fresh canvas; a span that changes while this one
    // is mounted is ``KeepInView``'s to handle, and re-running here would
    // undo the turn the user has made since.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orbit, camera, invalidate]);

  return null;
}

/**
 * Dolly out when the model grows, and in when it shrinks.
 *
 * The camera prop is read when the canvas mounts and never again -- which
 * is what keeps the view the user has turned to from being reset on every
 * render -- so turning the copies on would otherwise leave them off the
 * edge of the screen. The model is centred on the origin, which is also
 * where the controls orbit, so keeping it framed is a scale of the
 * camera's position about that point: the direction the user chose is
 * untouched.
 */
function KeepInView({ span }: { span: number }) {
  const { camera, invalidate } = useThree();
  const framed = useRef(span);
  useEffect(() => {
    if (!Number.isFinite(span) || span <= 0 || Math.abs(span - framed.current) < 1e-9) return;
    const scale = span / framed.current;
    framed.current = span;
    camera.position.multiplyScalar(scale);
    if (camera instanceof THREE.PerspectiveCamera) {
      camera.near = span * 0.02;
      camera.far = span * 40;
      camera.updateProjectionMatrix();
    }
    invalidate();
  }, [span, camera, invalidate]);
  return null;
}

/** Where a clip setting cuts, in model coordinates along its axis. */
function clipPosition(clip: ClipState, bounds: SurfaceDocument["bounds"]): number {
  if (!clip.axis) return 0;
  const low = bounds[`${clip.axis}Min`];
  const high = bounds[`${clip.axis}Max`];
  return low + clip.fraction * (high - low);
}

function SurfaceScene({
  surfaces,
  materials,
  hiddenMaterials,
  looks,
  clip,
  tiling,
  registerSnapshot,
  cameraMemory,
}: {
  surfaces: SurfaceDocument;
  materials: MaterialDefinition[];
  hiddenMaterials: string[];
  looks: Record<string, MaterialLook>;
  clip: ClipState;
  tiling: TilingState;
  registerSnapshot: (capture: (() => string) | null) => void;
  /** Where the camera was left, kept outside the canvas so it survives it. */
  cameraMemory: MutableRefObject<CameraMemory | null>;
}) {
  const { bounds } = surfaces;
  const tiles = useMemo(() => tileOffsets(tiling), [tiling]);
  // The copies are centred on the cell, so the cell's centre is still the
  // middle of what is on screen; only how far back the camera has to sit
  // depends on how many there are.
  const spread = {
    x: Math.max(...tiles.map(([x]) => Math.abs(x))) * 2,
    y: Math.max(...tiles.map(([, y]) => Math.abs(y))) * 2,
  };
  const span = Math.max(
    bounds.xMax - bounds.xMin + spread.x,
    bounds.yMax - bounds.yMin + spread.y,
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
  // The clipping plane in scene coordinates (the model sits at -center).
  // three keeps what lies on the plane's normal side; flipping turns the
  // normal round so the other half stays.
  const clipPlane = useMemo(() => {
    if (!clip.axis) return null;
    const at = clipPosition(clip, bounds) - center[clip.axis];
    const normal = new THREE.Vector3(
      clip.axis === "x" ? 1 : 0,
      clip.axis === "y" ? 1 : 0,
      clip.axis === "z" ? 1 : 0,
    );
    if (clip.flip) normal.negate();
    return new THREE.Plane(normal, clip.flip ? at : -at);
  }, [clip, bounds, center]);
  const planeSize = span * 1.3;
  const planeRotation: [number, number, number] =
    clip.axis === "x" ? [0, Math.PI / 2, 0] : clip.axis === "y" ? [Math.PI / 2, 0, 0] : [0, 0, 0];
  const planeAt = clip.axis ? clipPosition(clip, bounds) - center[clip.axis] : 0;

  return (
    // Frames are drawn only when something changed: a still scene stays
    // still, and a driver that re-presents each frame differently has
    // nothing to flicker with.
    <Canvas camera={camera} dpr={[1, 2]} frameloop="demand" gl={{ localClippingEnabled: true }}>
      <color attach="background" args={["#f4f7f9"]} />
      <KeepInView span={span} />
      <RememberCamera memory={cameraMemory} span={span} />
      <ambientLight intensity={0.72} />
      <directionalLight position={[span, -span, span * 1.6]} intensity={1.25} />
      <directionalLight position={[-span, span * 0.6, span]} intensity={0.45} />
      <group>
        {shown.map((surface, index) => {
          const library = materials.find((material) => material.name === surface.material);
          const look = looks[surface.material] ?? {};
          return (
            <SurfaceMesh
              key={surface.material}
              surface={surface}
              color={look.color ?? library?.color ?? surface.color}
              opacity={look.opacity ?? library?.opacity ?? 1}
              offset={center}
              order={index}
              exact={surfaces.exact === true}
              hiddenMaterials={hiddenMaterials}
              clipPlane={clipPlane}
              tiles={tiles}
            />
          );
        })}
        {clip.axis && (
          // A faint sheet where the cut is, so the plane can be placed by eye.
          <mesh
            rotation={planeRotation}
            position={[
              clip.axis === "x" ? planeAt : 0,
              clip.axis === "y" ? planeAt : 0,
              clip.axis === "z" ? planeAt : 0,
            ]}
          >
            <planeGeometry args={[planeSize, planeSize]} />
            <meshBasicMaterial color="#4e8fe8" transparent opacity={0.08} side={THREE.DoubleSide} depthWrite={false} />
          </mesh>
        )}
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
  vector,
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
  memory,
  children,
}: {
  image: string;
  /** The picture as outlines; drawn instead of `image` when there is one. */
  vector?: VectorPicture;
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
  /**
   * Where this view was zoomed and panned to, kept by the caller so that
   * switching steps -- which unmounts this while the next picture is
   * fetched -- does not throw it away.
   */
  memory: MutableRefObject<{ scale: number; x: number; y: number }>;
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

  // Zoom and pan resize and move the fitted frame: the wheel scales it
  // about the pointer, a drag moves it, and it never leaves a gap inside
  // the area the unzoomed picture filled. Nothing is re-fetched. The frame
  // is laid out at its zoomed size rather than scaled as a finished layer,
  // so outlines are drawn again sharp at every magnification (a scaled
  // layer is a bitmap stretched, and blurs).
  const [zoom, setZoom] = useState(memory.current);
  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;
  useEffect(() => {
    memory.current = zoom;
  }, [zoom, memory]);
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
  // A zoom taken from another picture -- another step, another axis -- can
  // sit outside what this one's fitted size allows, and the limits are not
  // known until it has one.
  useEffect(() => {
    // Not before there is one: the clamp reads the fitted size, and with
    // none it answers "no zoom at all", which would throw the remembered
    // zoom away on the very mount that is meant to restore it.
    if (!size) return;
    setZoom((current) => clampZoom(current));
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
                width: size.width * zoom.scale,
                height: size.height * zoom.scale,
                transform: `translate(${zoom.x}px, ${zoom.y}px)`,
                transformOrigin: "0 0",
                "--inv-scale": "1",
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
        {vector ? (
          <VectorImage vector={vector} alt={alt} />
        ) : (
          <img src={`data:image/png;base64,${image}`} alt={alt} draggable={false} />
        )}
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
  materials,
  hiddenMaterials,
  onToggleMaterial,
  looks,
  onLookChange,
  onExportMesh,
  onSaveImage,
  surfaces,
  section,
  topView,
  topSteps,
  onTopStepsChange,
  triangulation,
  onTriangulationChange,
}: ViewportProps) {
  // The 3D clipping plane and the material whose look is being edited.
  const [clip, setClip] = useState<ClipState>({ axis: null, fraction: 0.5, flip: false });
  // Outside the canvas, so switching steps -- which unmounts it while the
  // next result is fetched -- does not throw away the view the user turned to.
  const cameraMemory = useRef<CameraMemory | null>(null);
  // The same for the flat views, one each: the section and the top view are
  // looked at at different magnifications and should not share a zoom.
  const sectionZoom = useRef({ scale: 1, x: 0, y: 0 });
  const topZoom = useRef({ scale: 1, x: 0, y: 0 });
  // The window is the cell's period when the cell was cut to be one, which
  // is the case this is for, so that is where the pitch starts.
  const [tiling, setTiling] = useState<TilingState>({
    on: false,
    countX: 3,
    countY: 3,
    pitchX: windowBounds.xMax - windowBounds.xMin,
    pitchY: windowBounds.yMax - windowBounds.yMin,
  });
  // A different project, a different cell: follow its window until the user
  // has said otherwise.
  const windowSize = `${windowBounds.xMax - windowBounds.xMin}x${windowBounds.yMax - windowBounds.yMin}`;
  const chosenPitch = useRef(false);
  useEffect(() => {
    if (chosenPitch.current) return;
    setTiling((current) => ({
      ...current,
      pitchX: windowBounds.xMax - windowBounds.xMin,
      pitchY: windowBounds.yMax - windowBounds.yMin,
    }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [windowSize]);
  const [lookEditor, setLookEditor] = useState<{ material: MaterialDefinition; x: number; y: number } | null>(null);
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
        action: () => {
          if (!picture) return;
          const kind = mode === "section" ? "section" : "top";
          if (picture.image || !picture.vector) {
            onSaveImage(kind, picture.image);
            return;
          }
          void vectorToPng(picture.vector).then((png) => onSaveImage(kind, png));
        },
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
              looks={looks}
              clip={clip}
              tiling={tiling}
              registerSnapshot={registerSnapshot}
              cameraMemory={cameraMemory}
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
              vector={section.vector}
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
              memory={sectionZoom}
            />
          ) : (
            <div className="view-placeholder">
              <p>Run the flow to see a cut through the stack.</p>
            </div>
          )
        ) : topView ? (
          <PictureView
            image={topView.image}
            vector={topView.vector}
            width={topView.width}
            height={topView.height}
            extent={topView.extent}
            axes={["x", "y"]}
            alt="Top view"
            measuring={measuring}
            measurement={measurement}
            pendingStart={measureStart}
            onPoint={measurePoint}
            memory={topZoom}
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
        {mode === "top" && (
          // Colour says which material; this says where that material is at
          // two different heights, which colour alone cannot.
          <button
            type="button"
            className={`footer-toggle ${topSteps ? "active" : ""}`}
            title={
              topSteps
                ? "Stop drawing the steps inside a material"
                : "Draw the line where one material meets itself at another height, such as the rim of a trench cut into the wafer"
            }
            onClick={() => onTopStepsChange(!topSteps)}
          >
            Steps
          </button>
        )}
        {mode === "surfaces" && (
          <label>
            Mesh
            <select
              value={triangulation}
              title={
                "How the flat faces are cut into triangles. Both describe the same "
                + "solid; ear clipping builds it about twice as fast."
              }
              onChange={(event) => onTriangulationChange(event.target.value as Triangulation)}
            >
              <option value="ears">Ear clipping</option>
              <option value="delaunay">Delaunay</option>
            </select>
          </label>
        )}
        {mode === "surfaces" && (
          <div className="clip-controls tile-controls" role="group" aria-label="Repeat the cell">
            <button
              type="button"
              className={`footer-toggle ${tiling.on ? "active" : ""}`}
              title={
                tiling.on
                  ? "Show the one cell that was simulated"
                  : "Draw copies of this cell in a grid. Only the picture repeats: the simulation, the section, the top view and an exported mesh are all still the one cell."
              }
              onClick={() => setTiling((current) => ({ ...current, on: !current.on }))}
            >
              <Grid2x2 size={13} />
            </button>
            {tiling.on && (
              <>
                <NumberField
                  className="tile-count"
                  aria-label="Copies across x"
                  min={1}
                  max={MAX_TILES}
                  step={1}
                  value={tiling.countX}
                  onChange={(countX) => {
                    chosenPitch.current = true;
                    setTiling((current) => ({ ...current, countX: Math.round(countX) }));
                  }}
                />
                <span>×</span>
                <NumberField
                  className="tile-count"
                  aria-label="Copies across y"
                  min={1}
                  max={MAX_TILES}
                  step={1}
                  value={tiling.countY}
                  onChange={(countY) => {
                    chosenPitch.current = true;
                    setTiling((current) => ({ ...current, countY: Math.round(countY) }));
                  }}
                />
                <span title="Centre-to-centre spacing of the copies">pitch</span>
                <NumberField
                  className="tile-pitch"
                  aria-label="Pitch in x, µm"
                  step={0.001}
                  value={tiling.pitchX}
                  onChange={(pitchX) => {
                    chosenPitch.current = true;
                    setTiling((current) => ({ ...current, pitchX }));
                  }}
                />
                <NumberField
                  className="tile-pitch"
                  aria-label="Pitch in y, µm"
                  step={0.001}
                  value={tiling.pitchY}
                  onChange={(pitchY) => {
                    chosenPitch.current = true;
                    setTiling((current) => ({ ...current, pitchY }));
                  }}
                />
                <code>µm</code>
                {Math.round(tiling.countX) * Math.round(tiling.countY) > MAX_TILES && (
                  <span className="tile-warning" role="status">
                    over {MAX_TILES} copies: showing one
                  </span>
                )}
              </>
            )}
          </div>
        )}
        {mode === "surfaces" && (
          <div className="clip-controls" role="group" aria-label="Clipping plane">
            <span>Clip</span>
            {(["x", "y", "z"] as const).map((axis) => (
              <button
                key={axis}
                type="button"
                className={`footer-toggle ${clip.axis === axis ? "active" : ""}`}
                title={clip.axis === axis ? "Turn the clipping plane off" : `Cut the model with a plane across ${axis}`}
                onClick={() => setClip((current) => ({ ...current, axis: current.axis === axis ? null : axis }))}
              >
                {axis.toUpperCase()}
              </button>
            ))}
            {clip.axis && (
              <>
                <input
                  type="range"
                  min={0}
                  max={1000}
                  value={Math.round(clip.fraction * 1000)}
                  aria-label="Where the plane cuts"
                  onChange={(event) => setClip((current) => ({ ...current, fraction: Number(event.target.value) / 1000 }))}
                />
                <code>
                  {clip.axis} = {surfaces ? clipPosition(clip, surfaces.bounds).toFixed(3) : "–"} µm
                </code>
                <button
                  type="button"
                  className={`footer-toggle ${clip.flip ? "active" : ""}`}
                  title={clip.flip ? `Keeping the −${clip.axis} side; click to keep the +${clip.axis} side` : `Keeping the +${clip.axis} side; click to keep the −${clip.axis} side`}
                  aria-label="Keep the other side of the plane"
                  onClick={() => setClip((current) => ({ ...current, flip: !current.flip }))}
                >
                  <FlipHorizontal2 size={13} />
                </button>
              </>
            )}
          </div>
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
          {
            materials
            .filter((material) => shownMaterials.includes(material.name))
            .map((material) =>
              mode === "surfaces" || mode === "top" ? (
                <button
                  key={material.id}
                  type="button"
                  className={`${hiddenMaterials.includes(material.name) ? "hidden-material" : ""} ${mode === "surfaces" && looks[material.name] ? "custom-look" : ""}`}
                  title={
                    mode === "top"
                      ? hiddenMaterials.includes(material.name)
                        ? `Show ${material.name} in the view from above`
                        : `Look through ${material.name}: the view from above then shows what is under it.`
                      : (hiddenMaterials.includes(material.name)
                          ? `Show ${material.name} in the 3D view`
                          : `Hide ${material.name} in the 3D view`) +
                        ". Right-click, or click the swatch, for its colour and opacity here."
                  }
                  onClick={() => onToggleMaterial(material.name)}
                  onContextMenu={(event) => {
                    if (mode !== "surfaces") return;
                    event.preventDefault();
                    setLookEditor({ material, x: event.clientX, y: event.clientY });
                  }}
                >
                  <i
                    style={{
                      background:
                        mode === "surfaces"
                          ? looks[material.name]?.color ?? material.color
                          : material.color,
                      opacity:
                        mode === "surfaces"
                          ? 0.35 + 0.65 * (looks[material.name]?.opacity ?? material.opacity)
                          : 1,
                    }}
                    onClick={(event) => {
                      // The colour and opacity here are the 3D view's own.
                      if (mode !== "surfaces") return;
                      event.stopPropagation();
                      const box = event.currentTarget.getBoundingClientRect();
                      setLookEditor({ material, x: box.left, y: box.top - 6 });
                    }}
                  />
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
            )
          }
        </div>
      </div>
      {lookEditor && (
        <MaterialLookPopover
          material={lookEditor.material}
          look={looks[lookEditor.material.name] ?? {}}
          hidden={hiddenMaterials.includes(lookEditor.material.name)}
          anchor={lookEditor}
          onChange={(look) => onLookChange(lookEditor.material.name, look)}
          onToggleHidden={() => onToggleMaterial(lookEditor.material.name)}
          onClose={() => setLookEditor(null)}
        />
      )}
    </section>
  );
}

/**
 * A material's colour and opacity for the 3D view only. It opens above the
 * legend chip, stays until a click elsewhere or Escape, and never touches
 * the material library: the section and top view keep the library's colours.
 */
function MaterialLookPopover({
  material,
  look,
  hidden,
  anchor,
  onChange,
  onToggleHidden,
  onClose,
}: {
  material: MaterialDefinition;
  look: MaterialLook;
  hidden: boolean;
  anchor: { x: number; y: number };
  onChange: (look: MaterialLook | null) => void;
  onToggleHidden: () => void;
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
      // Above the anchor when it fits, else below.
      top: anchor.y - box.height - 8 >= 8 ? anchor.y - box.height - 8 : Math.min(anchor.y + 8, window.innerHeight - box.height - 8),
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
    return () => {
      window.removeEventListener("pointerdown", away, true);
      window.removeEventListener("keydown", key);
    };
  }, [onClose]);
  const color = look.color ?? material.color;
  const opacity = look.opacity ?? material.opacity;
  const changed = look.color !== undefined || look.opacity !== undefined;
  const update = (patch: MaterialLook) => {
    const next: MaterialLook = { ...look, ...patch };
    if (next.color === material.color) delete next.color;
    if (next.opacity === material.opacity) delete next.opacity;
    onChange(next.color === undefined && next.opacity === undefined ? null : next);
  };
  return createPortal(
    <div
      ref={ref}
      className="look-popover"
      role="dialog"
      aria-label={`How ${material.name} looks in the 3D view`}
      style={{ left: place.left, top: place.top }}
      onContextMenu={(event) => event.preventDefault()}
    >
      <div className="look-title">
        <i style={{ background: color }} />
        <strong>{material.name}</strong>
        <span>3D view only</span>
      </div>
      <label>
        <span>Colour</span>
        <input type="color" value={color} onChange={(event) => update({ color: event.target.value })} />
      </label>
      <label>
        <span>Opacity</span>
        <input
          type="range"
          min={0}
          max={100}
          value={Math.round(opacity * 100)}
          onChange={(event) => update({ opacity: Number(event.target.value) / 100 })}
        />
        <code>{Math.round(opacity * 100)}%</code>
      </label>
      <div className="look-actions">
        <button type="button" onClick={onToggleHidden}>
          {hidden ? <Eye size={12} /> : <EyeOff size={12} />}
          {hidden ? "Show" : "Hide"}
        </button>
        <button type="button" disabled={!changed} onClick={() => onChange(null)}>
          Library colour
        </button>
      </div>
    </div>,
    document.body,
  );
}
