import {
  ArrowLeft,
  CircleAlert,
  CheckCircle2,
  Cpu,
  Grid3x3,
  Layers,
  LoaderCircle,
  Play,
  Save,
  ScrollText,
  Square,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { bridge } from "./bridge";
import { CliPanel } from "./components/CliPanel";
import { GridEditor } from "./components/GridEditor";
import { Inspector, type LoopSummary } from "./components/Inspector";
import { MaterialEditor } from "./components/MaterialEditor";
import { ToolEditor } from "./components/ToolEditor";
import { PanelResizer } from "./components/PanelResizer";
import { ProjectHome } from "./components/ProjectHome";
import { RecipeEditor } from "./components/RecipeEditor";
import { SketchEditor } from "./components/SketchEditor";
import { MenuBar, type Menu } from "./components/MenuBar";
import { PROCESS_LABELS, StepList, summarizeStatuses, type LoopActions, type SelectModifiers } from "./components/StepList";
import { Viewport, type MaterialLook, type ViewMode } from "./components/Viewport";
import {
  addStep,
  dissolveLoop,
  duplicateStep,
  duplicateSteps,
  insertSteps,
  loopObstacle,
  loopSteps,
  makeLoop,
  moveSteps,
  renameLoop,
  setLoopRepeat,
  orderedSelection,
  removeSteps,
  setStepsEnabled,
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
  CliResult,
  Fidelity,
  FlowExportFormat,
  LibraryKind,
  ProcessType,
  Triangulation,
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
  forgetWorkspace,
  recentWorkspaces,
  rememberWorkspace,
  type RecentWorkspace,
} from "./domain/recent";

import {
  clipFrom,
  clipToPaste,
  pasteClip,
  rememberClip,
} from "./domain/clipboard";

import {
  clampPanelShares,
  DEFAULT_PANELS,
  loadPanelShares,
  percent,
  savePanelShares,
  type PanelShares,
} from "./domain/layout";

import { libraryOf, sameLibrary, withLibrary, type Library } from "./domain/library";
import { tabName, type TabHandle } from "./domain/tabs";

type SaveState = "saved" | "saving" | "unsaved" | "error";

const AUTOSAVE_DELAY_MS = 600;

function errorMessage(reason: unknown) {
  return reason instanceof Error ? reason.message : String(reason);
}

export type WorkspaceProps = {
  /** The workspace to open on mount; "" starts on the home page. */
  initialRoot: string;
  /** Whether this tab is the one on screen. A hidden tab fetches no views. */
  active: boolean;
  /** Told whenever what this tab holds changes, so the tab strip follows. */
  onChanged: (handle: TabHandle) => void;
  /** Open a workspace in a tab of its own (a recent one, or a new project). */
  onOpenInNewTab: (root: string) => void;
  /** Close this tab. */
  onClose: () => void;
  /**
   * The library as another tab last had it, with a count that rises on
   * every report. Materials, tools and recipes are the user's rather than
   * the project's, so every tab shows the same ones.
   */
  library: { count: number; value: Library } | null;
  /** Say what this tab's library holds, so the other tabs follow it. */
  onLibraryChanged: (library: Library) => void;
  /**
   * Ask to hold this workspace. False means another tab already does, and
   * the shell has brought that tab forward instead.
   */
  claimRoot: (root: string) => boolean;
};

export function Workspace({
  initialRoot,
  active,
  onChanged,
  onOpenInNewTab,
  onClose,
  library,
  onLibraryChanged,
  claimRoot,
}: WorkspaceProps) {
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
  const [restoring, setRestoring] = useState(() => initialRoot !== "");
  // A tab opens its workspace the first time it is looked at, not when the
  // window starts: reopening five tabs would otherwise be five workspaces
  // through one worker before anything could be done with the first.
  const [everShown, setEverShown] = useState(active);
  const [events, setEvents] = useState<WorkerEvent[]>([]);
  const [showLog, setShowLog] = useState(false);
  const [showMaterials, setShowMaterials] = useState(false);
  const [showRecipes, setShowRecipes] = useState(false);
  const [showTools, setShowTools] = useState(false);
  const [showGrid, setShowGrid] = useState(false);
  const [showCli, setShowCli] = useState(false);
  const [showShortcuts, setShowShortcuts] = useState(false);
  // A short message with an optional action, for menu commands that answer something.
  const [notice, setNotice] = useState<{
    title: string;
    text: string;
    action?: { label: string; run: () => void };
    // A second button, for when the notice offers a thing to do as
    // well as a thing to read: installing an update, and its notes.
    primary?: { label: string; run: () => void };
  } | null>(null);
  // Every selected step (the focused `selectedStepId` included) and the
  // anchor a Shift-click extends from, the way a file list selects.
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const anchorId = useRef("");
  // The loop whose header is selected: its steps are the selection, the
  // inspector shows its settings and the view the wafer after its last step.
  const [selectedLoopId, setSelectedLoopId] = useState("");
  const [loopDialog, setLoopDialog] = useState(false);
  // Steps copied with Ctrl+C, pasted after the focused step with Ctrl+V --
  // from any tab or window, so what is on the clipboard is read when this
  // tab comes forward rather than remembered here (see domain/clipboard).
  const [clipboardSize, setClipboardSize] = useState(0);
  // Undo and redo of document edits: the documents as they were.
  const undoStack = useRef<WorkspaceDocument[]>([]);
  const redoStack = useRef<WorkspaceDocument[]>([]);
  const [historySize, setHistorySize] = useState({ undo: 0, redo: 0 });
  // The sketch being drawn: an existing one by id, or a fresh one for this step.
  const [sketchEditor, setSketchEditor] = useState<{ sketch: QuickSketch; isNew: boolean } | null>(null);
  const [sketchBackdrop, setSketchBackdrop] = useState<TopViewDocument>();
  const [mode, setMode] = useState<ViewMode>("surfaces");
  const [interpolation, setInterpolation] = useState(1);
  // Whether the top view draws the steps inside a material. On by default:
  // without them a trench in silicon is the same colour as the silicon
  // around it, and the picture says nothing about where it is.
  const [topSteps, setTopSteps] = useState(true);
  // Which triangulator builds the 3D mesh. Ear clipping is the default;
  // the other is there to compare against, and costs a rebuild.
  const [triangulation, setTriangulation] = useState<Triangulation>("ears");
  // Build the faces that lie against another material. They are invisible
  // while both materials are shown -- two copies of one face fighting for
  // the same pixels -- and are most of a stack's mesh, so they are left
  // out until something is hidden. This forces them on regardless.
  const [alwaysBuried, setAlwaysBuried] = useState(false);
  // Prepare every step's buried faces when a flow finishes, rather than
  // each step's when its 3D view is opened. Off: it is the heavy mesh,
  // and a flow of twenty-odd steps is half a minute of it.
  const [prepareBuried, setPrepareBuried] = useState(false);
  const [sectionAxis, setSectionAxis] = useState<SectionAxis>("y");
  // Which of the project's saved AA–BB lines the section follows.
  const [activeLineId, setActiveLineId] = useState<string | null>(null);
  const [sectionIndex, setSectionIndex] = useState<number | null>(null);
  // Sampled films drawn as the surface they sample, or as the stored slabs.
  // The share of the window the user dragged each side panel to; null means
  // the stylesheet's defaults.
  const [panelShares, setPanelShares] = useState<PanelShares | null>(loadPanelShares);
  const resizePanel = (side: "steps" | "inspector", share: number) => {
    const next = clampPanelShares({ ...(panelShares ?? DEFAULT_PANELS), [side]: share });
    setPanelShares(next);
    savePanelShares(next);
  };
  const resetPanels = () => {
    setPanelShares(null);
    savePanelShares(null);
  };
  const [surfaces, setSurfaces] = useState<SurfaceDocument>();
  const [section, setSection] = useState<SectionDocument>();
  const [topView, setTopView] = useState<TopViewDocument>();
  const [viewLoading, setViewLoading] = useState(false);
  const [viewError, setViewError] = useState<string>();

  const [hiddenMaterials, setHiddenMaterials] = useState<string[]>([]);
  // Colours and opacities for the 3D view only; the library is not touched.
  const [materialLooks, setMaterialLooks] = useState<Record<string, MaterialLook>>({});

  const skipNextAutosave = useRef(false);
  const runningStepId = useRef<string | undefined>(undefined);
  // The selected step as the worker-event listener sees it; the listener is
  // attached once and must not close over a stale value.
  const selectedStepRef = useRef("");
  // The document and busy flag as they are now, for the console: it runs
  // several commands from one click, and each must see what the previous
  // one left, not what the click closed over.
  const documentRef = useRef<WorkspaceDocument | null>(null);
  documentRef.current = document;
  const busyRef = useRef(false);
  busyRef.current = busy;
  // Bumped when a step the view is showing finishes inside a run, so the
  // view is fetched again although nothing else about the request changed.
  const [viewNonce, setViewNonce] = useState(0);
  // The id of the run in flight, so the Stop button can name it.
  const runRequestId = useRef<string | undefined>(undefined);
  // Every tab is mounted, so the worker's events reach all of them and so
  // does every key press. An event belongs to the tab that asked for it;
  // one with no request behind it (a general log) belongs to the tab on
  // screen. Held in refs because the listeners are attached once.
  const onScreen = useRef(active);
  onScreen.current = active;
  // A stop has been asked for and the run has not ended yet. The worker
  // stops at its next check inside the running step, which is quick but
  // not instant, so the button says "Stopping…" rather than looking dead.
  const [stopping, setStopping] = useState(false);
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

  /** Drop every view fetched of this step; its stored result has changed. */
  const forgetViews = (stepId: string) => {
    for (const key of [...viewCache.current.keys()]) {
      if (key.includes(`:${stepId}:`) || key.endsWith(`:${stepId}`)) viewCache.current.delete(key);
    }
  };

  /** A step's result was just stored by a run that is still going. */
  const stepFinished = (stepId: string) => {
    forgetViews(stepId);
    skipNextAutosave.current = true;
    setDocumentState((current) => (current ? markStep(current, stepId, "clean") : current));
    if (selectedStepRef.current === stepId) setViewNonce((nonce) => nonce + 1);
  };

  const setDocument = (next: WorkspaceDocument, persist = true, remember = true) => {
    if (!persist) skipNextAutosave.current = true;
    if (persist && remember && document && document.root === next.root) {
      undoStack.current = [...undoStack.current.slice(-29), document];
      redoStack.current = [];
      setHistorySize({ undo: undoStack.current.length, redo: 0 });
    }
    setDocumentState(next);
    if (persist) setSaveState("unsaved");
  };

  const undo = () => {
    const previous = undoStack.current.pop();
    if (!previous || !document) return;
    redoStack.current.push(document);
    setHistorySize({ undo: undoStack.current.length, redo: redoStack.current.length });
    setDocument(previous, true, false);
  };

  const redo = () => {
    const next = redoStack.current.pop();
    if (!next || !document) return;
    undoStack.current.push(document);
    setHistorySize({ undo: undoStack.current.length, redo: redoStack.current.length });
    setDocument(next, true, false);
  };

  useEffect(() => {
    bridge
      .describe()
      .then(setCapabilities)
      .catch((reason) => setHomeError(errorMessage(reason)));
  }, []);

  // What the tab strip shows, and what stops the same workspace being
  // opened in two tabs -- two tabs autosaving one project would each
  // overwrite the other.
  //
  // A tab that has not been looked at yet holds nothing, but it is going
  // to hold ``initialRoot``, and it has to say so: reported as empty, the
  // same workspace could be opened again in another tab and the two would
  // fight over the file.
  useEffect(() => {
    const waiting = restoring && initialRoot !== "";
    onChanged({
      root: document?.root ?? (waiting ? initialRoot : ""),
      name: document?.project.name ?? (waiting ? tabName(initialRoot) : "New project"),
      busy,
      unsaved: saveState === "unsaved" || saveState === "saving",
    });
  }, [
    document?.root,
    document?.project.name,
    busy,
    saveState,
    onChanged,
    restoring,
    initialRoot,
  ]);

  useEffect(() => {
    let disposed = false;
    let unsubscribe: (() => void) | undefined;
    bridge
      .subscribeToWorkerEvents((event) => {
        // Not this tab's doing: another tab asked for it, and marking this
        // tab's steps from it would be plainly wrong.
        if (event.requestId ? event.requestId !== runRequestId.current : !onScreen.current) return;
        if (event.kind === "log" && /^(Downloading|Unpacking|Handing over)/.test(event.message)) {
          setInstallProgress(event.message);
        }
        if (event.kind === "progress" && event.stepId) {
          // A run stores each step as it finishes, so the finished ones can
          // be looked at while the rest are still computing: mark them as
          // the run goes, and forget any view fetched of an older result.
          const finished = runningStepId.current;
          const running = event.message.startsWith("Running") ? event.stepId : undefined;
          runningStepId.current = running;
          if (finished && finished !== running) stepFinished(finished);
          if (event.message.startsWith("Cached")) stepFinished(event.stepId);
          if (running) {
            forgetViews(running);
            skipNextAutosave.current = true;
            setDocumentState((current) => (current ? markStep(current, running, "running") : current));
          }
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

  // The library this tab last saw, which is both what it would report and
  // what keeps a report of its own from coming back to it as news.
  const ownLibrary = useRef<Library | null>(null);

  // Materials, tools and recipes are shared, so a change here is a change
  // in every tab. Sending it up first, then taking down what comes back:
  // without the first, the other tabs' autosaves would write their older
  // copy back over this edit, which is the whole reason the shell holds it.
  useEffect(() => {
    if (!document) return;
    const here = libraryOf(document);
    if (ownLibrary.current && sameLibrary(ownLibrary.current, here)) return;
    ownLibrary.current = here;
    onLibraryChanged(here);
  }, [document, onLibraryChanged]);

  useEffect(() => {
    if (!library || !document) return;
    if (sameLibrary(libraryOf(document), library.value)) return;
    ownLibrary.current = library.value;
    setDocumentState(withLibrary(document, library.value));
  }, [library, document]);

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
    setMaterialLooks({});
  };

  // The worker downloads and unpacks the release and starts the updater;
  // its progress comes back as log events. Once it has handed over, the
  // application leaves so the updater can replace it.
  const [installProgress, setInstallProgress] = useState<string>();
  const installUpdate = async (url: string) => {
    setInstallProgress("Starting");
    try {
      await bridge.installUpdate(url);
      setInstallProgress("Restarting");
      await bridge.quitForUpdate();
    } catch (reason) {
      setInstallProgress(undefined);
      throw reason;
    }
  };

  // One workspace, one tab: two tabs autosaving the same project would
  // each overwrite the other's edits. The shell answers whether this tab
  // may hold it, and brings the tab that already does to the front if not.
  const mayOpen = (rootPath: string) => {
    if (claimRoot(rootPath)) return true;
    setHomeError(undefined);
    return false;
  };

  const handleCreate = async (name: string, kernel: string) => {
    setBusy(true);
    setHomeError(undefined);
    try {
      const created = await bridge.createWorkspace(name, kernel);
      if (created && mayOpen(created.root)) openDocument(created);
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
      if (opened && mayOpen(opened.root)) openDocument(opened);
    } catch (reason) {
      setHomeError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const handleOpenRecent = async (rootPath: string) => {
    if (!mayOpen(rootPath)) return;
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
    if (active) setEverShown(true);
  }, [active]);

  useEffect(() => {
    if (!restoring || !everShown) return;
    const rootPath = initialRoot;
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
        // The tab falls back to its home page; the entry stays in the
        // recent list so the user can retry or forget it.
        setHomeError(`Could not reopen ${rootPath}: ${errorMessage(reason)}`);
      })
      .finally(() => {
        if (!disposed) setRestoring(false);
      });
    return () => {
      disposed = true;
    };
  }, [restoring, everShown, initialRoot]);

  const stepFileName = () => {
    const step = selectedStep ? `${steps.indexOf(selectedStep) + 1}-${selectedStep.name}` : "wafer";
    return `${document?.project.name ?? "process-studio"}-${step}`;
  };

  const exportMesh = async () => {
    if (!document || !branch) return;
    try {
      const path = await bridge.exportMesh(
        document.root,
        { branchId: branch.id, stepId: selectedStepId },
        stepFileName(),
      );
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

  // -- selection of several steps -------------------------------------------
  const selectStep = (stepId: string, modifiers: SelectModifiers) => {
    if (!document) return;
    const ids = steps.map((step) => step.id);
    if (modifiers.shift && anchorId.current && ids.includes(anchorId.current)) {
      const a = ids.indexOf(anchorId.current);
      const b = ids.indexOf(stepId);
      setSelectedIds(ids.slice(Math.min(a, b), Math.max(a, b) + 1));
    } else if (modifiers.ctrl) {
      setSelectedIds((current) =>
        current.includes(stepId) ? current.filter((id) => id !== stepId) : orderedSelection(document, [...current, stepId]),
      );
      anchorId.current = stepId;
    } else {
      setSelectedIds([stepId]);
      anchorId.current = stepId;
    }
    setSelectedStepId(stepId);
    setSelectedLoopId("");
  };
  const clearSelection = () => {
    setSelectedIds(selectedStepId ? [selectedStepId] : []);
    setSelectedLoopId("");
  };
  const selectAll = () => {
    setSelectedIds(steps.map((step) => step.id));
    if (!selectedStepId && steps[0]) setSelectedStepId(steps[0].id);
  };
  // The selection follows the flow: steps that left it drop out, and a
  // focus set from elsewhere (the inspector, a run) collapses it.
  useEffect(() => {
    setSelectedIds((current) => {
      const kept = document ? orderedSelection(document, current) : [];
      if (selectedStepId && !kept.includes(selectedStepId)) return [selectedStepId];
      return kept.length === current.length && kept.every((id, i) => id === current[i]) ? current : kept;
    });
  }, [document, selectedStepId]);
  const batchIds = selectedIds.length > 1 ? selectedIds : selectedStepId ? [selectedStepId] : [];
  // A selected loop stays selected only while the focus is on one of its steps.
  useEffect(() => {
    if (!selectedLoopId) return;
    if (!loopSteps(steps, selectedLoopId).some((step) => step.id === selectedStepId)) setSelectedLoopId("");
  }, [steps, selectedStepId, selectedLoopId]);

  // -- loops ----------------------------------------------------------------------
  const selectLoop = (loopId: string, within: WorkspaceDocument | null = document) => {
    const members = within ? loopSteps(getSteps(within), loopId) : [];
    if (members.length === 0) return;
    setSelectedIds(members.map((step) => step.id));
    setSelectedStepId(members[members.length - 1].id);
    anchorId.current = members[0].id;
    setSelectedLoopId(loopId);
  };
  const selectedLoop = selectedLoopId ? loopSteps(steps, selectedLoopId) : [];
  const loopSummary: LoopSummary | null =
    selectedLoop.length > 0
      ? {
          loop: selectedLoop[0].loop!,
          stepsPerIteration: selectedLoop.length / selectedLoop[0].loop!.repeat,
          first: steps.indexOf(selectedLoop[0]) + 1,
          last: steps.indexOf(selectedLoop[selectedLoop.length - 1]) + 1,
          status: summarizeStatuses(selectedLoop.map((step) => statuses[step.id] ?? "dirty")),
        }
      : null;
  const loopActions: LoopActions = {
    create: (name, repeat) => {
      if (!document) return;
      const result = makeLoop(document, batchIds, name, repeat);
      if (!result) return;
      setDocument(result.document);
      selectLoop(result.loop.id, result.document);
    },
    select: (loopId) => selectLoop(loopId),
    rename: (loopId, name) => document && setDocument(renameLoop(document, loopId, name)),
    setRepeat: (loopId, repeat) => {
      if (!document) return;
      const next = setLoopRepeat(document, loopId, repeat);
      setDocument(next);
      if (selectedLoopId === loopId) selectLoop(loopId, next);
    },
    dissolve: (loopId) => {
      if (!document) return;
      setDocument(dissolveLoop(document, loopId));
      setSelectedLoopId("");
      setSelectedIds(selectedStepId ? [selectedStepId] : []);
    },
    remove: (loopId) => {
      if (!document) return;
      const members = loopSteps(steps, loopId);
      if (members.length === 0) return;
      if (!window.confirm(`Delete the loop ${members[0].loop!.name} (${members.length} steps) and its stored results?`)) return;
      const first = steps.indexOf(members[0]);
      const next = removeSteps(document, members.map((step) => step.id));
      setDocument(next);
      const remaining = getSteps(next);
      const focus = remaining[Math.min(Math.max(0, first - 1), remaining.length - 1)]?.id ?? "";
      setSelectedLoopId("");
      setSelectedStepId(focus);
      setSelectedIds(focus ? [focus] : []);
    },
    duplicate: (loopId) => {
      if (!document) return;
      const members = loopSteps(steps, loopId);
      if (members.length === 0) return;
      const result = insertSteps(document, members, members[members.length - 1].id);
      setDocument(result.document);
      const copy = result.steps[0]?.loop?.id;
      if (copy) selectLoop(copy, result.document);
    },
    copy: (loopId) => copyIds(loopSteps(steps, loopId).map((step) => step.id)),
    move: (loopId, direction) =>
      document && setDocument(moveSteps(document, loopSteps(steps, loopId).map((step) => step.id), direction)),
    setEnabled: (loopId, enabled) =>
      document && setDocument(setStepsEnabled(document, loopSteps(steps, loopId).map((step) => step.id), enabled)),
    runToEnd: (loopId) => {
      const last = loopSteps(steps, loopId).at(-1);
      if (last) void runFlow(last.id);
    },
  };
  const loopBlocker = document ? loopObstacle(document, batchIds) : "Open a project first.";

  const duplicateSelected = () => {
    if (!document || batchIds.length === 0) return;
    const result = duplicateSteps(document, batchIds);
    setDocument(result.document);
    setSelectedIds(result.steps.map((step) => step.id));
    setSelectedStepId(result.steps.at(-1)?.id ?? "");
  };
  const removeSelected = () => {
    if (!document || batchIds.length === 0) return;
    const names = batchIds.map((id) => steps.find((step) => step.id === id)?.name ?? id);
    const what = names.length === 1 ? names[0] : `${names.length} steps`;
    if (!window.confirm(`Delete ${what} and their stored results?`)) return;
    const first = steps.findIndex((step) => step.id === batchIds[0]);
    const next = removeSteps(document, batchIds);
    setDocument(next);
    const remaining = getSteps(next);
    const focus = remaining[Math.min(Math.max(0, first - 1), remaining.length - 1)]?.id ?? "";
    setSelectedStepId(focus);
    setSelectedIds(focus ? [focus] : []);
  };
  const moveSelected = (direction: -1 | 1) => {
    if (!document || batchIds.length === 0) return;
    setDocument(moveSteps(document, batchIds, direction));
  };
  const enableSelected = (enabled: boolean) => {
    if (!document || batchIds.length === 0) return;
    setDocument(setStepsEnabled(document, batchIds, enabled));
  };
  const copySelected = () => copyIds(batchIds);
  const copyIds = (ids: string[]) => {
    if (!document || ids.length === 0) return;
    const clip = clipFrom(document, ids);
    if (!clip) return;
    rememberClip(clip);
    setClipboardSize(clip.steps.length);
    // The whole payload as JSON on the system clipboard: that is what lets
    // another tab, another window, or a script read it -- and it carries
    // the materials, tools and sketches the steps name, because a step
    // pasted into a project that has never heard of its material cannot
    // run (see domain/clipboard).
    void navigator.clipboard?.writeText(JSON.stringify(clip, null, 2)).catch(() => undefined);
    report(`Copied ${clip.steps.length} step(s).`);
  };

  /** How many steps a paste would bring in, wherever they were copied. */
  const refreshPasteCount = useCallback(async () => {
    const clip = await clipToPaste();
    setClipboardSize(clip?.steps.length ?? 0);
  }, []);

  const pasteSteps = async () => {
    if (!document) return;
    const clip = await clipToPaste();
    if (!clip) {
      report("Nothing on the clipboard to paste.", true);
      setClipboardSize(0);
      return;
    }
    const result = pasteClip(document, clip, selectedStepId || undefined);
    setDocument(result.document);
    setSelectedIds(result.steps.map((step) => step.id));
    setSelectedStepId(result.steps.at(-1)?.id ?? "");
    const brought = [
      result.addedMaterials.length ? `${result.addedMaterials.join(", ")}` : "",
      result.addedTools.length ? `tool${result.addedTools.length > 1 ? "s" : ""} ${result.addedTools.join(", ")}` : "",
      result.newSketches.length ? `${result.newSketches.length} sketch(es)` : "",
    ].filter(Boolean);
    report(
      `Pasted ${result.steps.length} step(s)` +
        (clip.project && clip.project !== document.project.name ? ` from ${clip.project}` : "") +
        (brought.length ? `, and added ${brought.join(", ")}` : "") +
        ".",
    );
    // A sketch is a file of its own beside the workspace, not part of the
    // document, so a pasted one has to be written before a run can read it.
    for (const sketch of result.newSketches) {
      try {
        await bridge.saveSketch(document.root, sketch);
      } catch (reason) {
        report(`Could not save the pasted sketch ${sketch.name}: ${errorMessage(reason)}`, true);
      }
    }
  };

  // -- the File menu -------------------------------------------------------------
  const report = (message: string, show = false) => {
    setEvents((current) => [...current, { kind: "log", message }]);
    if (show) setShowLog(true);
  };
  const saveNow = async () => {
    if (!document) return;
    setSaveState("saving");
    try {
      const saved = await bridge.saveDocument(document);
      skipNextAutosave.current = true;
      setDocumentState(saved);
      setSaveState("saved");
    } catch (reason) {
      setSaveState("error");
      report(`Save failed: ${errorMessage(reason)}`, true);
    }
  };
  const withWorkspace = async (label: string, work: (root: string) => Promise<WorkspaceDocument | string | null | void>) => {
    if (!document || busy) return;
    setBusy(true);
    try {
      const saved = await bridge.saveDocument(document);
      skipNextAutosave.current = true;
      setDocumentState(saved);
      setSaveState("saved");
      const result = await work(saved.root);
      if (result && typeof result === "object") {
        viewCache.current.clear();
        openDocument(result);
        report(`${label} done.`);
      } else if (typeof result === "string") {
        report(`${label}: ${result}`);
      }
    } catch (reason) {
      report(`${label} failed: ${errorMessage(reason)}`, true);
    } finally {
      setBusy(false);
    }
  };
  const saveAs = () => withWorkspace("Save as", (root) => bridge.saveWorkspaceAs(root, document!.project.name));
  const exportFlowAs = (format: FlowExportFormat) =>
    withWorkspace(`Export flow (${format})`, (root) => bridge.exportFlow(root, format, document!.project.name));
  const importFlowFile = () => withWorkspace("Apply flow file", (root) => bridge.importFlow(root));
  const exportLibraryAs = (kind: LibraryKind) =>
    withWorkspace(`Export ${kind}`, (root) => bridge.exportLibrary(root, kind, document!.project.name));
  const importLibraryFrom = (kind: LibraryKind) => withWorkspace(`Import ${kind}`, (root) => bridge.importLibrary(root, kind));
  const revealFolder = () => {
    if (!document) return;
    void bridge.revealPath(document.root).catch((reason) => report(`Could not open the folder: ${errorMessage(reason)}`, true));
  };
  const closeProject = () => {
    if (!document) return;
    void (async () => {
      if (saveState === "unsaved") await saveNow();
      setDocumentState(null);
    })();
  };
  const forceRerun = () => {
    if (!document || busy) return;
    if (!window.confirm("Discard every stored result and run the whole flow again?")) return;
    void runFlow(undefined, true);
  };
  const checkUpdateFromMenu = () => {
    void bridge
      .checkUpdate()
      .then((info) =>
        setNotice(
          info.isNewer
            ? {
                title: `Version ${info.latestVersion} is available`,
                text: info.asset
                  ? `This is ${info.currentVersion}. Installing downloads ${
                      info.asset.sizeBytes ? `${Math.round(info.asset.sizeBytes / 1_048_576)} MB and ` : ""
                    }restarts the application.`
                  : `This is ${info.currentVersion}. There is no packaged build for this platform, so install it from the release page.`,
                primary: info.asset
                  ? {
                      label: "Install now",
                      run: () => {
                        const url = info.asset?.url;
                        if (url) void installUpdate(url).catch((reason) =>
                          setNotice({ title: "Could not install the update", text: errorMessage(reason) }),
                        );
                      },
                    }
                  : undefined,
                action: { label: "Release notes", run: () => void bridge.openUrl(info.releaseUrl) },
              }
            : { title: "Up to date", text: `${info.currentVersion} is the newest release.` },
        ),
      )
      .catch((reason) => setNotice({ title: "Could not check for updates", text: errorMessage(reason) }));
  };

  // -- keyboard shortcuts -----------------------------------------------------------
  useEffect(() => {
    if (!document) return;
    // Only the tab on screen: a hidden one would also copy its own
    // selection on Ctrl+C, and the last one to answer would win.
    if (!active) return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing =
        !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable);
      const mod = event.ctrlKey || event.metaKey;
      const key = event.key.toLowerCase();
      if (mod && key === "s") {
        event.preventDefault();
        void saveNow();
      } else if (event.key === "F5") {
        event.preventDefault();
        if (busy) return;
        // Ctrl runs the selected step again although it is up to date;
        // Shift runs the flow up to it.
        if (mod) {
          if (selectedStepId) void runFlow(undefined, false, selectedStepId);
        } else {
          void runFlow(event.shiftKey ? selectedStepId || undefined : undefined);
        }
      } else if (typing) {
        return;
      } else if (mod && key === "z" && !event.shiftKey) {
        event.preventDefault();
        undo();
      } else if ((mod && key === "y") || (mod && event.shiftKey && key === "z")) {
        event.preventDefault();
        redo();
      } else if (mod && key === "a") {
        event.preventDefault();
        selectAll();
      } else if (mod && key === "c") {
        copySelected();
      } else if (mod && key === "v") {
        void pasteSteps();
      } else if (mod && key === "d") {
        event.preventDefault();
        duplicateSelected();
      } else if (mod && key === "g") {
        event.preventDefault();
        if (loopBlocker === null) setLoopDialog(true);
      } else if (event.key === "Delete" || event.key === "Backspace") {
        if (batchIds.length && !busy) {
          event.preventDefault();
          removeSelected();
        }
      } else if (event.altKey && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
        event.preventDefault();
        moveSelected(event.key === "ArrowUp" ? -1 : 1);
      } else if (event.key === "Escape" && selectedIds.length > 1) {
        clearSelection();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const refreshViews = useCallback(async () => {
    // Views are read beside a run: the worker answers them on a lane of its
    // own, and a step that has finished is stored before the next begins.
    if (!root || !branchId) return;
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
        // Hiding a material is asking to see the cavity it leaves, which is
        // made of the faces around it: they have to be fetched then.
        const buried = alwaysBuried || hiddenMaterials.length > 0;
        const key = `surfaces:${target}:${interpolation}:${triangulation}:${buried}`;
        const hit = cached<SurfaceDocument>(key);
        if (hit) {
          setSurfaces(hit);
          setViewLoading(false);
          return;
        }
        setViewLoading(true);
        const next = await bridge.getSurfaces(root, { ...request, triangulation, buried });
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
        const key = `section:${target}:${interpolation}:${sectionAxis}:${position ?? "mid"}:${line ? line.start.join(",") + ">" + line.end.join(",") : ""}:`;
        const hit = cached<SectionDocument>(key);
        const next = hit ?? (await (async () => {
          setViewLoading(true);
          return remember(
            key,
            await bridge.getSection(root, { ...request, axis: sectionAxis, position, line }),
          );
        })());
        if (token !== viewToken.current) return;
        setSection(next);
        if (sectionAxis !== "line") {
          sectionPosition.current = next.position;
          if (next.index !== sectionIndex) setSectionIndex(next.index);
        }
      } else {
        // The top view is drawn by the worker, so hiding a material is a
        // different picture rather than something to switch off here.
        const key = `top:${target}:${topSteps}:${hiddenMaterials.join(",")}`;
        const hit = cached<TopViewDocument>(key);
        if (hit) {
          setTopView(hit);
          setViewLoading(false);
          return;
        }
        setViewLoading(true);
        const next = await bridge.getTopView(root, {
          ...request,
          hidden: hiddenMaterials,
          steps: topSteps,
        });
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
    // trigger here: a finished run flips `busy`, which is, and a step that
    // finishes mid-run bumps `viewNonce`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    root,
    branchId,
    busy,
    viewNonce,
    selectedStepId,
    selectedStatus,
    mode,
    interpolation,
    topSteps,
    triangulation,
    alwaysBuried,
    hiddenMaterials,
    sectionAxis,
    sectionIndex,
    sectionLine,
  ]);

  useEffect(() => {
    // A tab nobody is looking at asks for nothing: the views are the
    // expensive half of the worker's work, and they all share one worker.
    if (!active) return;
    void refreshViews();
  }, [refreshViews, active]);

  // Whoever copied last wins, wherever they did it: another tab shares
  // this window's held copy, another window goes through the system
  // clipboard. Both are settled by asking when this tab comes forward.
  useEffect(() => {
    if (!active) return;
    void refreshPasteCount();
    const onFocus = () => void refreshPasteCount();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [active, refreshPasteCount]);

  useEffect(() => {
    selectedStepRef.current = selectedStepId;
  }, [selectedStepId]);

  const runFlow = async (throughStepId?: string, force = false, fromStepId?: string) => {
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
        { branchId: branch.id, throughStepId, force, fromStepId, prepareBuried },
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
      if (runningStepId.current) forgetViews(runningStepId.current);
      runRequestId.current = undefined;
      setStopping(false);
      setBusy(false);
    }
  };

  // One CLI command against the open workspace, run inside the worker. The
  // document it hands back replaces what is on screen, so a `steps add` or
  // a pasted flow shows up at once, and a `run` reports through the same
  // progress events as the Run button (statuses, running marks, views).
  const runCli = async (argv: string[], stdin?: string): Promise<CliResult> => {
    const current = documentRef.current;
    if (!current) throw new Error("No workspace is open.");
    if (busyRef.current) throw new Error("The worker is busy; wait for the current run to finish.");
    setBusy(true);
    busyRef.current = true;
    runningStepId.current = undefined;
    const requestId = `cli-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    runRequestId.current = requestId;
    try {
      // The command works what is stored, so pending edits go first.
      const saved = await bridge.saveDocument(current);
      skipNextAutosave.current = true;
      documentRef.current = saved;
      setDocumentState(saved);
      setSaveState("saved");
      const result = await bridge.runCli(saved.root, argv, stdin, requestId);
      if (result.document) {
        viewCache.current.clear();
        skipNextAutosave.current = true;
        documentRef.current = result.document;
        setDocumentState(result.document);
        setSaveState("saved");
        const after = getSteps(result.document);
        if (!after.some((step) => step.id === selectedStepRef.current)) {
          setSelectedStepId(after[after.length - 1]?.id ?? "");
        }
        setViewNonce((nonce) => nonce + 1);
      }
      return result;
    } finally {
      if (runningStepId.current) forgetViews(runningStepId.current);
      runningStepId.current = undefined;
      runRequestId.current = undefined;
      busyRef.current = false;
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
    if (!requestId || stopping) return;
    setStopping(true);
    void bridge.cancel(requestId).catch((reason) => {
      setStopping(false);
      setEvents((current) => [
        ...current,
        { kind: "log", message: `Could not stop the run: ${errorMessage(reason)}` },
      ]);
    });
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

  // The film model is a project setting. Both modes' results stay stored,
  // so switching only changes which of them the views and statuses show:
  // the document is saved at once (the views read the stored setting) and
  // the statuses come back with it.
  const setFidelity = async (fidelity: Fidelity) => {
    if (!document || busy) return;
    setBusy(true);
    try {
      const saved = await bridge.saveDocument({
        ...document,
        project: { ...document.project, fidelity },
      });
      viewCache.current.clear();
      skipNextAutosave.current = true;
      setDocumentState(saved);
      setSaveState("saved");
      setViewNonce((nonce) => nonce + 1);
    } catch (reason) {
      setEvents((current) => [
        ...current,
        { kind: "log", message: `Could not switch the film model: ${errorMessage(reason)}` },
      ]);
      setShowLog(true);
    } finally {
      setBusy(false);
    }
  };

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
        <span>Opening {initialRoot}…</span>
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
        version={capabilities?.workerVersion}
        onCheckUpdate={() => bridge.checkUpdate()}
        onInstallUpdate={installUpdate}
        installProgress={installProgress}
        onOpenUrl={(url) => bridge.openUrl(url)}
      />
    );
  }

  const progress = events
    .filter((event) => event.kind === "progress" && event.total)
    .at(-1);

  const fidelity = document.project.fidelity ?? "detailed";
  const selectionCount = batchIds.length;
  const kernelProcessTypes = (projectKernel?.processTypes ?? ["deposit", "etch", "cmp", "no_geometry"]) as ProcessType[];
  const menus: Menu[] = [
    {
      label: "File",
      items: [
        { label: "New tab", action: () => onOpenInNewTab(""), shortcut: "Ctrl+T" },
        { label: "Close tab", action: onClose, shortcut: "Ctrl+W" },
        { label: "Open project…", action: () => void handleOpen(), shortcut: "Ctrl+O", separated: true },
        ...recent
          .filter((item) => item.root !== document.root)
          .slice(0, 5)
          .map((item, index) => ({
            label: item.name,
            heading: index === 0 ? "Recent (opens in a new tab)" : undefined,
            action: () => onOpenInNewTab(item.root),
          })),
        { label: "Save", action: () => void saveNow(), shortcut: "Ctrl+S", separated: true, disabled: saveState === "saving" },
        { label: "Save as…", action: () => void saveAs(), disabled: busy },
        { label: "Flow file (JSON, YAML)…", heading: "Import", action: () => void importFlowFile(), separated: true, disabled: busy },
        { label: "GDSII layout…", action: () => void handleImportGds(), disabled: busy },
        { label: "Materials…", action: () => void importLibraryFrom("materials"), disabled: busy },
        { label: "Recipes…", action: () => void importLibraryFrom("recipes"), disabled: busy },
        { label: "Tools…", action: () => void importLibraryFrom("tools"), disabled: busy },
        { label: "Flow as Excel…", heading: "Export", action: () => void exportFlowAs("xlsx"), separated: true, disabled: busy },
        { label: "Flow as CSV…", action: () => void exportFlowAs("csv"), disabled: busy },
        { label: "Flow file (JSON)…", action: () => void exportFlowAs("json"), disabled: busy },
        { label: "Flow file (YAML)…", action: () => void exportFlowAs("yaml"), disabled: busy },
        { label: "Materials…", action: () => void exportLibraryAs("materials"), disabled: busy },
        { label: "Recipes…", action: () => void exportLibraryAs("recipes"), disabled: busy },
        { label: "Tools…", action: () => void exportLibraryAs("tools"), disabled: busy },
        { label: "3D surfaces…", action: () => void exportMesh(), disabled: busy },
        { label: "Show workspace folder", action: revealFolder, separated: true },
        { label: "Close project", action: closeProject },
      ],
    },
    {
      label: "Edit",
      items: [
        { label: "Undo", action: undo, shortcut: "Ctrl+Z", disabled: historySize.undo === 0 },
        { label: "Redo", action: redo, shortcut: "Ctrl+Y", disabled: historySize.redo === 0 },
        ...kernelProcessTypes.map((type, index) => ({
          label: PROCESS_LABELS[type] ?? type,
          heading: index === 0 ? "Add step after the selected one" : undefined,
          separated: index === 0,
          action: () => {
            const { document: next, step } = addStep(document, type, selectedStepId);
            setDocument(next);
            setSelectedStepId(step.id);
          },
        })),
        { label: `Duplicate${selectionCount > 1 ? ` ${selectionCount} steps` : ""}`, action: duplicateSelected, shortcut: "Ctrl+D", separated: true, disabled: selectionCount === 0 },
        { label: `Copy${selectionCount > 1 ? ` ${selectionCount} steps` : ""}`, action: copySelected, shortcut: "Ctrl+C", disabled: selectionCount === 0 },
        { label: `Paste after the selected step${clipboardSize ? ` (${clipboardSize})` : ""}`, action: () => void pasteSteps(), shortcut: "Ctrl+V" },
        { label: `Delete${selectionCount > 1 ? ` ${selectionCount} steps` : ""}…`, action: removeSelected, shortcut: "Del", disabled: selectionCount === 0 || busy, danger: true },
        { label: `Repeat${selectionCount > 1 ? ` ${selectionCount} steps` : " the selected step"} as a loop…`, action: () => setLoopDialog(true), shortcut: "Ctrl+G", separated: true, disabled: loopBlocker !== null },
        { label: "Take the selected loop apart", action: () => selectedLoopId && loopActions.dissolve(selectedLoopId), disabled: !selectedLoopId },
        { label: "Select all steps", action: selectAll, shortcut: "Ctrl+A", separated: true },
        { label: "Move up", action: () => moveSelected(-1), shortcut: "Alt+↑", disabled: selectionCount === 0 },
        { label: "Move down", action: () => moveSelected(1), shortcut: "Alt+↓", disabled: selectionCount === 0 },
        { label: "Skip in the run", action: () => enableSelected(false), disabled: selectionCount === 0 },
        { label: "Include in the run", action: () => enableSelected(true), disabled: selectionCount === 0 },
      ],
    },
    {
      label: "View",
      items: [
        { label: "3D surfaces", action: () => setMode("surfaces"), checked: mode === "surfaces" },
        { label: "Section", action: () => setMode("section"), checked: mode === "section" },
        { label: "Top view", action: () => setMode("top"), checked: mode === "top" },
        { label: "Mark the steps inside a material on the top view", action: () => setTopSteps((value) => !value), checked: topSteps, separated: true },
        { label: "Worker log", action: () => setShowLog((value) => !value), checked: showLog, separated: true },
      ],
    },
    // How the 3D mesh is built, as opposed to what is being shown. Both
    // cost a rebuild of the mesh, so they live in a menu rather than under
    // the pointer; the footer keeps the triangulator too, for comparing.
    ...(projectKernel?.id === "slab"
      ? [
          {
            label: "Display",
            items: [
              {
                label: "Ear clipping (fast)",
                heading: "3D mesh triangulation",
                action: () => setTriangulation("ears"),
                checked: triangulation === "ears",
              },
              {
                label: "Delaunay",
                action: () => setTriangulation("delaunay"),
                checked: triangulation === "delaunay",
              },
              {
                label: "Build the faces between materials",
                heading: "Buried faces",
                separated: true,
                action: () => setAlwaysBuried((value) => !value),
                checked: alwaysBuried,
              },
              {
                label: hiddenMaterials.length
                  ? `Built now: a material is hidden (${hiddenMaterials.length})`
                  : "Built when a material is hidden",
                action: () => {},
                disabled: true,
              },
              {
                label: "Prepare them for every step after a run",
                separated: true,
                action: () => setPrepareBuried((value) => !value),
                checked: prepareBuried,
              },
              {
                label: "Otherwise prepared when a step's 3D view is opened",
                action: () => {},
                disabled: true,
              },
            ],
          },
        ]
      : []),
    {
      label: "Run",
      items: [
        { label: "Run the flow", action: () => void runFlow(), shortcut: "F5", disabled: busy },
        {
          label: "Run the selected step again",
          action: () => selectedStepId && void runFlow(undefined, false, selectedStepId),
          shortcut: "Ctrl+F5",
          disabled: busy || !selectedStepId,
        },
        { label: "Run to the selected step", action: () => void runFlow(selectedStepId || undefined), shortcut: "Shift+F5", disabled: busy || !selectedStepId },
        { label: stopping ? "Stopping…" : "Stop", action: stopRun, disabled: stopping || !(busy && runRequestId.current) },
        { label: "Discard results and run everything again…", action: forceRerun, disabled: busy, separated: true },
        ...(projectKernel?.id === "slab"
          ? [
              { label: "Detailed films (rounded)", heading: "Film model", action: () => void setFidelity("detailed"), checked: fidelity === "detailed", separated: true, disabled: busy },
              { label: "Simplified films (square, fast)", action: () => void setFidelity("simplified"), checked: fidelity === "simplified", disabled: busy },
            ]
          : []),
        { label: projectKernel && projectKernel.spacingRole !== "grid" ? "Geometry resolution…" : "Simulation grid…", action: () => setShowGrid(true), separated: true },
        { label: "Command console…", action: () => setShowCli(true) },
      ],
    },
    {
      label: "Libraries",
      items: [
        { label: "Materials…", action: () => setShowMaterials(true) },
        { label: "Recipes…", action: () => setShowRecipes(true) },
        { label: "Tools…", action: () => setShowTools(true) },
      ],
    },
    {
      label: "Help",
      items: [
        { label: "Documentation", action: () => void bridge.openUrl("https://github.com/lisiyuan2005/process-studio/") },
        { label: "Keyboard shortcuts", action: () => setShowShortcuts(true) },
        { label: "Check for updates", action: checkUpdateFromMenu, separated: true },
        { label: `About Process Studio ${capabilities?.workerVersion ?? ""}`, action: () => setNotice({ title: `Process Studio ${capabilities?.workerVersion ?? ""}`, text: `Kernel build: ${capabilities?.buildVariant ?? "full"}. Workspace: ${document.root}` }) },
      ],
    },
  ];

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
        panelShares
          ? ({
              "--steps-width": percent(panelShares.steps),
              "--inspector-width": percent(panelShares.inspector),
            } as React.CSSProperties)
          : undefined
      }
    >
      <header className="topbar">
        <button
          type="button"
          className="icon-button back-button"
          title="Close this tab"
          onClick={onClose}
        >
          <ArrowLeft size={17} />
        </button>
        <div className="brand-compact">
          <span className="brand-glyph">PS</span>
          <span>Process Studio</span>
        </div>
        <MenuBar menus={menus} />
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
          {projectKernel?.id === "slab" && (
            <button
              type="button"
              className={`fidelity-button fidelity-${document.project.fidelity ?? "detailed"}`}
              title={
                (document.project.fidelity ?? "detailed") === "simplified"
                  ? "Simplified: square films, one sample per plane, fast. Click for detailed films (rounded, sampled at the resolution). Results of both modes are kept."
                  : "Detailed: rounded films sampled at the resolution. Click for simplified films (square corners, much faster). Results of both modes are kept."
              }
              disabled={busy}
              onClick={() =>
                void setFidelity(
                  (document.project.fidelity ?? "detailed") === "simplified" ? "detailed" : "simplified",
                )
              }
            >
              <Layers size={13} />
              {(document.project.fidelity ?? "detailed") === "simplified" ? "Simplified" : "Detailed"}
            </button>
          )}
          {projectKernel && (
            <span
              className="kernel-badge"
              title={`${projectKernel.summary} Fixed when the project was created.`}
            >
              <Cpu size={13} />
              {projectKernel.id === "slab" ? "Slab" : projectKernel.id === "levelset" ? "Level set" : projectKernel.name}
            </span>
          )}
        </div>
        <div className="topbar-spacer" />
        <div className={`save-indicator save-${saveState}`}>
          {saveState === "saving" ? (
            <LoaderCircle className="spin" size={13} />
          ) : saveState === "error" ? (
            <CircleAlert size={13} />
          ) : (
            <Save size={13} />
          )}
          <span>
            {saveState === "saving"
              ? "Saving"
              : saveState === "unsaved"
                ? "Unsaved"
                : saveState === "error"
                  ? "Save failed"
                  : "Saved"}
          </span>
        </div>
        <button type="button" className="log-button" onClick={() => setShowLog((value) => !value)}>
          <ScrollText size={15} />
          Log{events.length > 0 && <span>{events.length}</span>}
        </button>
        {busy && runRequestId.current ? (
          <button
            type="button"
            className="primary-button run-button stop-button"
            title={
              stopping
                ? "Stopping: the running step gives up at its next check; steps that finished stay stored"
                : "Stop the run. The step running now gives up part-way and is not stored; steps that finished stay stored"
            }
            disabled={stopping}
            onClick={stopRun}
          >
            <Square size={13} fill="currentColor" />
            {stopping ? "Stopping…" : "Stop"}
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
          share={(panelShares ?? DEFAULT_PANELS).steps}
          onResize={(width) => resizePanel("steps", width)}
          onReset={resetPanels}
        />
        <PanelResizer
          side="right"
          share={(panelShares ?? DEFAULT_PANELS).inspector}
          onResize={(width) => resizePanel("inspector", width)}
          onReset={resetPanels}
        />
        <StepList
          steps={steps}
          statuses={statuses}
          accentFor={(step) => stepAccentColor(document, step)}
          selectedStepId={selectedStepId}
          selectedIds={selectedIds}
          busy={busy}
          canPaste={clipboardSize > 0}
          onSelect={selectStep}
          onClearSelection={clearSelection}
          onDuplicateSelected={duplicateSelected}
          onRemoveSelected={removeSelected}
          onMoveSelected={moveSelected}
          onEnableSelected={enableSelected}
          onCopySelected={copySelected}
          onPaste={() => void pasteSteps()}
          processTypes={kernelProcessTypes}
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
          selectedLoopId={selectedLoopId}
          loopObstacle={loopBlocker}
          loops={loopActions}
          loopDialogOpen={loopDialog}
          onLoopDialogChange={setLoopDialog}
        />
        <Viewport
          mode={mode}
          onModeChange={setMode}
          title={
            loopSummary
              ? `${loopSummary.loop.name} · after its last step (${loopSummary.last})`
              : selectedStep
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
          topSteps={topSteps}
          onTopStepsChange={setTopSteps}
          triangulation={triangulation}
          onTriangulationChange={setTriangulation}
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
          looks={materialLooks}
          onLookChange={(material, look) =>
            setMaterialLooks((current) => {
              const next = { ...current };
              if (look) next[material] = look;
              else delete next[material];
              return next;
            })
          }
          surfaces={surfaces}
          section={section}
          topView={topView}
        />
        <Inspector
          step={selectedStep}
          loop={loopSummary}
          onLoopRename={(name) => selectedLoopId && loopActions.rename(selectedLoopId, name)}
          onLoopRepeat={(repeat) => selectedLoopId && loopActions.setRepeat(selectedLoopId, repeat)}
          onLoopDissolve={() => selectedLoopId && loopActions.dissolve(selectedLoopId)}
          onLoopRemove={() => selectedLoopId && loopActions.remove(selectedLoopId)}
          onLoopRun={() => selectedLoopId && loopActions.runToEnd(selectedLoopId)}
          onSelectLoop={() => selectedStep?.loop && selectLoop(selectedStep.loop.id)}
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

      {notice && (
        <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label={notice.title}>
          <div className="modal-card notice-card">
            <header className="modal-header">
              <div>
                <h2>{notice.title}</h2>
              </div>
              <button type="button" className="icon-button" aria-label="Close" onClick={() => setNotice(null)}>
                <X size={16} />
              </button>
            </header>
            <p className="notice-text">{notice.text}</p>
            <div className="modal-actions">
              {notice.primary && (
                <button type="button" className="primary-button" onClick={() => { notice.primary?.run(); setNotice(null); }}>
                  {notice.primary.label}
                </button>
              )}
              {notice.action && (
                <button type="button" className="secondary-button" onClick={() => { notice.action?.run(); setNotice(null); }}>
                  {notice.action.label}
                </button>
              )}
              <button type="button" className="secondary-button" onClick={() => setNotice(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {showShortcuts && (
        <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts">
          <div className="modal-card notice-card">
            <header className="modal-header">
              <div>
                <span className="eyebrow">HELP</span>
                <h2>Keyboard shortcuts</h2>
              </div>
              <button type="button" className="icon-button" aria-label="Close" onClick={() => setShowShortcuts(false)}>
                <X size={16} />
              </button>
            </header>
            <table className="shortcut-table">
              <tbody>
                {[
                  ["Ctrl+S", "Save the workspace"],
                  ["Ctrl+O", "Open a project"],
                  ["F5", "Run the flow"],
                  ["Shift+F5", "Run to the selected step"],
                  ["Click / Ctrl+click / Shift+click", "Select a step / add or remove one / extend the selection"],
                  ["Ctrl+A", "Select every step"],
                  ["Ctrl+C / Ctrl+V", "Copy the selected steps / paste them after the selected step, in any project"],
                  ["Ctrl+T / Ctrl+W", "New tab / close this tab"],
                  ["Ctrl+Tab", "The next tab (Ctrl+Shift+Tab for the previous one)"],
                  ["Ctrl+D", "Duplicate the selected steps"],
                  ["Ctrl+G", "Repeat the selected steps as a loop"],
                  ["Delete", "Delete the selected steps"],
                  ["Alt+↑ / Alt+↓", "Move the selected steps"],
                  ["Ctrl+Z / Ctrl+Y", "Undo / redo an edit"],
                  ["Esc", "Keep only the focused step selected"],
                  ["Ctrl+Enter", "Run the pasted commands in the command console"],
                ].map(([keys, what]) => (
                  <tr key={keys}>
                    <td><kbd>{keys}</kbd></td>
                    <td>{what}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {showCli && (
        <CliPanel
          cli={capabilities?.cli}
          root={document.root}
          steps={steps}
          selectedStepId={selectedStepId}
          sectionLine={sectionLine}
          busy={busy}
          onRun={runCli}
          onStop={stopRun}
          onClose={() => setShowCli(false)}
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
