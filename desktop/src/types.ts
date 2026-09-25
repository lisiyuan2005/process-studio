export type ProcessType = "deposit" | "etch" | "cmp" | "no_geometry" | "oxidation" | "flip";
export type MaskSource = "none" | "quick_sketch" | "gds";
export type MaskKeep = "inside" | "outside";
/**
 * `clean` is a current result, `stale` a stored result that is no longer
 * current, `dirty` a step that has never run and has nothing stored.
 */
export type StepStatus = "clean" | "stale" | "dirty" | "running" | "failed";
export type ParameterValue = string | number | boolean | null | number[];

export interface GridDefinition {
  xMin: number;
  xMax: number;
  yMin: number;
  yMax: number;
  zMin: number;
  zMax: number;
  nx: number;
  ny: number;
  nz: number;
  /** Node spacing in micrometres, reported by the worker. */
  spacingUm: number;
  nodeCount: number;
}

/** The project window in micrometres. The wafer surface is z = 0 inside it. */
export interface WindowBounds {
  xMin: number;
  xMax: number;
  yMin: number;
  yMax: number;
  zMin: number;
  zMax: number;
}

export interface GridEstimate {
  spacingNm: number;
  shape: [number, number, number];
  nodeCount: number;
  /** Bytes for the saved state: one double-precision field per material. */
  stateBytes: number;
  /** Bytes to run comfortably, including solver temporaries. */
  recommendedBytes: number;
}

export interface GridPlan {
  kernel: string;
  /** "grid" when the spacing is the lattice, otherwise a plain resolution. */
  spacingRole: SpacingRole;
  grid: GridDefinition;
  /** A kernel without a field reports only the spacing it would work at. */
  estimate: GridEstimate | { spacingNm: number; spacingXyNm: number };
  maximumNodes: number | null;
  withinLimit: boolean;
  unchanged: boolean;
}

export type SpacingRole = "grid" | "conformal_resolution";

/** One simulation core a project can be built on. */
export interface KernelDescription {
  id: string;
  name: string;
  version: string;
  summary: string;
  processTypes: ProcessType[];
  maskSources: MaskSource[];
  depositionModes: string[];
  /** Accepted directional_fraction values, empty when any value works. */
  directionalFractions: number[];
  surfaces: boolean;
  spacingRole: SpacingRole;
  spacingPresetsNm: number[];
  maximumNodes: number | null;
}

export interface MaterialDefinition {
  id: string;
  name: string;
  category: string;
  color: string;
  opacity: number;
}

export interface MaterialResponse {
  material: string;
  rateUmPerMin: number;
  stopLayer: boolean;
}

/** A machine or bench a step runs on; `group` is a path such as "Etch/Dry". */
export interface ToolDefinition {
  id: string;
  name: string;
  group: string;
  notes: string;
  /** The recipes loaded on this machine, by the names the lab uses. */
  recipes?: string[];
}

export interface Recipe {
  id: string;
  name: string;
  processType: ProcessType;
  tool: string;
  /** Where the recipe sits below its process type, e.g. "ALD/Oxides"; empty for the type itself. */
  group: string;
  outputMaterial: string | null;
  parameters: Record<string, ParameterValue>;
  /** What the tool is set to; null means the same as `parameters`. */
  experimentParameters?: Record<string, ParameterValue> | null;
  materialResponses: Record<string, MaterialResponse>;
}

/**
 * A block of steps repeated N times. Every iteration is a real step in the
 * flow (each with its own result), tagged with the loop and its 0-based
 * iteration; the editor keeps the iterations identical.
 */
export interface StepLoop {
  id: string;
  name: string;
  repeat: number;
  iteration: number;
}

export interface ProcessStep {
  id: string;
  name: string;
  processType: ProcessType;
  tool: string;
  outputMaterial: string | null;
  parameters: Record<string, ParameterValue>;
  /**
   * What the tool is set to, when that is not what the kernel is asked to
   * build. Null (or absent) means the two are the same, which is the usual
   * case. A kernel never reads it and a digest never covers it, so writing
   * down what the machine did cannot make a result stale.
   */
  experimentParameters?: Record<string, ParameterValue> | null;
  materialResponses: Record<string, MaterialResponse>;
  maskSource: MaskSource;
  layer: number | null;
  datatype: number | null;
  keep: MaskKeep;
  enabled: boolean;
  /** The loop the step belongs to; absent or null when it stands alone. */
  loop?: StepLoop | null;
}

