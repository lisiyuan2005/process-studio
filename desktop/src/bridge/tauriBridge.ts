import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open, save } from "@tauri-apps/plugin-dialog";
import type { DesktopBridge, RunOptions, ViewRequest } from "./bridge";
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

const WORKER_EVENT = "process-studio-worker";

function call<T>(
  method: string,
  params: Record<string, unknown> = {},
  requestId?: string,
): Promise<T> {
  return invoke<T>("worker_invoke", { method, params, requestId: requestId ?? null });
}

function safeFileName(name: string) {
  return name.replace(/[^a-zA-Z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "process-studio";
}

export class TauriBridge implements DesktopBridge {
  readonly runtime = "tauri" as const;

  describe(): Promise<WorkerCapabilities> {
    return call<WorkerCapabilities>("describe");
  }

  async createWorkspace(name: string, kernel: string): Promise<WorkspaceDocument | null> {
    const trimmed = name.trim();
    if (!trimmed) return null;
    const parent = await open({
      directory: true,
      multiple: false,
      title: "Choose where the workspace directory is created",
    });
    if (!parent || Array.isArray(parent)) return null;
    const separator = parent.includes("\\") ? "\\" : "/";
    const root = `${parent}${separator}${safeFileName(trimmed)}`;
    return call<WorkspaceDocument>("create_workspace", { root, name: trimmed, kernel });
  }

  async openWorkspace(): Promise<WorkspaceDocument | null> {
    const root = await open({
      directory: true,
      multiple: false,
      title: "Open a Process Studio workspace",
    });
    if (!root || Array.isArray(root)) return null;
    return call<WorkspaceDocument>("open_workspace", { root });
  }

  saveDocument(document: WorkspaceDocument): Promise<WorkspaceDocument> {
    return call<WorkspaceDocument>("save_document", { root: document.root, document });
  }

  planGrid(root: string, targetSpacingNm: number): Promise<GridPlan> {
    return call<GridPlan>("plan_grid", { root, targetSpacingNm });
  }

  setGrid(root: string, targetSpacingNm: number): Promise<WorkspaceDocument> {
    return call<WorkspaceDocument>("set_grid", { root, targetSpacingNm });
  }

  saveSketch(root: string, sketch: QuickSketch): Promise<WorkspaceDocument> {
    return call<WorkspaceDocument>("save_sketch", {
      root,
      sketchId: sketch.id,
      sketch: { name: sketch.name, shapes: sketch.shapes },
    });
  }

  runFlow(root: string, options: RunOptions, requestId?: string): Promise<RunResult> {
    return call<RunResult>("run_flow", { root, ...options }, requestId);
  }

  cancel(requestId: string): Promise<void> {
    return invoke<void>("worker_cancel", { requestId });
  }

  getSurfaces(root: string, request: ViewRequest): Promise<SurfaceDocument> {
    return call<SurfaceDocument>("get_surfaces", { root, ...request });
  }

  getSection(
    root: string,
    request: ViewRequest & { axis: "x" | "y"; position?: number },
  ): Promise<SectionDocument> {
    return call<SectionDocument>("get_section", { root, ...request });
  }

  getTopView(root: string, request: ViewRequest): Promise<TopViewDocument> {
    return call<TopViewDocument>("get_top_view", { root, ...request });
  }

  async importGds(root: string): Promise<GdsImportResult | null> {
    const source = await open({
      directory: false,
      multiple: false,
      title: "Import a GDSII layout",
      filters: [{ name: "GDSII layout", extensions: ["gds"] }],
    });
    if (!source || Array.isArray(source)) return null;
    return call<GdsImportResult>("import_gds", { root, source });
  }

  async exportRecipes(root: string, projectName: string): Promise<string | null> {
    const destination = await save({
      title: "Export the recipe library",
      defaultPath: `${safeFileName(projectName)}-recipes.xlsx`,
      filters: [{ name: "Excel workbook", extensions: ["xlsx"] }],
    });
    if (!destination) return null;
    const result = await call<{ path: string }>("export_recipes_xlsx", { root, destination });
    return result.path;
  }

  async importRecipes(root: string): Promise<WorkspaceDocument | null> {
    const source = await open({
      directory: false,
      multiple: false,
      title: "Import a recipe workbook",
      filters: [{ name: "Excel workbook", extensions: ["xlsx"] }],
    });
    if (!source || Array.isArray(source)) return null;
    const result = await call<{ imported: number; document: WorkspaceDocument }>(
      "import_recipes_xlsx",
      { root, source },
    );
    return result.document;
  }

  subscribeToWorkerEvents(callback: (event: WorkerEvent) => void): Promise<() => void> {
    return listen<WorkerEvent>(WORKER_EVENT, (event) => callback(event.payload));
  }
}
