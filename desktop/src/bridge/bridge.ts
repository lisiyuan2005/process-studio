import type {
  GdsImportResult,
  GridPlan,
  MaskKeep,
  MaskPreview,
  QuickSketch,
  RunResult,
  SectionAxis,
  SectionDocument,
  SectionLine,
  SurfaceDocument,
  TopViewDocument,
  WindowBounds,
  WorkerCapabilities,
  WorkerEvent,
  WorkspaceDocument,
} from "../types";

export interface RunOptions {
  branchId?: string;
  throughStepId?: string;
  force?: boolean;
}

export interface ViewRequest {
  branchId: string;
  /** Empty means the bare wafer, before the first step. */
  stepId: string;
  interpolation?: number;
}

export interface DesktopBridge {
  readonly runtime: "tauri" | "browser";
  describe(): Promise<WorkerCapabilities>;
  /** The kernel is chosen here and only here: a project keeps it for life. */
  createWorkspace(name: string, kernel: string): Promise<WorkspaceDocument | null>;
  openWorkspace(): Promise<WorkspaceDocument | null>;
  /** Open a workspace whose directory is already known, as on a reload. */
  openWorkspaceAt(root: string): Promise<WorkspaceDocument>;
  saveDocument(document: WorkspaceDocument): Promise<WorkspaceDocument>;
  /** `bounds` is the project window in µm; omitted means keep the current one. */
  /** `xyNm` is the slab kernel's XY arc sagitta; null lets it follow the z step. */
  planGrid(root: string, targetSpacingNm: number, bounds?: WindowBounds, xyNm?: number | null): Promise<GridPlan>;
  setGrid(root: string, targetSpacingNm: number, bounds?: WindowBounds, xyNm?: number | null): Promise<WorkspaceDocument>;
  saveSketch(root: string, sketch: QuickSketch): Promise<WorkspaceDocument>;
  /** The kernel's own reading of an unsaved sketch, for the editor's fill. */
  previewMask(root: string, sketch: QuickSketch, keep: MaskKeep): Promise<MaskPreview>;
  /** `requestId` names the run so `cancel` can withdraw it while it runs. */
  runFlow(root: string, options: RunOptions, requestId?: string): Promise<RunResult>;
  /** Stop a run: a queued one is refused at once, a running one at its next step. */
  cancel(requestId: string): Promise<void>;
  /** `loft` joins a sampled kernel's bands into the surface they sample; default true. */
  getSurfaces(root: string, request: ViewRequest & { loft?: boolean }): Promise<SurfaceDocument>;
  /** `smooth` draws a sampled kernel's bands as the surface they sample; default true. */
  getSection(
    root: string,
    request: ViewRequest & { axis: SectionAxis; position?: number; line?: SectionLine; smooth?: boolean },
  ): Promise<SectionDocument>;
  getTopView(root: string, request: ViewRequest): Promise<TopViewDocument>;
  /** Ask where to save, then write the 3D surfaces there; null when the dialog is dismissed. */
  exportMesh(root: string, request: ViewRequest & { loft?: boolean }, defaultName: string): Promise<string | null>;
  /** Ask where to save, then write a PNG there; null when the dialog is dismissed. */
  saveImage(defaultName: string, imageBase64: string): Promise<string | null>;
  importGds(root: string): Promise<GdsImportResult | null>;
  exportRecipes(root: string, projectName: string): Promise<string | null>;
  importRecipes(root: string): Promise<WorkspaceDocument | null>;
  subscribeToWorkerEvents(callback: (event: WorkerEvent) => void): Promise<() => void>;
}
