export type ProcessType = "deposit" | "etch" | "cmp" | "no_geometry";
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
  estimate: GridEstimate | { spacingNm: number };
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

export interface Recipe {
  id: string;
  name: string;
  processType: ProcessType;
  tool: string;
  outputMaterial: string | null;
  parameters: Record<string, ParameterValue>;
  materialResponses: Record<string, MaterialResponse>;
}

export interface ProcessStep {
  id: string;
  name: string;
  processType: ProcessType;
  tool: string;
  outputMaterial: string | null;
  parameters: Record<string, ParameterValue>;
  materialResponses: Record<string, MaterialResponse>;
  maskSource: MaskSource;
  layer: number | null;
  datatype: number | null;
  keep: MaskKeep;
  enabled: boolean;
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
}

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
  sketches: QuickSketch[];
  /** Step status per branch id, as computed from stored snapshot digests. */
  stepStatuses: Record<string, Record<string, StepStatus>>;
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
  vertexCount: number;
  triangleCount: number;
}

export interface SurfaceDocument {
  interpolation: number;
  /** Absent when the kernel's geometry is exact rather than sampled. */
  sampledSpacingUm?: number;
  /** True when the surfaces are the geometry itself, not an isosurface. */
  exact?: boolean;
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
export interface SectionLine {
  start: [number, number];
  end: [number, number];
}

export type SectionAxis = "x" | "y" | "line";

export interface SectionDocument {
  /** Base64 PNG of the cut, already flipped so row zero is the top. */
  image: string;
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
}

/** Where a sketch exposes the wafer, drawn by the worker over the project window. */
export interface MaskPreview {
  image: string;
  width: number;
  height: number;
  exposedFraction: number;
  extent: ImageExtent;
}

export interface TopViewDocument {
  image: string;
  width: number;
  height: number;
  extent: ImageExtent;
  exact?: boolean;
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

export interface WorkerCapabilities {
  workerVersion: string;
  protocolVersion: number;
  processTypes: ProcessType[];
  maskSources: MaskSource[];
  sketch: { shapes: string[]; operations: string[] };
  kernels: KernelDescription[];
  defaultKernel: string;
  /** "full", or the id of the one kernel a single-kernel build ships. */
  buildVariant?: string;
  rendering: { surfaces: boolean; maximumInterpolation: number };
  numerics: {
    solverOrders: number[];
    refinementFactors: number[];
    defaultMaxNodes: number;
    maximumNodes: number;
    spacingPresetsNm: number[];
  };
  limits: { interpolationIsDisplayOnly: boolean; calibrated: boolean };
}
