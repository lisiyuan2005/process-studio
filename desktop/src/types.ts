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
  grid: GridDefinition;
  estimate: GridEstimate;
  maximumNodes: number;
  withinLimit: boolean;
  unchanged: boolean;
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
  normals: string;
  /** Base64 little-endian Uint32Array, three indices per triangle. */
  indices: string;
  vertexCount: number;
  triangleCount: number;
}

export interface SurfaceDocument {
  interpolation: number;
  sampledSpacingUm: number;
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

export interface SectionDocument {
  /** Base64 PNG of the cut, already flipped so row zero is the top. */
  image: string;
  axis: "x" | "y";
  position: number;
  index: number;
  interpolation: number;
  sampledSpacingUm: number;
  width: number;
  height: number;
  horizontalAxis: "x" | "y";
  extent: ImageExtent;
  positions: number[];
}

export interface TopViewDocument {
  image: string;
  width: number;
  height: number;
  extent: ImageExtent;
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
