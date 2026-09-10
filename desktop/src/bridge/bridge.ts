import type {
  GdsImportResult,
  GridDefinition,
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
  createWorkspace(name: string): Promise<WorkspaceDocument | null>;
  openWorkspace(): Promise<WorkspaceDocument | null>;
  saveDocument(document: WorkspaceDocument): Promise<WorkspaceDocument>;
  setGrid(root: string, grid: GridDefinition): Promise<WorkspaceDocument>;
  saveSketch(root: string, sketch: QuickSketch): Promise<WorkspaceDocument>;
  runFlow(root: string, options: RunOptions): Promise<RunResult>;
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
