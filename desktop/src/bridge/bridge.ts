import type {
  CliResult,
  FlowExportFormat,
  LibraryKind,
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
  TopShading,
  Triangulation,
  UpdateInfo,
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
  /**
   * Run this step again even though its digest says it is up to date, and
   * every step after it, which starts from what it leaves. What a step
   * depends on is not all visible to the digest.
   */
  fromStepId?: string;
  /**
   * Also prepare, for every step of the run, the faces between materials
   * that hiding one needs. Opening a step's 3D view prepares its own, so
   * this is only for walking a whole flow without ever waiting.
   */
  prepareBuried?: boolean;
}

export interface ViewRequest {
  branchId: string;
  /** Empty means the bare wafer, before the first step. */
  stepId: string;
  interpolation?: number;
  /** Top view only: colour by topmost material (default) or by surface height. */
  shading?: TopShading;
  /** 3D view only: which triangulator builds the mesh. */
  triangulation?: Triangulation;
  /**
   * 3D view only: also build the faces that lie against another material.
   * They are invisible while both materials are shown and are most of a
   * stack's mesh, so they are asked for only when something is hidden.
   */
  buried?: boolean;
  /**
   * Top view only: materials to look through. They are neither drawn nor
   * allowed to cover what is under them, so hiding the resist shows the
   * hole it is standing in rather than a blank.
   */
  hidden?: string[];
  /**
   * Top view only: draw the line where one material meets itself at
   * another height. Coloured by material a step inside one material is
   * invisible -- the wafer and the floor of a trench cut into it are the
   * same silicon -- so this is on unless it is turned off.
   */
  steps?: boolean;
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
  /**
   * Run one CLI command (without the program) against `root`, inside the
   * worker. `stdin` stands in for a file argument of `-`; `requestId` lets
   * `cancel` stop a `run` started this way.
   */
  runCli(root: string, argv: string[], stdin?: string, requestId?: string): Promise<CliResult>;
  /** Ask GitHub for the newest release and how it compares with this build. */
  checkUpdate(): Promise<UpdateInfo>;
  /** Download the release at `url`, unpack it and start the updater; resolves when the app should quit. */
  installUpdate(url: string): Promise<void>;
  /** Leave so the updater can replace the application. */
  quitForUpdate(): Promise<void>;
  /** Write the flow as a table or a flow file; the user picks the place. Null when they cancel. */
  exportFlow(root: string, format: FlowExportFormat, projectName: string): Promise<string | null>;
  /** Make the workspace match a flow file the user picks. Null when they cancel. */
  importFlow(root: string): Promise<WorkspaceDocument | null>;
  exportLibrary(root: string, kind: LibraryKind, projectName: string): Promise<string | null>;
  importLibrary(root: string, kind: LibraryKind): Promise<WorkspaceDocument | null>;
  /** Copy the workspace to a directory the user picks and open the copy. Null when they cancel. */
  saveWorkspaceAs(root: string, projectName: string): Promise<WorkspaceDocument | null>;
  /** Show a workspace directory in the system file manager. */
  revealPath(path: string): Promise<void>;
  /** Open one of the repository's own pages (a release, a download) in the browser. */
  openUrl(url: string): Promise<void>;
  getSurfaces(root: string, request: ViewRequest): Promise<SurfaceDocument>;
  getSection(
    root: string,
    request: ViewRequest & { axis: SectionAxis; position?: number; line?: SectionLine },
  ): Promise<SectionDocument>;
  getTopView(root: string, request: ViewRequest): Promise<TopViewDocument>;
  /** Ask where to save, then write the 3D surfaces there; null when the dialog is dismissed. */
  exportMesh(root: string, request: ViewRequest, defaultName: string): Promise<string | null>;
  /** Ask where to save, then write a PNG there; null when the dialog is dismissed. */
  saveImage(defaultName: string, imageBase64: string): Promise<string | null>;
  importGds(root: string): Promise<GdsImportResult | null>;
  exportRecipes(root: string, projectName: string): Promise<string | null>;
  importRecipes(root: string): Promise<WorkspaceDocument | null>;
  subscribeToWorkerEvents(callback: (event: WorkerEvent) => void): Promise<() => void>;
}
