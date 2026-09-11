import type { DesktopBridge, RunOptions, ViewRequest } from "./bridge";
import type {
  GdsImportResult,
  ProcessStep,
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

const NO_KERNEL =
  "The browser preview has no process kernel. Start the desktop shell with " +
  "npm run tauri dev to run the flow.";

/**
 * Layout-only stand-in used by `npm run dev` in a plain browser. It serves one
 * fixed document so the shell can be worked on without Python, and refuses
 * every call that would need a real result rather than inventing one.
 */
export class BrowserBridge implements DesktopBridge {
  readonly runtime = "browser" as const;

  private document: WorkspaceDocument = demoDocument();

  async describe(): Promise<WorkerCapabilities> {
    return {
      workerVersion: "preview",
      protocolVersion: 2,
      processTypes: ["deposit", "etch", "cmp", "no_geometry"],
      maskSources: ["none", "quick_sketch", "gds"],
      sketch: {
        shapes: ["rectangle", "circle", "polygon", "path"],
        operations: ["merge", "subtract", "intersect"],
      },
      rendering: { surfaces: false, maximumInterpolation: 4 },
      kernels: [
        {
          id: "levelset",
          name: "Level set",
          version: "preview",
          summary: "Signed distance fields on a uniform grid.",
          processTypes: ["deposit", "etch", "cmp", "no_geometry"],
          maskSources: ["none", "quick_sketch", "gds"],
          depositionModes: ["conformal", "directional", "evaporation", "fill"],
          directionalFractions: [],
          surfaces: false,
          spacingRole: "grid",
          spacingPresetsNm: [25, 12.5, 6.25],
          maximumNodes: 20_000_000,
        },
        {
          id: "slab",
          name: "Slab (DeviceFlow)",
          version: "preview",
          summary: "Exact polygon slabs from the DeviceFlow core.",
          processTypes: ["deposit", "etch", "cmp", "no_geometry"],
          maskSources: ["none", "quick_sketch", "gds"],
          depositionModes: ["conformal", "planar"],
          directionalFractions: [0, 1],
          surfaces: false,
          spacingRole: "conformal_resolution",
          spacingPresetsNm: [25, 10, 2],
          maximumNodes: null,
        },
      ],
      defaultKernel: "levelset",
      numerics: {
        solverOrders: [1, 2],
        refinementFactors: [2, 4, 8],
        defaultMaxNodes: 20_000_000,
        maximumNodes: 20_000_000,
        spacingPresetsNm: [25, 12.5, 6.25],
      },
      limits: { interpolationIsDisplayOnly: true, calibrated: false },
    };
  }

  async createWorkspace(name: string, kernel: string): Promise<WorkspaceDocument | null> {
    this.document = {
      ...demoDocument(),
      project: { ...demoDocument().project, name, kernel },
    };
    return this.document;
  }

  async openWorkspace(): Promise<WorkspaceDocument | null> {
    return this.document;
  }

  async saveDocument(document: WorkspaceDocument): Promise<WorkspaceDocument> {
    this.document = document;
    return document;
  }

  async planGrid(): Promise<GridPlan> {
    // Matching a spacing to the project bounds is the kernel's search, not a
    // rounding rule the preview could fake.
    throw new Error(NO_KERNEL);
  }

  async setGrid(): Promise<WorkspaceDocument> {
    throw new Error(NO_KERNEL);
  }

  async saveSketch(_root: string, sketch: QuickSketch): Promise<WorkspaceDocument> {
    const sketches = this.document.sketches.some((item) => item.id === sketch.id)
      ? this.document.sketches.map((item) => (item.id === sketch.id ? sketch : item))
      : [...this.document.sketches, sketch];
    this.document = { ...this.document, sketches };
    return this.document;
  }

  async runFlow(_root: string, _options: RunOptions): Promise<RunResult> {
    throw new Error(NO_KERNEL);
  }

  async cancel(): Promise<void> {
    // Nothing runs in the preview, so there is nothing to stop.
  }

  async getSurfaces(_root: string, _request: ViewRequest): Promise<SurfaceDocument> {
    throw new Error(NO_KERNEL);
  }

  async getSection(): Promise<SectionDocument> {
    throw new Error(NO_KERNEL);
  }

  async getTopView(): Promise<TopViewDocument> {
    throw new Error(NO_KERNEL);
  }

  async importGds(): Promise<GdsImportResult | null> {
    throw new Error(NO_KERNEL);
  }

  async exportRecipes(): Promise<string | null> {
    throw new Error(NO_KERNEL);
  }

  async importRecipes(): Promise<WorkspaceDocument | null> {
    throw new Error(NO_KERNEL);
  }

  async subscribeToWorkerEvents(_callback: (event: WorkerEvent) => void): Promise<() => void> {
    return () => undefined;
  }
}

/** Mirrors process_studio.defaults so the preview matches a fresh workspace. */
export function demoDocument(): WorkspaceDocument {
  const branchId = "default-main";
  const steps: ProcessStep[] = [
    {
      id: "step-litho", name: "Lithography", processType: "no_geometry", tool: "Stepper",
      outputMaterial: null, parameters: { sketch_id: "default" }, materialResponses: {},
      maskSource: "quick_sketch", layer: null, datatype: null, keep: "inside", enabled: true,
    },
    {
      id: "step-etch", name: "Trench Etch", processType: "etch", tool: "ICP-RIE",
      outputMaterial: null, parameters: { sketch_id: "default", target: 0.32, directional_fraction: 0.9 },
      materialResponses: { Si: { material: "Si", rateUmPerMin: 0.12, stopLayer: false } },
      maskSource: "quick_sketch", layer: null, datatype: null, keep: "inside", enabled: true,
    },
    {
      id: "step-strip", name: "Resist Strip", processType: "no_geometry", tool: "Ash",
      outputMaterial: null, parameters: {}, materialResponses: {}, maskSource: "none",
      layer: null, datatype: null, keep: "inside", enabled: true,
    },
    {
      id: "step-ald", name: "Conformal Al2O3", processType: "deposit", tool: "ALD",
      outputMaterial: "Al2O3", parameters: { target: 0.04, temperature_c: 250, rate: 0.002 },
      materialResponses: {}, maskSource: "none", layer: null, datatype: null, keep: "inside", enabled: true,
    },
  ];
  return {
    root: "(browser preview)",
    project: {
      id: "default-project",
      name: "Preview Project",
      grid: {
        xMin: -0.8,
        xMax: 0.8,
        yMin: -0.8,
        yMax: 0.8,
        zMin: -0.8,
        zMax: 0.4,
        nx: 41,
        ny: 41,
        nz: 31,
        spacingUm: 0.04,
        nodeCount: 41 * 41 * 31,
      },
      gdsPath: null,
      activeBranchId: branchId,
      kernel: "levelset",
      resolutionUm: null,
    },
    branches: [
      {
        id: branchId,
        name: "main",
        parentBranchId: null,
        parentStepId: null,
        steps,
      },
    ],
    recipes: [
      {
        id: "recipe-si-trench",
        name: "Si Directional Trench Etch",
        processType: "etch",
        tool: "ICP-RIE",
        outputMaterial: null,
        parameters: { target: 0.32, directional_fraction: 0.9 },
        materialResponses: { Si: { material: "Si", rateUmPerMin: 0.12, stopLayer: false } },
      },
      {
        id: "recipe-ald-al2o3",
        name: "Conformal Al2O3",
        processType: "deposit",
        tool: "ALD",
        outputMaterial: "Al2O3",
        parameters: { target: 0.04, temperature_c: 250, rate: 0.002 },
        materialResponses: {},
      },
      {
        id: "recipe-litho",
        name: "Lithography",
        processType: "no_geometry",
        tool: "Stepper",
        outputMaterial: null,
        parameters: {},
        materialResponses: {},
      },
      {
        id: "recipe-strip",
        name: "Resist Strip",
        processType: "no_geometry",
        tool: "Ash",
        outputMaterial: null,
        parameters: {},
        materialResponses: {},
      },
    ],
    materials: [
      { id: "material-si", name: "Si", category: "Semiconductor", color: "#7b68b8", opacity: 1 },
      { id: "material-sio2", name: "SiO2", category: "Dielectric", color: "#e8c46a", opacity: 0.9 },
      { id: "material-al2o3", name: "Al2O3", category: "Dielectric", color: "#ef9b35", opacity: 0.9 },
      { id: "material-tin", name: "TiN", category: "Metal", color: "#b9a43b", opacity: 1 },
      { id: "material-w", name: "W", category: "Metal", color: "#7f8790", opacity: 1 },
    ],
    sketches: [
      {
        id: "default",
        name: "default",
        shapes: [
          {
            kind: "circle",
            operation: "merge",
            parameters: { center: [0, 0], radius: 0.22 },
            array: [1, 1, 0, 0],
          },
        ],
      },
    ],
    stepStatuses: {
      [branchId]: Object.fromEntries(steps.map((step) => [step.id, "dirty" as const])),
    },
  };
}
