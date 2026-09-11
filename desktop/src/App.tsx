import {
  ArrowLeft,
  CircleAlert,
  CheckCircle2,
  Cpu,
  FileSpreadsheet,
  Grid3x3,
  LoaderCircle,
  Map as MapIcon,
  Palette,
  Play,
  Save,
  Square,
  TerminalSquare,
  Wrench,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { bridge } from "./bridge";
import { GridEditor } from "./components/GridEditor";
import { Inspector } from "./components/Inspector";
import { MaterialEditor } from "./components/MaterialEditor";
import { ToolEditor } from "./components/ToolEditor";
import { PanelResizer } from "./components/PanelResizer";
import { ProjectHome } from "./components/ProjectHome";
import { RecipeEditor } from "./components/RecipeEditor";
import { SketchEditor } from "./components/SketchEditor";
import { StepList } from "./components/StepList";
import { Viewport, type ViewMode } from "./components/Viewport";
import {
  addStep,
  duplicateStep,
  getActiveBranch,
  newId,
  getSteps,
  hasDirtySteps,
  loadRecipeIntoStep,
  markStep,
  recipeFromStep,
  removeMaterial,
  removeRecipe,
  moveStep,
  nextSectionLineName,
  removeSectionLine,
  removeStep,
  removeTool,
  renameStep,
  reorderSteps,
  setActiveBranch,
  setStepProcessType,
  setStepStatuses,
  stepAccentColor,
  stepStatus,
  toggleStep,
  updateStep,
  updateStepParameters,
  upsertMaterial,
  upsertSectionLine,
  upsertTool,
  toolUsage,
  upsertRecipe,
} from "./domain/project";
import type {
  ParameterValue,
  QuickSketch,
  SectionAxis,
  SectionDocument,
  SectionLine,
  SurfaceDocument,
  TopViewDocument,
  WindowBounds,
  WorkerCapabilities,
  WorkerEvent,
  WorkspaceDocument,
} from "./types";

import {
  clearLastWorkspace,
  forgetWorkspace,
  lastWorkspace,
  recentWorkspaces,
  rememberWorkspace,
  type RecentWorkspace,
} from "./domain/recent";

import {
  clampPanelWidths,
  loadPanelWidths,
  savePanelWidths,
  type PanelWidths,
} from "./domain/layout";

type SaveState = "saved" | "saving" | "unsaved" | "error";

const DEFAULT_PANELS: PanelWidths = { steps: 286, inspector: 306 };

const AUTOSAVE_DELAY_MS = 600;

function errorMessage(reason: unknown) {
  return reason instanceof Error ? reason.message : String(reason);
}

export default function App() {
  const [document, setDocumentState] = useState<WorkspaceDocument | null>(null);
  const [capabilities, setCapabilities] = useState<WorkerCapabilities>();
  const [selectedStepId, setSelectedStepId] = useState("");
  const [busy, setBusy] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>("saved");
  const [homeError, setHomeError] = useState<string>();
  const [recent, setRecent] = useState<RecentWorkspace[]>(recentWorkspaces);
  // A reload of the window must not land on the home page: the workspace
  // that was open is reopened first, and the home page shows only if that
  // fails or nothing was open.
  const [restoring, setRestoring] = useState(
    () => bridge.runtime === "tauri" && lastWorkspace() !== null,
  );
  const [events, setEvents] = useState<WorkerEvent[]>([]);
  const [showLog, setShowLog] = useState(false);
  const [showMaterials, setShowMaterials] = useState(false);
  const [showRecipes, setShowRecipes] = useState(false);
  const [showTools, setShowTools] = useState(false);
  const [showGrid, setShowGrid] = useState(false);
  // The sketch being drawn: an existing one by id, or a fresh one for this step.
  const [sketchEditor, setSketchEditor] = useState<{ sketch: QuickSketch; isNew: boolean } | null>(null);
  const [sketchBackdrop, setSketchBackdrop] = useState<TopViewDocument>();
  const [mode, setMode] = useState<ViewMode>("surfaces");
  const [interpolation, setInterpolation] = useState(1);
  const [sectionAxis, setSectionAxis] = useState<SectionAxis>("y");
  // Which of the project's saved AA–BB lines the section follows.
  const [activeLineId, setActiveLineId] = useState<string | null>(null);
  const [sectionIndex, setSectionIndex] = useState<number | null>(null);
  // Sampled films drawn as the surface they sample, or as the stored slabs.
  const [smoothSection, setSmoothSection] = useState(true);
  // Side panel widths the user dragged; null means the stylesheet's defaults.
  const [panelWidths, setPanelWidths] = useState<PanelWidths | null>(loadPanelWidths);
  const resizePanel = (side: "steps" | "inspector", width: number) => {
    const current = panelWidths ?? DEFAULT_PANELS;
    const next = clampPanelWidths({ ...current, [side]: width }, window.innerWidth);
    setPanelWidths(next);
    savePanelWidths(next);
  };
  const resetPanels = () => {
    setPanelWidths(null);
    savePanelWidths(null);
  };
  const [surfaces, setSurfaces] = useState<SurfaceDocument>();
  const [section, setSection] = useState<SectionDocument>();
  const [topView, setTopView] = useState<TopViewDocument>();
  const [viewLoading, setViewLoading] = useState(false);
  const [viewError, setViewError] = useState<string>();

  const [hiddenMaterials, setHiddenMaterials] = useState<string[]>([]);

  const skipNextAutosave = useRef(false);
  const runningStepId = useRef<string | undefined>(undefined);
  // The id of the run in flight, so the Stop button can name it.
  const runRequestId = useRef<string | undefined>(undefined);
  // Only the newest view request may write to the view; a slower earlier one
  // must not land on top of it and show another step's geometry.
  const viewToken = useRef(0);
  const shownTarget = useRef<string>("");
  // Views already fetched for a stored result, keyed by what was asked for.
  // A stored result only changes when a run recomputes it, so the cache is
  // dropped at every run and at every window change, and switching between
  // 3D, section and top view of the same step costs nothing in between.
  const viewCache = useRef(new Map<string, SurfaceDocument | SectionDocument | TopViewDocument>());
  // The cut position lives in micrometres so it survives a step change.
  const sectionPosition = useRef<number | null>(null);

  const branch = document ? getActiveBranch(document) : undefined;
  const root = document?.root;
  const branchId = branch?.id;
  const steps = document ? getSteps(document) : [];
  const sectionLines = document?.project.sectionLines ?? [];
  const sectionLine = sectionLines.find((line) => line.id === activeLineId) ?? null;
  const selectedStep = steps.find((step) => step.id === selectedStepId);
  const statuses = useMemo(
    () => (document && branch ? document.stepStatuses[branch.id] ?? {} : {}),
    [document, branch],
  );
  const selectedStatus = selectedStepId ? statuses[selectedStepId] ?? "dirty" : "clean";
  const projectKernel = capabilities?.kernels.find(
    (item) => item.id === document?.project.kernel,
  );

  const setDocument = (next: WorkspaceDocument, persist = true) => {
    if (!persist) skipNextAutosave.current = true;
    setDocumentState(next);
    if (persist) setSaveState("unsaved");
  };

  useEffect(() => {
    bridge
      .describe()
      .then(setCapabilities)
      .catch((reason) => setHomeError(errorMessage(reason)));
  }, []);

  useEffect(() => {
    let disposed = false;
    let unsubscribe: (() => void) | undefined;
    bridge
      .subscribeToWorkerEvents((event) => {
        if (event.kind === "progress" && event.stepId && event.message.startsWith("Running")) {
          runningStepId.current = event.stepId;
        }
        setEvents((current) => [...current.slice(-149), event]);
      })
      .then((listener) => {
        if (disposed) listener();
        else unsubscribe = listener;
      })
      .catch((reason) =>
        setEvents((current) => [
          ...current,
          { kind: "log", message: `Could not attach worker events: ${errorMessage(reason)}` },
        ]),
      );
    return () => {
      disposed = true;
      unsubscribe?.();
    };
  }, []);

  useEffect(() => {
    if (!capabilities) return;
    // Whether surfaces can be drawn is the kernel's answer, not the build's:
    // the slab kernel hands over its own triangles and needs no extractor.
    const supported = projectKernel?.surfaces ?? capabilities.rendering.surfaces;
    if (!supported && mode === "surfaces") setMode("section");
  }, [capabilities, projectKernel, mode]);

  // Autosave: the worker is the store of record, so every edit is written back.
  useEffect(() => {
    if (!document || busy) return;
    if (skipNextAutosave.current) {
      skipNextAutosave.current = false;
      return;
    }
    const handle = window.setTimeout(async () => {
      setSaveState("saving");
      try {
        const saved = await bridge.saveDocument(document);
        skipNextAutosave.current = true;
        setDocumentState(saved);
        setSaveState("saved");
      } catch (reason) {
        setSaveState("error");
        setEvents((current) => [
          ...current,
          { kind: "log", message: `Save failed: ${errorMessage(reason)}` },
        ]);
        setShowLog(true);
      }
    }, AUTOSAVE_DELAY_MS);
    return () => window.clearTimeout(handle);
  }, [document, busy]);

  const openDocument = (next: WorkspaceDocument) => {
    skipNextAutosave.current = true;
    viewCache.current.clear();
    setDocumentState(next);
    setSaveState("saved");
    setRecent(rememberWorkspace(next.root, next.project.name));
    const first = getSteps(next)[0]?.id ?? "";
    setSelectedStepId(first);
    setSectionIndex(null);
    sectionPosition.current = null;
    setHiddenMaterials([]);
  };

  const handleCreate = async (name: string, kernel: string) => {
    setBusy(true);
    setHomeError(undefined);
    try {
      const created = await bridge.createWorkspace(name, kernel);
      if (created) openDocument(created);
    } catch (reason) {
      setHomeError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const handleOpen = async () => {
    setBusy(true);
    setHomeError(undefined);
    try {
      const opened = await bridge.openWorkspace();
      if (opened) openDocument(opened);
    } catch (reason) {
      setHomeError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const handleOpenRecent = async (rootPath: string) => {
    setBusy(true);
    setHomeError(undefined);
    try {
      openDocument(await bridge.openWorkspaceAt(rootPath));
    } catch (reason) {
      setHomeError(`Could not open ${rootPath}: ${errorMessage(reason)}`);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (!restoring) return;
    const rootPath = lastWorkspace();
    if (!rootPath) {
      setRestoring(false);
      return;
    }
    let disposed = false;
    bridge
      .openWorkspaceAt(rootPath)
      .then((opened) => {
        if (!disposed) openDocument(opened);
      })
      .catch((reason) => {
        if (disposed) return;
        // Do not try again on the next reload; the entry stays in the recent
        // list so the user can retry or forget it.
        clearLastWorkspace();
        setHomeError(`Could not reopen ${rootPath}: ${errorMessage(reason)}`);
      })
      .finally(() => {
        if (!disposed) setRestoring(false);
      });
    return () => {
      disposed = true;
    };
    // Runs once, for the workspace that was open when the window loaded.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const stepFileName = () => {
    const step = selectedStep ? `${steps.indexOf(selectedStep) + 1}-${selectedStep.name}` : "wafer";
    return `${document?.project.name ?? "process-studio"}-${step}`;
  };

  const exportMesh = async () => {
    if (!document || !branch) return;
    try {
      const path = await bridge.exportMesh(document.root, { branchId: branch.id, stepId: selectedStepId }, stepFileName());
      if (path) setEvents((current) => [...current, { kind: "log", message: `Exported the 3D surfaces to ${path}` }]);
    } catch (reason) {
      setEvents((current) => [...current, { kind: "log", message: `Mesh export failed: ${errorMessage(reason)}` }]);
      setShowLog(true);
    }
  };

  const saveImage = async (kind: "3d" | "section" | "top", image: string) => {
    try {
      const path = await bridge.saveImage(`${stepFileName()}-${kind}`, image);
      if (path) setEvents((current) => [...current, { kind: "log", message: `Saved the picture to ${path}` }]);
    } catch (reason) {
      setEvents((current) => [...current, { kind: "log", message: `Saving the picture failed: ${errorMessage(reason)}` }]);
      setShowLog(true);
    }
  };

  const removeStepById = (stepId: string) => {
    if (!document) return;
    const target = steps.find((step) => step.id === stepId);
    if (!target) return;
    if (!window.confirm(`Delete ${target.name} and its stored result?`)) return;
    const index = steps.indexOf(target);
    const next = removeStep(document, stepId);
    setDocument(next);
    if (selectedStepId === stepId) {
      setSelectedStepId(getSteps(next)[Math.max(0, index - 1)]?.id ?? "");
    }
  };

  const refreshViews = useCallback(async () => {
    // A run holds the project database open; read the views once it is done.
    if (!root || !branchId || busy) return;
    const token = (viewToken.current += 1);
    const target = `${branchId}:${selectedStepId}`;
    if (shownTarget.current !== target) {
      // Never leave one step's result on screen while another one loads.
      shownTarget.current = target;
      setSurfaces(undefined);
      setSection(undefined);
      setTopView(undefined);
    }
    if (selectedStatus === "dirty") {
      // Nothing was ever stored for this step, so there is nothing to fetch.
      setViewLoading(false);
      setViewError("This step has not run yet. Run the flow to see it.");
      return;
    }
    setViewError(undefined);
    const request = { branchId, stepId: selectedStepId, interpolation };
    const remember = <T extends SurfaceDocument | SectionDocument | TopViewDocument>(key: string, value: T) => {
      const cache = viewCache.current;
      cache.delete(key);
      cache.set(key, value);
      // A handful of views is plenty; a 3D payload can be tens of megabytes.
      while (cache.size > 8) cache.delete(cache.keys().next().value as string);
      return value;
    };
    const cached = <T,>(key: string): T | undefined => viewCache.current.get(key) as T | undefined;
    try {
      // A stale step still has the result of the last run, so it is shown,
      // labelled out of date, instead of being refused.
      if (mode === "surfaces") {
        const key = `surfaces:${target}:${interpolation}`;
        const hit = cached<SurfaceDocument>(key);
        if (hit) {
          setSurfaces(hit);
          setViewLoading(false);
          return;
        }
        setViewLoading(true);
        const next = await bridge.getSurfaces(root, request);
        if (token !== viewToken.current) return;
        setSurfaces(remember(key, next));
      } else if (mode === "section") {
        if (sectionAxis === "line" && !sectionLine) {
          setViewLoading(false);
          setViewError("Draw the AA–BB line on the top view first.");
          return;
        }
        const position = sectionAxis === "line" ? undefined : sectionPosition.current ?? undefined;
        const line = sectionAxis === "line" && sectionLine ? sectionLine : undefined;
        const key = `section:${target}:${interpolation}:${sectionAxis}:${position ?? "mid"}:${line ? line.start.join(",") + ">" + line.end.join(",") : ""}:${smoothSection ? "smooth" : "exact"}`;
        const hit = cached<SectionDocument>(key);
        const next = hit ?? (await (async () => {
          setViewLoading(true);
          return remember(
            key,
            await bridge.getSection(root, { ...request, axis: sectionAxis, position, line, smooth: smoothSection }),
          );
        })());
        if (token !== viewToken.current) return;
        setSection(next);
        if (sectionAxis !== "line") {
          sectionPosition.current = next.position;
          if (next.index !== sectionIndex) setSectionIndex(next.index);
        }
      } else {
        const key = `top:${target}`;
        const hit = cached<TopViewDocument>(key);
        if (hit) {
          setTopView(hit);
          setViewLoading(false);
          return;
        }
        setViewLoading(true);
        const next = await bridge.getTopView(root, request);
        if (token !== viewToken.current) return;
        setTopView(remember(key, next));
      }
    } catch (reason) {
      if (token !== viewToken.current) return;
      setViewError(errorMessage(reason));
    } finally {
      if (token === viewToken.current) setViewLoading(false);
    }
    // Edits do not change what a run stored, so the document identity is not a
    // trigger here: a finished run flips `busy`, which is.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    root,
    branchId,
    busy,
    selectedStepId,
    selectedStatus,
    mode,
    interpolation,
    sectionAxis,
    sectionIndex,
    sectionLine,
    smoothSection,
  ]);

  useEffect(() => {
    void refreshViews();
  }, [refreshViews]);

  const runFlow = async (throughStepId?: string) => {
    if (!document || !branch || busy) return;
    setBusy(true);
    runningStepId.current = undefined;
    // Whatever the run stores replaces what the views have seen.
    viewCache.current.clear();
    const requestId = `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    runRequestId.current = requestId;
    let working: WorkspaceDocument = document;
    if (throughStepId) working = markStep(working, throughStepId, "running");
    setDocumentState(working);
    try {
      // Flush pending edits first: the worker runs what is stored, not what is on screen.
      const saved = await bridge.saveDocument(document);
      skipNextAutosave.current = true;
      setSaveState("saved");
      const result = await bridge.runFlow(
        saved.root,
        { branchId: branch.id, throughStepId },
        requestId,
      );
      skipNextAutosave.current = true;
      setDocumentState(setStepStatuses(saved, result.branchId, result.stepStatuses));
      setEvents((current) => [
        ...current,
        {
          kind: "log",
          message: `Ran ${result.executedStepIds.length} step(s), reused ${result.cachedStepIds.length}.`,
        },
      ]);
    } catch (reason) {
      const message = errorMessage(reason);
      const stopped = /cancelled|Stopped before/i.test(message);
      const failed = stopped ? undefined : runningStepId.current;
      // Whatever finished before the failure is stored; the worker's own
      // statuses say which steps those are.
      try {
        const refreshed = await bridge.saveDocument(document);
        skipNextAutosave.current = true;
        setDocumentState(failed ? markStep(refreshed, failed, "failed") : refreshed);
      } catch {
        if (failed) {
          skipNextAutosave.current = true;
          setDocumentState((current) => (current ? markStep(current, failed, "failed") : current));
        }
      }
      if (failed) setSelectedStepId(failed);
      setEvents((current) => [
        ...current,
        { kind: "log", message: stopped ? `Run stopped: ${message}` : `Run failed: ${message}` },
      ]);
      if (!stopped) setShowLog(true);
    } finally {
      runRequestId.current = undefined;
      setBusy(false);
    }
  };

  const openSketchEditor = async (sketchId: string | null) => {
    if (!document || !branch || !selectedStep) return;
    const existing = sketchId ? document.sketches.find((item) => item.id === sketchId) : undefined;
    const sketch: QuickSketch = existing ?? {
      id: newId("sketch"),
      name: "New sketch",
      shapes: [],
    };
    setSketchBackdrop(undefined);
    setSketchEditor({ sketch, isNew: !existing });
    // The wafer this step's mask lands on is the result of the step before
    // it; when that has not been run there is simply no backdrop.
    const index = steps.indexOf(selectedStep);
    const previous = index > 0 ? steps[index - 1].id : "";
    if (previous && (statuses[previous] ?? "dirty") === "dirty") return;
    try {
      setSketchBackdrop(await bridge.getTopView(document.root, { branchId: branch.id, stepId: previous }));
    } catch {
      setSketchBackdrop(undefined);
    }
  };

  const saveSketch = async (sketch: QuickSketch) => {
    if (!document || !selectedStep || !sketchEditor) return;
    setBusy(true);
    try {
      // Flush edits first so the returned document does not drop them.
      const flushed = await bridge.saveDocument(document);
      const saved = await bridge.saveSketch(flushed.root, sketch);
      skipNextAutosave.current = true;
      setDocumentState(
        sketchEditor.isNew
          ? updateStepParameters(saved, selectedStep.id, { sketch_id: sketch.id })
          : saved,
      );
      if (sketchEditor.isNew) setSaveState("unsaved");
      setSketchEditor(null);
      setEvents((current) => [
        ...current,
        { kind: "log", message: `Saved sketch ${sketch.name} (${sketch.shapes.length} shape(s)).` },
      ]);
    } catch (reason) {
      setEvents((current) => [
        ...current,
        { kind: "log", message: `Sketch save failed: ${errorMessage(reason)}` },
      ]);
      setShowLog(true);
    } finally {
      setBusy(false);
    }
  };

  const stopRun = () => {
    const requestId = runRequestId.current;
    if (!requestId) return;
    void bridge.cancel(requestId).catch((reason) =>
      setEvents((current) => [
        ...current,
        { kind: "log", message: `Could not stop the run: ${errorMessage(reason)}` },
      ]),
    );
  };

  const handleImportGds = async () => {
    if (!document || busy) return;
    setBusy(true);
    try {
      const result = await bridge.importGds(document.root);
      if (result) {
        skipNextAutosave.current = true;
        setDocumentState(result.document);
        setEvents((current) => [
          ...current,
          {
            kind: "log",
            message: `Imported ${result.fileName}: ${result.layers.length} layer/datatype pair(s).`,
          },
        ]);
      }
    } catch (reason) {
      setEvents((current) => [
        ...current,
        { kind: "log", message: `GDS import failed: ${errorMessage(reason)}` },
      ]);
      setShowLog(true);
    } finally {
      setBusy(false);
    }
  };

  const planGrid = useCallback(
    (targetSpacingNm: number, bounds: WindowBounds, xyNm: number | null) => {
      if (!document) return Promise.reject(new Error("No workspace is open."));
      return bridge.planGrid(document.root, targetSpacingNm, bounds, xyNm);
    },
    [document?.root],
  );

  const handleApplyGrid = async (targetSpacingNm: number, bounds: WindowBounds, xyNm: number | null) => {
    if (!document || busy) return;
    setBusy(true);
    try {
      const saved = await bridge.saveDocument(document);
      const updated = await bridge.setGrid(saved.root, targetSpacingNm, bounds, xyNm);
      viewCache.current.clear();
      skipNextAutosave.current = true;
      setDocumentState(updated);
      setSaveState("saved");
      setShowGrid(false);
      const grid = updated.project.grid;
      setEvents((current) => [
        ...current,
        {
          kind: "log",
          message: `Window ${grid.xMin}..${grid.xMax} × ${grid.yMin}..${grid.yMax} × ${grid.zMin}..${grid.zMax} µm, grid ${grid.nx}×${grid.ny}×${grid.nz} (${(
            grid.spacingUm * 1000
          ).toFixed(3)} nm). Stored results were discarded.`,
        },
      ]);
    } catch (reason) {
      setEvents((current) => [
        ...current,
        { kind: "log", message: `Grid change failed: ${errorMessage(reason)}` },
      ]);
      setShowLog(true);
    } finally {
      setBusy(false);
    }
  };

  const usedMaterialNames = useMemo(() => {
    const names = new Set<string>();
    document?.recipes.forEach((recipe) => {
      if (recipe.outputMaterial) names.add(recipe.outputMaterial);
      Object.keys(recipe.materialResponses).forEach((name) => names.add(name));
    });
    document?.branches.forEach((item) => item.steps.forEach((step) => {
      if (step.outputMaterial) names.add(step.outputMaterial);
      Object.keys(step.materialResponses).forEach((name) => names.add(name));
    }));
    return names;
  }, [document?.recipes, document?.branches]);

  if (restoring) {
    return (
      <div className="restore-splash">
        <LoaderCircle className="spin" size={16} />
        <span>Reopening the last workspace…</span>
      </div>
    );
  }

  if (!document || !branch) {
    return (
      <ProjectHome
        runtime={bridge.runtime}
        busy={busy}
        error={homeError}
        kernels={capabilities?.kernels ?? []}
        defaultKernel={capabilities?.defaultKernel ?? "levelset"}
        recent={recent}
        onCreate={handleCreate}
        onOpen={handleOpen}
        onOpenRecent={(rootPath) => void handleOpenRecent(rootPath)}
        onForgetRecent={(rootPath) => setRecent(forgetWorkspace(rootPath))}
      />
    );
  }

  const progress = events
    .filter((event) => event.kind === "progress" && event.total)
    .at(-1);

  // A stale step still shows what the last run stored, labelled as out of date.
  const viewNotice =
    selectedStatus === "stale"
      ? "Out of date: this is what the last run stored for this step."
      : undefined;

  return (
    <div
      className="app-shell"
      // The panel widths live on the shell so the log drawer, which sits
      // outside the grid, lines up with the same edges.
      style={
        panelWidths
          ? ({
              "--steps-width": `${panelWidths.steps}px`,
              "--inspector-width": `${panelWidths.inspector}px`,
            } as React.CSSProperties)
          : undefined
      }
    >
      <header className="topbar">
        <button
          type="button"
          className="icon-button back-button"
          title="Close this workspace"
          onClick={() => setDocumentState(null)}
        >
          <ArrowLeft size={17} />
        </button>
        <div className="brand-compact">
          <span className="brand-glyph">PS</span>
          <span>Process Studio</span>
        </div>
        <div className="project-crumb">
          <span className="divider-dot">/</span>
          <strong>{document.project.name}</strong>
          <span className="project-path">{document.root}</span>
        </div>
        <div className="variant-controls">
          <select
            aria-label="Active branch"
            value={branch.id}
            onChange={(event) => {
              const next = setActiveBranch(document, event.target.value);
              setDocument(next);
              setSelectedStepId(getSteps(next)[0]?.id ?? "");
            }}
          >
            {document.branches.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
          <button
            type="button"
            title={
              projectKernel && projectKernel.spacingRole !== "grid"
                ? `${projectKernel.name}: geometry resolution`
                : "Simulation grid"
            }
            onClick={() => setShowGrid(true)}
          >
            <Grid3x3 size={13} />
            {(
              (projectKernel && projectKernel.spacingRole !== "grid"
                ? document.project.resolutionUm ?? document.project.grid.spacingUm
                : document.project.grid.spacingUm) * 1000
            ).toFixed(0)}{" "}
            nm
          </button>
          {projectKernel && (
            <span
              className="kernel-badge"
              title={`${projectKernel.summary} Fixed when the project was created.`}
            >
              <Cpu size={13} />
              {projectKernel.name}
            </span>
          )}
          <button type="button" title="Import a GDSII layout" onClick={handleImportGds}>
            <MapIcon size={13} />
            GDS
          </button>
        </div>
        <div className="topbar-spacer" />
        <button type="button" className="log-button" onClick={() => setShowMaterials(true)}>
          <Palette size={15} />
          Materials
        </button>
        <button type="button" className="log-button" onClick={() => setShowRecipes(true)}>
          <FileSpreadsheet size={15} />
          Recipes
        </button>
        <button type="button" className="log-button" onClick={() => setShowTools(true)}>
          <Wrench size={14} />
          Tools
        </button>
        <div className={`save-indicator save-${saveState}`}>
          {saveState === "saving" ? (
            <LoaderCircle className="spin" size={13} />
          ) : saveState === "error" ? (
            <CircleAlert size={13} />
          ) : (
            <Save size={13} />
          )}
          {saveState === "saving"
            ? "Saving"
            : saveState === "unsaved"
              ? "Unsaved"
              : saveState === "error"
                ? "Save failed"
                : "Saved"}
        </div>
        <button type="button" className="log-button" onClick={() => setShowLog((value) => !value)}>
          <TerminalSquare size={15} />
          Log{events.length > 0 && <span>{events.length}</span>}
        </button>
        {busy && runRequestId.current ? (
          <button
            type="button"
            className="primary-button run-button stop-button"
            title="Stop after the step that is running now; finished steps stay stored"
            onClick={stopRun}
          >
            <Square size={13} fill="currentColor" />
            Stop
          </button>
        ) : (
          <button
            type="button"
            className="primary-button run-button"
            disabled={busy || !hasDirtySteps(document)}
            onClick={() => runFlow()}
          >
            {busy ? (
              <LoaderCircle className="spin" size={15} />
            ) : hasDirtySteps(document) ? (
              <Play size={15} fill="currentColor" />
            ) : (
              <CheckCircle2 size={15} />
            )}
            {busy ? "Working" : hasDirtySteps(document) ? "Run stale" : "Up to date"}
          </button>
        )}
      </header>

      {busy && progress?.total ? (
        <div className="global-progress">
          <div style={{ width: `${((progress.completed ?? 0) / progress.total) * 100}%` }} />
        </div>
      ) : null}

      <div className="workspace-grid">
        <PanelResizer
          side="left"
          width={(panelWidths ?? DEFAULT_PANELS).steps}
          onResize={(width) => resizePanel("steps", width)}
          onReset={resetPanels}
        />
        <PanelResizer
          side="right"
          width={(panelWidths ?? DEFAULT_PANELS).inspector}
          onResize={(width) => resizePanel("inspector", width)}
          onReset={resetPanels}
        />
        <StepList
          steps={steps}
          statuses={statuses}
          accentFor={(step) => stepAccentColor(document, step)}
          selectedStepId={selectedStepId}
          busy={busy}
          onSelect={setSelectedStepId}
          onAdd={(processType) => {
            const { document: next, step } = addStep(document, processType, selectedStepId);
            setDocument(next);
            setSelectedStepId(step.id);
          }}
          onToggle={(stepId) => setDocument(toggleStep(document, stepId))}
          onReorder={(activeId, overId) => setDocument(reorderSteps(document, activeId, overId))}
          onRunToHere={(stepId) => void runFlow(stepId)}
          onDuplicate={(stepId) => {
            const result = duplicateStep(document, stepId);
            if (!result) return;
            setDocument(result.document);
            setSelectedStepId(result.step.id);
          }}
          onMove={(stepId, direction) => setDocument(moveStep(document, stepId, direction))}
          onRemove={removeStepById}
        />
        <Viewport
          mode={mode}
          onModeChange={setMode}
          title={
            selectedStep
              ? `${selectedStep.name} · after step ${steps.indexOf(selectedStep) + 1}`
              : "Initial wafer"
          }
          loading={viewLoading}
          error={viewError}
          notice={viewNotice}
          surfacesSupported={projectKernel?.surfaces ?? capabilities?.rendering.surfaces ?? false}
          maximumInterpolation={capabilities?.rendering.maximumInterpolation ?? 4}
          interpolation={interpolation}
          onInterpolationChange={setInterpolation}
          sectionAxis={sectionAxis}
          onSectionAxisChange={(axis) => {
            setSectionAxis(axis);
            setSectionIndex(null);
            sectionPosition.current = null;
          }}
          sectionLines={sectionLines}
          sectionLine={sectionLine}
          windowBounds={{
            xMin: document.project.grid.xMin,
            xMax: document.project.grid.xMax,
            yMin: document.project.grid.yMin,
            yMax: document.project.grid.yMax,
          }}
          onSelectLine={(lineId) => {
            setActiveLineId(lineId);
            if (lineId) setSectionAxis("line");
            else if (sectionAxis === "line") setSectionAxis("y");
          }}
          onSaveLine={(line) => {
            const isNew = !line.id;
            const saved: SectionLine = isNew
              ? { ...line, id: newId("line"), name: line.name || nextSectionLineName(document) }
              : line;
            setDocument(upsertSectionLine(document, saved));
            setActiveLineId(saved.id);
            setSectionAxis("line");
            // A freshly drawn line is what the user wants to look at.
            if (isNew) setMode("section");
          }}
          onRemoveLine={(lineId) => {
            setDocument(removeSectionLine(document, lineId));
            if (activeLineId === lineId) {
              setActiveLineId(null);
              if (sectionAxis === "line") setSectionAxis("y");
            }
          }}
          smoothSection={smoothSection}
          onSmoothSectionChange={setSmoothSection}
          onExportMesh={() => void exportMesh()}
          onSaveImage={(kind, image) => void saveImage(kind, image)}
          sectionIndex={sectionIndex ?? 0}
          onSectionIndexChange={(index) => {
            sectionPosition.current = section?.positions?.[index] ?? null;
            setSectionIndex(index);
          }}
          materials={document.materials}
          hiddenMaterials={hiddenMaterials}
          onToggleMaterial={(material) =>
            setHiddenMaterials((current) =>
              current.includes(material)
                ? current.filter((name) => name !== material)
                : [...current, material],
            )
          }
          surfaces={surfaces}
          section={section}
          topView={topView}
        />
        <Inspector
          step={selectedStep}
          recipes={document.recipes}
          tools={document.tools}
          onManageTools={() => setShowTools(true)}
          materials={document.materials}
          status={selectedStep ? stepStatus(document, selectedStep.id) : "dirty"}
          sketches={document.sketches}
          gdsPath={document.project.gdsPath}
          kernel={projectKernel}
          solverOrders={capabilities?.numerics.solverOrders ?? [1, 2]}
          busy={busy}
          onRename={(name) => selectedStep && setDocument(renameStep(document, selectedStep.id, name))}
          onProcessTypeChange={(processType) =>
            selectedStep && setDocument(setStepProcessType(document, selectedStep.id, processType))
          }
          onDefinitionChange={(patch) =>
            selectedStep && setDocument(updateStep(document, selectedStep.id, patch))
          }
          onParameter={(patch: Record<string, ParameterValue>) =>
            selectedStep && setDocument(updateStepParameters(document, selectedStep.id, patch))
          }
          onLoadRecipe={(recipe) =>
            selectedStep && setDocument(loadRecipeIntoStep(document, selectedStep.id, recipe))
          }
          onSaveRecipe={(name) => {
            if (!selectedStep) return;
            const recipe = recipeFromStep(selectedStep, name);
            setDocument(upsertRecipe(document, recipe));
            setEvents((current) => [
              ...current,
              { kind: "log", message: `Saved ${recipe.name} to the Recipe Library.` },
            ]);
          }}
          onMaskChange={(patch) =>
            selectedStep && setDocument(updateStep(document, selectedStep.id, patch))
          }
          onEditSketch={(sketchId) => void openSketchEditor(sketchId)}
          onRunToHere={() => selectedStep && void runFlow(selectedStep.id)}
          onRemove={() => selectedStep && removeStepById(selectedStep.id)}
        />
      </div>

      {showTools && (
        <ToolEditor
          tools={document.tools}
          usage={toolUsage(document)}
          onSave={(tool) => setDocument(upsertTool(document, tool))}
          onDelete={(toolId) => setDocument(removeTool(document, toolId))}
          onClose={() => setShowTools(false)}
        />
      )}

      {showMaterials && (
        <MaterialEditor
          materials={document.materials}
          usedNames={usedMaterialNames}
          onSave={(material) => setDocument(upsertMaterial(document, material))}
          onDelete={(materialId) => setDocument(removeMaterial(document, materialId))}
          onClose={() => setShowMaterials(false)}
        />
      )}

      {showRecipes && (
        <RecipeEditor
          recipes={document.recipes}
          materials={document.materials}
          tools={document.tools}
          busy={busy}
          onSave={(recipe) => setDocument(upsertRecipe(document, recipe))}
          onDelete={(recipeId) => setDocument(removeRecipe(document, recipeId))}
          onExport={async () => {
            try {
              const path = await bridge.exportRecipes(document.root, document.project.name);
              if (path) {
                setEvents((current) => [
                  ...current,
                  { kind: "log", message: `Exported the recipe library to ${path}` },
                ]);
              }
            } catch (reason) {
              setEvents((current) => [
                ...current,
                { kind: "log", message: `Export failed: ${errorMessage(reason)}` },
              ]);
              setShowLog(true);
            }
          }}
          onImport={async () => {
            try {
              const imported = await bridge.importRecipes(document.root);
              if (imported) {
                skipNextAutosave.current = true;
                setDocumentState(imported);
              }
            } catch (reason) {
              setEvents((current) => [
                ...current,
                { kind: "log", message: `Import failed: ${errorMessage(reason)}` },
              ]);
              setShowLog(true);
            }
          }}
          onClose={() => setShowRecipes(false)}
        />
      )}

      {sketchEditor && selectedStep && (
        <SketchEditor
          sketch={sketchEditor.sketch}
          isNew={sketchEditor.isNew}
          grid={document.project.grid}
          keep={selectedStep.keep}
          backdrop={sketchBackdrop}
          busy={busy}
          onPreview={(sketch) => bridge.previewMask(document.root, sketch, selectedStep.keep)}
          onSave={(sketch) => void saveSketch(sketch)}
          onClose={() => setSketchEditor(null)}
        />
      )}

      {showGrid && (
        <GridEditor
          grid={document.project.grid}
          kernel={projectKernel}
          resolutionUm={document.project.resolutionUm}
          resolutionXyUm={document.project.resolutionXyUm}
          presetsNm={
            projectKernel?.spacingPresetsNm ??
            capabilities?.numerics.spacingPresetsNm ?? [25, 12.5, 6.25]
          }
          maximumNodes={
            projectKernel
              ? projectKernel.maximumNodes
              : capabilities?.numerics.maximumNodes ?? 20_000_000
          }
          busy={busy}
          onPlan={planGrid}
          onApply={handleApplyGrid}
          onClose={() => setShowGrid(false)}
        />
      )}

      {showLog && (
        <section className="log-drawer">
          <div className="log-heading">
            <span>Worker activity</span>
            <span className="log-heading-actions">
              <button type="button" onClick={() => setEvents([])}>
                Clear
              </button>
              <button
                type="button"
                className="log-close"
                aria-label="Close the log"
                title="Close"
                onClick={() => setShowLog(false)}
              >
                <X size={13} />
              </button>
            </span>
          </div>
          <div className="log-lines">
            {events.length === 0 ? (
              <p>No worker activity yet.</p>
            ) : (
              events.map((event, index) => (
                <div key={`${index}-${event.message}`}>
                  <span>{event.kind === "progress" ? "RUN" : "LOG"}</span>
                  <code>{event.message}</code>
                </div>
              ))
            )}
          </div>
        </section>
      )}
    </div>
  );
}