export interface FlowBranch {
  id: string;
  name: string;
  parentBranchId: string | null;
  parentStepId: string | null;
  steps: ProcessStep[];
}

export interface ProjectSummary {
  id: string;
  name: string;
  grid: GridDefinition;
  gdsPath: string | null;
  activeBranchId: string | null;
  /** Fixed when the project was created; the worker refuses to change it. */
  kernel: string;
  /** Length a gridless kernel resolves geometry at, in micrometres. */
  resolutionUm: number | null;
  /** The slab kernel's XY arc sagitta when it differs from the z step; null follows it. */
  resolutionXyUm: number | null;
  sectionLines: SectionLine[];
  /**
   * How the slab kernel shapes films: "detailed" (rounded, sampled at the
   * resolution) or "simplified" (square corners, much faster). Results of
   * both are stored side by side.
   */
  fidelity?: Fidelity;
}

export type Fidelity = "detailed" | "simplified" | "voxel";

export interface SketchShape {
  kind: "rectangle" | "circle" | "polygon" | "path";
  operation: "merge" | "subtract" | "intersect";
  parameters: Record<string, unknown>;
  array: [number, number, number, number];
}

export interface QuickSketch {
  id: string;
  name: string;
  shapes: SketchShape[];
}

export interface WorkspaceDocument {
  root: string;
  project: ProjectSummary;
  branches: FlowBranch[];
  recipes: Recipe[];
  materials: MaterialDefinition[];
  tools: ToolDefinition[];
  sketches: QuickSketch[];
  /** Step status per branch id, as computed from stored snapshot digests. */
  stepStatuses: Record<string, Record<string, StepStatus>>;
  /**
   * Library entries the user deleted since the last save, by id. The
   * library is shared by every project, so only these are deleted there:
   * an entry a document merely lacks is left alone.
   */
  deleted?: Partial<Record<LibraryKind, string[]>>;
}

export type LibraryKind = "materials" | "tools" | "recipes";
export type FlowExportFormat = "xlsx" | "csv" | "json" | "yaml";

/** The newest published release, compared with this build. */
export interface UpdateInfo {
  currentVersion: string;
  latestVersion: string;
  /**
   * The latest release is a different build from this one. Not "greater":
   * the version was reset when the project narrowed to one kernel, so the
   * release to install can be a lower number than a machine is running.
   */
  available: boolean;
  /** Whether that release is also a higher version than this build. */
  newer: boolean;
  releaseUrl: string;
  publishedAt: string | null;
  notes: string;
  /** The download for this platform and edition, when the release carries one. */
  asset: { name: string; url: string; sizeBytes: number } | null;
}

/** One command line run inside the worker, as the CLI would have run it. */
export interface CliResult {
  exitCode: number;
  stdout: string;
  stderr: string;
  /** The workspace as it stands after the command, when it still is one. */
  document?: WorkspaceDocument;
}

export interface RunResult {
  branchId: string;
  executedStepIds: string[];
  cachedStepIds: string[];
  elapsedMs: number;
  materials: string[];
  stepStatuses: Record<string, StepStatus>;
}

export interface WorkerEvent {
  kind: "progress" | "log";
  /** The request this event belongs to, when the worker was answering one. */
  requestId?: string;
  stepId?: string;
  message: string;
  completed?: number;
  total?: number;
}

export interface SurfacePayload {
  material: string;
  color: string;
  /** Base64 little-endian Float32Array, three components per vertex. */
  positions: string;
  /** Empty when the kernel sends flat geometry: the viewer then shades each face on its own. */
  normals: string;
  shading?: "flat" | "smooth";
  /** Base64 little-endian Uint32Array, three indices per triangle. */
  indices: string;
  /** Base64 Uint8Array, one flag per triangle: 1 where the face lies against another material. */
  interfaceFaces?: string;
  /** Base64 Uint8Array, one entry per triangle: the index in `neighbourMaterials` of the material the face lies against, 255 for none. */
  neighbourFaces?: string;
  neighbourMaterials?: string[];
  vertexCount: number;
  triangleCount: number;
}

