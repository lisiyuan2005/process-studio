import {
  ArrowLeft,
  CircleAlert,
  CheckCircle2,
  FileSpreadsheet,
  Grid3x3,
  LoaderCircle,
  Map as MapIcon,
  Palette,
  Play,
  Save,
  TerminalSquare,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { bridge } from "./bridge";
import { GridEditor } from "./components/GridEditor";
import { Inspector } from "./components/Inspector";
import { MaterialEditor } from "./components/MaterialEditor";
import { ProjectHome } from "./components/ProjectHome";
import { RecipeEditor } from "./components/RecipeEditor";
import { StepList } from "./components/StepList";
import { Viewport, type ViewMode } from "./components/Viewport";
import {
  addStep,
  getActiveBranch,
  getSteps,
  hasDirtySteps,
  markStep,
  recipeFor,
  removeMaterial,
  removeRecipe,
  removeStep,
  renameStep,
  reorderSteps,
  resolvedParameters,
  setActiveBranch,
  setStepStatuses,
  stepAccentColor,
  stepStatus,
  toggleStep,
  updateStep,
  updateStepOverrides,
  upsertMaterial,
  upsertRecipe,
} from "./domain/project";
import type {
  GridDefinition,
  ParameterValue,
  SectionDocument,
  SurfaceDocument,
  TopViewDocument,
  WorkerCapabilities,
  WorkerEvent,
  WorkspaceDocument,
} from "./types";

type SaveState = "saved" | "saving" | "unsaved" | "error";

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
  const [events, setEvents] = useState<WorkerEvent[]>([]);
  const [showLog, setShowLog] = useState(false);
  const [showMaterials, setShowMaterials] = useState(false);
  const [showRecipes, setShowRecipes] = useState(false);
  const [showGrid, setShowGrid] = useState(false);
  const [mode, setMode] = useState<ViewMode>("surfaces");
  const [interpolation, setInterpolation] = useState(1);
  const [sectionAxis, setSectionAxis] = useState<"x" | "y">("y");
  const [sectionIndex, setSectionIndex] = useState<number | null>(null);
  const [surfaces, setSurfaces] = useState<SurfaceDocument>();
  const [section, setSection] = useState<SectionDocument>();
  const [topView, setTopView] = useState<TopViewDocument>();
  const [viewLoading, setViewLoading] = useState(false);
  const [viewError, setViewError] = useState<string>();

  const skipNextAutosave = useRef(false);
  const runningStepId = useRef<string | undefined>(undefined);

  const branch = document ? getActiveBranch(document) : undefined;
  const steps = document ? getSteps(document) : [];
  const selectedStep = steps.find((step) => step.id === selectedStepId);
  const selectedRecipe = document ? recipeFor(document, selectedStep) : undefined;
  const statuses = useMemo(
    () => (document && branch ? document.stepStatuses[branch.id] ?? {} : {}),
    [document, branch],
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
    if (!capabilities.rendering.surfaces && mode === "surfaces") setMode("section");
  }, [capabilities, mode]);

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
    setDocumentState(next);
    setSaveState("saved");
    const first = getSteps(next)[0]?.id ?? "";
    setSelectedStepId(first);
    setSectionIndex(null);
  };

  const handleCreate = async () => {
    setBusy(true);
    setHomeError(undefined);
    try {
      const created = await bridge.createWorkspace();
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

  const refreshViews = useCallback(async () => {
    // A run holds the project database open; read the views once it is done.
    if (!document || !branch || busy) return;
    const status = selectedStepId ? statuses[selectedStepId] : "clean";
    if (selectedStepId && status !== "clean") {
      setSurfaces(undefined);
      setSection(undefined);
      setTopView(undefined);
      setViewError("This step has no stored result yet. Run the flow to see it.");
      return;
    }
    setViewLoading(true);
    setViewError(undefined);
    const request = { branchId: branch.id, stepId: selectedStepId, interpolation };
    try {
      if (mode === "surfaces") {
        setSurfaces(await bridge.getSurfaces(document.root, request));
      } else if (mode === "section") {
        const next = await bridge.getSection(document.root, {
          ...request,
          axis: sectionAxis,
          position:
            sectionIndex === null ? undefined : section?.positions?.[sectionIndex] ?? undefined,
        });
        setSection(next);
        if (next.index !== sectionIndex) setSectionIndex(next.index);
      } else {
        setTopView(await bridge.getTopView(document.root, request));
      }
    } catch (reason) {
      setViewError(errorMessage(reason));
    } finally {
      setViewLoading(false);
    }
    // `section` is intentionally excluded: it is the result this effect writes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [document, branch, busy, selectedStepId, statuses, mode, interpolation, sectionAxis, sectionIndex]);

  useEffect(() => {
    void refreshViews();
  }, [refreshViews]);

  const runFlow = async (throughStepId?: string) => {
    if (!document || !branch || busy) return;
    setBusy(true);
    runningStepId.current = undefined;
    let working: WorkspaceDocument = document;
    if (throughStepId) working = markStep(working, throughStepId, "running");
    setDocumentState(working);
    try {
      // Flush pending edits first: the worker runs what is stored, not what is on screen.
      const saved = await bridge.saveDocument(document);
      skipNextAutosave.current = true;
      setSaveState("saved");
      const result = await bridge.runFlow(saved.root, { branchId: branch.id, throughStepId });
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
      const failed = runningStepId.current;
      if (failed) {
        skipNextAutosave.current = true;
        setDocumentState((current) => (current ? markStep(current, failed, "failed") : current));
        setSelectedStepId(failed);
      }
      setEvents((current) => [
        ...current,
        { kind: "log", message: `Run failed: ${errorMessage(reason)}` },
      ]);
      setShowLog(true);
    } finally {
      setBusy(false);
    }
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

  const handleApplyGrid = async (grid: GridDefinition) => {
    if (!document || busy) return;
    setBusy(true);
    try {
      const saved = await bridge.saveDocument(document);
      const updated = await bridge.setGrid(saved.root, grid);
      skipNextAutosave.current = true;
      setDocumentState(updated);
      setSaveState("saved");
      setShowGrid(false);
      setEvents((current) => [
        ...current,
        {
          kind: "log",
          message: `Grid set to ${grid.nx}×${grid.ny}×${grid.nz} (${(
            grid.spacingUm * 1000
          ).toFixed(2)} nm). Stored results were discarded.`,
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
    return names;
  }, [document?.recipes]);

  const usedRecipeIds = useMemo(
    () => new Set(document?.branches.flatMap((item) => item.steps.map((step) => step.recipeId))),
    [document?.branches],
  );

  if (!document || !branch) {
    return (
      <ProjectHome
        runtime={bridge.runtime}
        busy={busy}
        error={homeError}
        onCreate={handleCreate}
        onOpen={handleOpen}
      />
    );
  }

  const progress = events
    .filter((event) => event.kind === "progress" && event.total)
    .at(-1);

  return (
    <div className="app-shell">
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
          <button type="button" title="Simulation grid" onClick={() => setShowGrid(true)}>
            <Grid3x3 size={13} />
            {(document.project.grid.spacingUm * 1000).toFixed(0)} nm
          </button>
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
          {busy ? "Running" : hasDirtySteps(document) ? "Run stale" : "Up to date"}
        </button>
      </header>

      {busy && progress?.total ? (
        <div className="global-progress">
          <div style={{ width: `${((progress.completed ?? 0) / progress.total) * 100}%` }} />
        </div>
      ) : null}

      <div className="workspace-grid">
        <StepList
          steps={steps}
          recipes={document.recipes}
          statuses={statuses}
          accentFor={(step) => stepAccentColor(document, step)}
          selectedStepId={selectedStepId}
          onSelect={setSelectedStepId}
          onAdd={(recipeId) => {
            const recipe = document.recipes.find((item) => item.id === recipeId);
            if (!recipe) return;
            const { document: next, step } = addStep(document, recipe, selectedStepId);
            setDocument(next);
            setSelectedStepId(step.id);
          }}
          onToggle={(stepId) => setDocument(toggleStep(document, stepId))}
          onReorder={(activeId, overId) => setDocument(reorderSteps(document, activeId, overId))}
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
          surfacesSupported={capabilities?.rendering.surfaces ?? false}
          maximumInterpolation={capabilities?.rendering.maximumInterpolation ?? 4}
          interpolation={interpolation}
          onInterpolationChange={setInterpolation}
          sectionAxis={sectionAxis}
          onSectionAxisChange={(axis) => {
            setSectionAxis(axis);
            setSectionIndex(null);
          }}
          sectionIndex={sectionIndex ?? 0}
          onSectionIndexChange={setSectionIndex}
          materials={document.materials}
          surfaces={surfaces}
          section={section}
          topView={topView}
        />
        <Inspector
          step={selectedStep}
          recipe={selectedRecipe}
          status={selectedStep ? stepStatus(document, selectedStep.id) : "dirty"}
          sketches={document.sketches}
          gdsPath={document.project.gdsPath}
          solverOrders={capabilities?.numerics.solverOrders ?? [1, 2]}
          resolved={selectedStep ? resolvedParameters(document, selectedStep) : {}}
          busy={busy}
          onRename={(name) => selectedStep && setDocument(renameStep(document, selectedStep.id, name))}
          onOverride={(patch: Record<string, ParameterValue>) =>
            selectedStep && setDocument(updateStepOverrides(document, selectedStep.id, patch))
          }
          onReplaceOverrides={(overrides) =>
            selectedStep && setDocument(updateStep(document, selectedStep.id, { overrides }))
          }
          onMaskChange={(patch) =>
            selectedStep && setDocument(updateStep(document, selectedStep.id, patch))
          }
          onRunToHere={() => selectedStep && void runFlow(selectedStep.id)}
          onRemove={() => {
            if (!selectedStep) return;
            if (!window.confirm(`Delete ${selectedStep.name} and its stored result?`)) return;
            const index = steps.indexOf(selectedStep);
            const next = removeStep(document, selectedStep.id);
            setDocument(next);
            setSelectedStepId(getSteps(next)[Math.max(0, index - 1)]?.id ?? "");
          }}
        />
      </div>

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
          usedRecipeIds={usedRecipeIds}
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

      {showGrid && (
        <GridEditor
          grid={document.project.grid}
          busy={busy}
          onApply={handleApplyGrid}
          onClose={() => setShowGrid(false)}
        />
      )}

      {showLog && (
        <section className="log-drawer">
          <div className="log-heading">
            <span>Worker activity</span>
            <button onClick={() => setEvents([])}>Clear</button>
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
