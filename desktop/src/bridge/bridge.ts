import type {
  GdsImportResult,
  GridPlan,
  QuickSketch,
  RunResult,
  SectionDocument,
  SurfaceDocument,
  TopViewDocument,
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
  saveDocument(document: WorkspaceDocument): Promise<WorkspaceDocument>;
  planGrid(root: string, targetSpacingNm: number): Promise<GridPlan>;
  setGrid(root: string, targetSpacingNm: number): Promise<WorkspaceDocument>;
  saveSketch(root: string, sketch: QuickSketch): Promise<WorkspaceDocument>;
  /** `requestId` names the run so `cancel` can withdraw it while it runs. */
  runFlow(root: string, options: RunOptions, requestId?: string): Promise<RunResult>;
  /** Stop a run: a queued one is refused at once, a running one at its next step. */
  cancel(requestId: string): Promise<void>;
  getSurfaces(root: string, request: ViewRequest): Promise<SurfaceDocument>;
  getSection(
    root: string,
    request: ViewRequest & { axis: "x" | "y"; position?: number },
  ): Promise<SectionDocument>;
  getTopView(root: string, request: ViewRequest): Promise<TopViewDocument>;
  importGds(root: string): Promise<GdsImportResult | null>;
  exportRecipes(root: string, projectName: string): Promise<string | null>;
  importRecipes(root: string): Promise<WorkspaceDocument | null>;
  subscribeToWorkerEvents(callback: (event: WorkerEvent) => void): Promise<() => void>;
}