export interface SurfaceDocument {
  interpolation: number;
  /** Absent when the kernel's geometry is exact rather than sampled. */
  sampledSpacingUm?: number;
  /** True when the surfaces are the geometry itself, not an isosurface. */
  exact?: boolean;
  /** Which triangulator built these meshes; absent from other kernels. */
  triangulation?: Triangulation;
  /** How a voxel state was drawn. */
  mesh?: VoxelMesh;
  /** True when the faces against other materials are in here as well. */
  buried?: boolean;
  bounds: {
    xMin: number;
    xMax: number;
    yMin: number;
    yMax: number;
    zMin: number;
    zMax: number;
  };
  surfaces: SurfacePayload[];
}

export interface ImageExtent {
  horizontalMin: number;
  horizontalMax: number;
  verticalMin: number;
  verticalMax: number;
}

/** The AA–BB line of a free section: two points in micrometres. */
/** A named AA–BB cut, saved with the project; points are in micrometres. */
export interface SectionLine {
  id: string;
  name: string;
  start: [number, number];
  end: [number, number];
}

export type SectionAxis = "x" | "y" | "line";

/** One material's region (or step lines) as loops in the picture's frame. */
export interface VectorShape {
  material: string;
  color: string;
  /** Base64 float32 (x, y) pairs, x to the right and y down, in picture units. */
  points: string;
  /** Base64 int32: where each loop (or line) starts in `points`. */
  starts: string;
}

/** A picture as outlines: filled even-odd in order, then the lines on top. */
export interface VectorPicture {
  width: number;
  height: number;
  shapes: VectorShape[];
  lines: VectorShape[];
}

export interface SectionDocument {
  /** Base64 PNG of the cut, already flipped so row zero is the top; empty when `vector` is sent. */
  image: string;
  vector?: VectorPicture;
  axis: SectionAxis;
  position: number;
  index: number;
  interpolation: number;
  sampledSpacingUm: number;
  width: number;
  height: number;
  /** "s" is distance along the AA–BB line. */
  horizontalAxis: "x" | "y" | "s";
  extent: ImageExtent;
  /** Empty for a line cut, which has no slider. */
  positions: number[];
  line?: SectionLine;
  exact?: boolean;
  /** The materials this step's result holds: what the legend lists. */
  materials?: string[];
}

/** Where a sketch exposes the wafer, drawn by the worker over the project window. */
export interface MaskPreview {
  image: string;
  width: number;
  height: number;
  exposedFraction: number;
  extent: ImageExtent;
}

/**
 * How the slab kernel's display mesh is triangulated. The two describe the
 * same solid -- same vertices, same triangle count, same area and volume --
 * and differ only in which diagonals cut a flat cap, so this compares them
 * rather than changing an answer. Ear clipping is the default and is about
 * thirty times faster over a cap with a hole array.
 */
export type Triangulation = "ears" | "delaunay";

/**
 * How the voxel model's 3D view is drawn: a face for every fine cell side
 * along a boundary (drawn from a coarser copy when the grid is fine), or
 * each slab's materials as polygons -- the full resolution, far fewer
 * triangles, built once per step and slower the first time.
 */
export type VoxelMesh = "cells" | "polygons";

export interface TopViewDocument {
  image: string;
  vector?: VectorPicture;
  width: number;
  height: number;
  extent: ImageExtent;
  exact?: boolean;
  /** The materials this step's result holds, hidden ones included: what the legend lists. */
  materials?: string[];
}

export interface GdsLayer {
  layer: number;
  datatype: number;
}

export interface GdsImportResult {
  fileName: string;
  path: string;
  layers: GdsLayer[];
  document: WorkspaceDocument;
}

/** One column of the exported flow table. */
export interface FlowColumn {
  id: string;
  label: string;
}

export interface WorkerCapabilities {
  workerVersion: string;
  protocolVersion: number;
  processTypes: ProcessType[];
  maskSources: MaskSource[];
  sketch: { shapes: string[]; operations: string[] };
  kernels: KernelDescription[];
  defaultKernel: string;
  /** The columns a flow table can be exported with, in table order. */
  flowColumns?: FlowColumn[];
  /** "full" when the build ships every kernel it knows of. */
  buildVariant?: string;
  rendering: { surfaces: boolean; maximumInterpolation: number };
  limits: { interpolationIsDisplayOnly: boolean; calibrated: boolean };
  /** How to run this same worker as the command-line tool: the program and its leading arguments. */
  cli?: { command: string[]; packaged: boolean };
}
