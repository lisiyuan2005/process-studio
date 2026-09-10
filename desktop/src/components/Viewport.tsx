import { OrbitControls } from "@react-three/drei";
import { Canvas } from "@react-three/fiber";
import { Box, CircleAlert, Image as ImageIcon, LoaderCircle, Ruler } from "lucide-react";
import { useEffect, useMemo } from "react";
import * as THREE from "three";
import type {
  MaterialDefinition,
  SectionDocument,
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
  surfacesSupported: boolean;
  maximumInterpolation: number;
  interpolation: number;
  onInterpolationChange: (value: number) => void;
  sectionAxis: "x" | "y";
  onSectionAxisChange: (axis: "x" | "y") => void;
  sectionIndex: number;
  onSectionIndexChange: (index: number) => void;
  materials: MaterialDefinition[];
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
}: {
  surfaces: SurfaceDocument;
  materials: MaterialDefinition[];
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
        {surfaces.surfaces.map((surface) => (
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

export function Viewport({
  mode,
  onModeChange,
  title,
  loading,
  error,
  surfacesSupported,
  maximumInterpolation,
  interpolation,
  onInterpolationChange,
  sectionAxis,
  onSectionAxisChange,
  sectionIndex,
  onSectionIndexChange,
  materials,
  surfaces,
  section,
  topView,
}: ViewportProps) {
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
            <SurfaceScene surfaces={surfaces} materials={materials} />
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
            <div className="image-view">
              <img
                src={`data:image/png;base64,${section.image}`}
                alt={`Section at ${section.axis} = ${section.position.toFixed(3)} µm`}
              />
            </div>
          ) : (
            <div className="view-placeholder">
              <p>Run the flow to see a cut through the stack.</p>
            </div>
          )
        ) : topView ? (
          <div className="image-view">
            <img src={`data:image/png;base64,${topView.image}`} alt="Top view" />
          </div>
        ) : (
          <div className="view-placeholder">
            <p>Run the flow to see the top view.</p>
          </div>
        )}

        {mode !== "top" && (
          <span className="preview-disclaimer">
            Sampling density is a display setting. It does not change what the kernel computed.
          </span>
        )}
      </div>

      <div className="viewport-footer">
        {mode === "section" && section && (
          <>
            <label>
              Axis
              <select
                value={sectionAxis}
                onChange={(event) => onSectionAxisChange(event.target.value as "x" | "y")}
              >
                <option value="y">Cut along y</option>
                <option value="x">Cut along x</option>
              </select>
            </label>
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
            .map((material) => (
              <span key={material.id}>
                <i style={{ background: material.color }} />
                {material.name}
              </span>
            ))}
        </div>
      </div>
    </section>
  );
}
