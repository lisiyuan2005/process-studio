import type {
  SectionLine,
  ToolDefinition,
  FlowBranch,
  MaterialDefinition,
  ParameterValue,
  ProcessType,
  ProcessStep,
  Recipe,
  StepLoop,
  StepStatus,
  WorkspaceDocument,
} from "../types";
import { defaultParameters } from "./parameters";

/** Every document edit goes through these pure helpers so the UI never mutates state in place. */

export function newId(prefix: string) {
  const random = globalThis.crypto?.randomUUID?.() ?? Math.random().toString(16).slice(2);
  return `${prefix}-${random.replace(/-/g, "").slice(0, 16)}`;
}

export function getActiveBranch(document: WorkspaceDocument): FlowBranch {
  const active = document.branches.find((branch) => branch.id === document.project.activeBranchId);
  return active ?? document.branches[0];
}

/**
 * For each step of a branch, the names of the branches forked after it.
 *
 * A process split leaves the flow carrying on in two places, and a list
 * that shows one branch cannot say so on its own.
 */
export function forksByStep(
  document: WorkspaceDocument,
  branchId: string,
): Record<string, string[]> {
  const byStep: Record<string, string[]> = {};
  for (const branch of document.branches) {
    if (branch.parentBranchId !== branchId || !branch.parentStepId) continue;
    (byStep[branch.parentStepId] ??= []).push(branch.name);
  }
  return byStep;
}

export function getSteps(document: WorkspaceDocument): ProcessStep[] {
  return getActiveBranch(document)?.steps ?? [];
}

export function stepStatus(document: WorkspaceDocument, stepId: string): StepStatus {
  const branch = getActiveBranch(document);
  return document.stepStatuses[branch?.id ?? ""]?.[stepId] ?? "dirty";
}

export function hasDirtySteps(document: WorkspaceDocument): boolean {
  return getSteps(document).some((step) => stepStatus(document, step.id) !== "clean");
}

const STEP_NAMES: Record<ProcessType, string> = {
  deposit: "New deposition",
  etch: "New etch",
  cmp: "New CMP",
  no_geometry: "New process note",
  oxidation: "New oxidation",
  flip: "Flip wafer",
};

function copyResponses(responses: Recipe["materialResponses"]) {
  return Object.fromEntries(
    Object.entries(responses).map(([material, response]) => [material, { ...response }]),
  );
}

function processParameters(parameters: Record<string, ParameterValue>) {
  return Object.fromEntries(Object.entries(parameters).filter(([key]) => key !== "sketch_id"));
}

function withBranch(document: WorkspaceDocument, branch: FlowBranch): WorkspaceDocument {
  return {
    ...document,
    branches: document.branches.map((item) => (item.id === branch.id ? branch : item)),
  };
}

function withSteps(document: WorkspaceDocument, steps: ProcessStep[]): WorkspaceDocument {
  const branch = getActiveBranch(document);
  return withBranch(document, { ...branch, steps });
}

/** Invalidate a step and everything after it, the way the worker's digest chain does. */
function invalidateFrom(document: WorkspaceDocument, stepId: string): WorkspaceDocument {
  const branch = getActiveBranch(document);
  const index = branch.steps.findIndex((step) => step.id === stepId);
  if (index < 0) return document;
  const statuses = { ...(document.stepStatuses[branch.id] ?? {}) };
  branch.steps.slice(index).forEach((step) => {
    // A step that had a current result keeps it, now marked out of date; one
    // that never ran still has nothing to show.
    statuses[step.id] = statuses[step.id] === "dirty" ? "dirty" : "stale";
  });
  return { ...document, stepStatuses: { ...document.stepStatuses, [branch.id]: statuses } };
}

export function setActiveBranch(document: WorkspaceDocument, branchId: string): WorkspaceDocument {
  if (!document.branches.some((branch) => branch.id === branchId)) return document;
  return { ...document, project: { ...document.project, activeBranchId: branchId } };
}

export function addStep(
  document: WorkspaceDocument,
  processType: ProcessType,
  afterStepId?: string,
): { document: WorkspaceDocument; step: ProcessStep } {
  const step: ProcessStep = {
    id: newId("step"),
    name: STEP_NAMES[processType],
    processType,
    tool: "",
    outputMaterial: processType === "oxidation" ? "SiO2" : null,
    parameters: defaultParameters(processType),
    materialResponses: {},
    maskSource: "none",
    layer: null,
    datatype: null,
    keep: "inside",
    enabled: true,
  };
  const steps = getSteps(document);
  const after = afterStepId ? steps.find((item) => item.id === afterStepId) : undefined;
  if (after?.loop) {
    // Inside a loop the new step joins every iteration, right after the
    // counterpart of the step it was added after.
    const inserted = insertAfterEach(document, loopCounterparts(steps, after.id), (anchor) => ({
      ...step,
      id: newId("step"),
      parameters: { ...step.parameters },
      loop: { ...anchor.loop! },
    }));
    const focus = inserted.steps[loopCounterparts(steps, after.id).indexOf(after.id)] ?? inserted.steps[0];
    return { document: inserted.document, step: focus };
  }
  const inserted = insertAfterEach(document, [after?.id ?? ""], () => step);
  return { document: inserted.document, step: inserted.steps[0] };
}

/**
 * Put one new step after each of `afterIds` (at the end for an id that is
 * not on the branch); the new steps have nothing run and invalidate what
 * follows the first of them. Returns the new steps in the order of `afterIds`.
 */
function insertAfterEach(
  document: WorkspaceDocument,
  afterIds: string[],
  make: (after: ProcessStep) => ProcessStep,
): { document: WorkspaceDocument; steps: ProcessStep[] } {
  const steps = [...getSteps(document)];
  const created: ProcessStep[] = [];
  for (const afterId of afterIds) {
    const index = steps.findIndex((item) => item.id === afterId);
    const made = make(steps[index] ?? steps[steps.length - 1]);
    if (index >= 0) steps.splice(index + 1, 0, made);
    else steps.push(made);
    created.push(made);
  }
  if (created.length === 0) return { document, steps: [] };
  const branch = getActiveBranch(document);
  const statuses = { ...(document.stepStatuses[branch.id] ?? {}) };
  for (const made of created) statuses[made.id] = "dirty";
  const updated = {
    ...withSteps(document, steps),
    stepStatuses: { ...document.stepStatuses, [branch.id]: statuses },
  };
  const first = steps.find((item) => created.some((made) => made.id === item.id))!;
  return { document: invalidateFrom(updated, first.id), steps: created };
}

/** Delete a step; inside a loop its counterpart in every iteration goes with it. */
export function removeStep(document: WorkspaceDocument, stepId: string): WorkspaceDocument {
  return removeSteps(document, [stepId]);
}

/**
 * A copy of a step, placed right after it: the same settings under a new
 * id, nothing run yet. Inside a loop the copy appears in every iteration.
 */
export function duplicateStep(
  document: WorkspaceDocument,
  stepId: string,
): { document: WorkspaceDocument; step: ProcessStep } | null {
  const steps = getSteps(document);
  const source = steps.find((step) => step.id === stepId);
  if (!source) return null;
  const counterparts = loopCounterparts(steps, stepId);
  const inserted = insertAfterEach(document, counterparts, (anchor) =>
    cloneStep(anchor, `${anchor.name} copy`),
  );
  return { document: inserted.document, step: inserted.steps[counterparts.indexOf(stepId)] };
}

/**
 * Move a step one place up (-1) or down (+1). A step inside a loop moves
 * within its iteration, and its counterparts move with it; a step on its
 * own passes a whole loop in one move.
 */
export function moveStep(
  document: WorkspaceDocument,
  stepId: string,
  direction: -1 | 1,
): WorkspaceDocument {
  return moveSteps(document, [stepId], direction);
}

export function renameStep(
  document: WorkspaceDocument,
  stepId: string,
  name: string,
): WorkspaceDocument {
  const trimmed = name.trim();
  if (!trimmed) return document;
  // A name is metadata: it never invalidates a stored result.
  const wanted = new Set(loopCounterparts(getSteps(document), stepId));
  return withSteps(
    document,
    getSteps(document).map((step) => (wanted.has(step.id) ? { ...step, name: trimmed } : step)),
  );
}

export function toggleStep(document: WorkspaceDocument, stepId: string): WorkspaceDocument {
  const steps = getSteps(document);
  const step = steps.find((item) => item.id === stepId);
  if (!step) return document;
  return setStepsEnabled(document, loopCounterparts(steps, stepId), !step.enabled);
}

/** Change a step's settings; inside a loop every iteration gets the same change. */
export function updateStep(
  document: WorkspaceDocument,
  stepId: string,
  patch: Partial<Omit<ProcessStep, "id">>,
): WorkspaceDocument {
  const steps = getSteps(document);
  const ids = loopCounterparts(steps, stepId);
  if (ids.length === 0) return document;
  const wanted = new Set(ids);
  const updated = withSteps(
    document,
    steps.map((step) =>
      wanted.has(step.id)
        ? {
            ...step,
            ...patch,
            ...(patch.parameters ? { parameters: { ...patch.parameters } } : {}),
            ...(patch.materialResponses ? { materialResponses: copyResponses(patch.materialResponses) } : {}),
          }
        : step,
    ),
  );
  return invalidateFrom(updated, ids[0]);
}

export function updateStepParameters(
  document: WorkspaceDocument,
  stepId: string,
  parameters: Record<string, ParameterValue>,
): WorkspaceDocument {
  const step = getSteps(document).find((item) => item.id === stepId);
  if (!step) return document;
  const merged: Record<string, ParameterValue> = { ...step.parameters, ...parameters };
  for (const [key, value] of Object.entries(parameters)) {
    // Null is the remove button. An empty string is a value: a text
    // parameter (the recipe loaded on the tool, a note) starts empty and
    // has to be addable before it is typed into.
    if (value === null) delete merged[key];
  }
  return updateStep(document, stepId, { parameters: merged });
}

/**
 * Edit what the tool was set to for a step.
 *
 * Unlike every other step edit this one does not invalidate anything: the
 * kernel never reads these values, so a result computed before they were
 * written down is still that result. Touching them for the first time
 * copies the simulation values, so only the differences have to be typed;
 * `null` gives that up and the two are the same again.
 */
export function updateStepExperiment(
  document: WorkspaceDocument,
  stepId: string,
  patch: Record<string, ParameterValue> | null,
): WorkspaceDocument {
  const steps = getSteps(document);
  const ids = new Set(loopCounterparts(steps, stepId));
  if (ids.size === 0) return document;
  return withSteps(
    document,
    steps.map((step) => {
      if (!ids.has(step.id)) return step;
      if (patch === null) return { ...step, experimentParameters: null };
      const merged = { ...(step.experimentParameters ?? step.parameters), ...patch };
      for (const [key, value] of Object.entries(patch)) {
        if (value === null) delete merged[key];
      }
      return { ...step, experimentParameters: merged };
    }),
  );
}

export function setStepProcessType(
  document: WorkspaceDocument,
  stepId: string,
  processType: ProcessType,
): WorkspaceDocument {
  return updateStep(document, stepId, {
    processType,
    tool: "",
    outputMaterial: null,
    parameters: defaultParameters(processType),
    materialResponses: {},
  });
}

/** Copy a library template into a step. There is deliberately no retained recipe id. */
export function loadRecipeIntoStep(
  document: WorkspaceDocument,
  stepId: string,
  recipe: Recipe,
): WorkspaceDocument {
  const sketchId = document.branches
    .flatMap((branch) => branch.steps)
    .find((step) => step.id === stepId)?.parameters.sketch_id;
  return updateStep(document, stepId, {
    processType: recipe.processType,
    tool: recipe.tool,
    outputMaterial: recipe.outputMaterial,
    parameters: {
      ...processParameters(recipe.parameters),
      ...(sketchId == null ? {} : { sketch_id: sketchId }),
    },
    materialResponses: copyResponses(recipe.materialResponses),
  });
}

export function recipeFromStep(step: ProcessStep, name: string): Recipe {
  return {
    id: newId("recipe"),
    name: name.trim() || step.name,
    processType: step.processType,
    tool: step.tool,
    group: "",
    outputMaterial: step.outputMaterial,
    parameters: processParameters(step.parameters),
    materialResponses: copyResponses(step.materialResponses),
  };
}

/**
 * Drop the dragged unit where another one is. A unit is a step on its own
 * or a whole loop (id `loop:ID`); steps inside a loop are not dragged.
 */
export function reorderSteps(
  document: WorkspaceDocument,
  activeId: string,
  overId: string,
): WorkspaceDocument {
  const steps = getSteps(document);
  const units = flowUnits(steps);
  const from = units.findIndex((unit) => unit.id === activeId);
  const to = units.findIndex((unit) => unit.id === overId);
  if (from < 0 || to < 0 || from === to) return document;
  const [moved] = units.splice(from, 1);
  units.splice(to, 0, moved);
  return withReordered(document, steps, units.flatMap((unit) => unit.steps));
}

/** The document with the steps in a new order, invalidated from the first step that moved. */
function withReordered(
  document: WorkspaceDocument,
  before: ProcessStep[],
  after: ProcessStep[],
): WorkspaceDocument {
  const first = after.findIndex((step, index) => before[index]?.id !== step.id);
  if (first < 0) return document;
  return invalidateFrom(withSteps(document, after), after[first].id);
}

export function upsertMaterial(
  document: WorkspaceDocument,
  material: MaterialDefinition,
): WorkspaceDocument {
  const exists = document.materials.some((item) => item.id === material.id);
  return {
    ...document,
    materials: exists
      ? document.materials.map((item) => (item.id === material.id ? material : item))
      : [...document.materials, material],
  };
}

export function removeMaterial(document: WorkspaceDocument, materialId: string): WorkspaceDocument {
  return {
    ...document,
    materials: document.materials.filter((item) => item.id !== materialId),
  };
}

export function upsertRecipe(document: WorkspaceDocument, recipe: Recipe): WorkspaceDocument {
  const exists = document.recipes.some((item) => item.id === recipe.id);
  const recipes = exists
    ? document.recipes.map((item) => (item.id === recipe.id ? recipe : item))
    : [...document.recipes, recipe];
  return { ...document, recipes };
}

export function removeRecipe(document: WorkspaceDocument, recipeId: string): WorkspaceDocument {
  return { ...document, recipes: document.recipes.filter((item) => item.id !== recipeId) };
}

export function setStepStatuses(
  document: WorkspaceDocument,
  branchId: string,
  statuses: Record<string, StepStatus>,
): WorkspaceDocument {
  return {
    ...document,
    stepStatuses: { ...document.stepStatuses, [branchId]: statuses },
  };
}

export function markStep(
  document: WorkspaceDocument,
  stepId: string,
  status: StepStatus,
): WorkspaceDocument {
  const branch = getActiveBranch(document);
  return setStepStatuses(document, branch.id, {
    ...(document.stepStatuses[branch.id] ?? {}),
    [stepId]: status,
  });
}

export function materialColor(document: WorkspaceDocument, name: string): string {
  return document.materials.find((material) => material.name === name)?.color ?? "#7c83a0";
}

/** Materials a step can produce or consume, used to colour the flow list. */
export function stepAccentColor(document: WorkspaceDocument, step: ProcessStep): string {
  if (step.outputMaterial) return materialColor(document, step.outputMaterial);
  const first = Object.keys(step.materialResponses)[0];
  return first ? materialColor(document, first) : "#98a5b1";
}

export function validateDocument(value: unknown): value is WorkspaceDocument {
  if (!value || typeof value !== "object") return false;
  const document = value as Partial<WorkspaceDocument>;
  return (
    typeof document.root === "string" &&
    !!document.project &&
    Array.isArray(document.branches) &&
    document.branches.length > 0 &&
    Array.isArray(document.recipes) &&
    Array.isArray(document.materials)
  );
}

/** A name no saved line has yet: "Line 1", "Line 2", ... */
export function nextSectionLineName(document: WorkspaceDocument): string {
  const taken = new Set(document.project.sectionLines.map((line) => line.name));
  let number = document.project.sectionLines.length + 1;
  while (taken.has(`Line ${number}`)) number += 1;
  return `Line ${number}`;
}

function withSectionLines(document: WorkspaceDocument, sectionLines: SectionLine[]): WorkspaceDocument {
  return { ...document, project: { ...document.project, sectionLines } };
}

/** Save a line: a new id appends it, a known id replaces it in place. */
export function upsertSectionLine(document: WorkspaceDocument, line: SectionLine): WorkspaceDocument {
  const lines = document.project.sectionLines;
  const index = lines.findIndex((item) => item.id === line.id);
  if (index < 0) return withSectionLines(document, [...lines, line]);
  return withSectionLines(document, lines.map((item, at) => (at === index ? line : item)));
}

export function removeSectionLine(document: WorkspaceDocument, lineId: string): WorkspaceDocument {
  return withSectionLines(
    document,
    document.project.sectionLines.filter((line) => line.id !== lineId),
  );
}

export function upsertTool(document: WorkspaceDocument, tool: ToolDefinition): WorkspaceDocument {
  const exists = document.tools.some((item) => item.id === tool.id);
  return {
    ...document,
    tools: exists
      ? document.tools.map((item) => (item.id === tool.id ? tool : item))
      : [...document.tools, tool],
  };
}

export function removeTool(document: WorkspaceDocument, toolId: string): WorkspaceDocument {
  return { ...document, tools: document.tools.filter((item) => item.id !== toolId) };
}

/** How many steps and recipes name each tool, for the editor's list. */
export function toolUsage(document: WorkspaceDocument): Map<string, number> {
  const usage = new Map<string, number>();
  const count = (name: string) => {
    if (name) usage.set(name, (usage.get(name) ?? 0) + 1);
  };
  document.branches.forEach((branch) => branch.steps.forEach((step) => count(step.tool)));
  document.recipes.forEach((recipe) => count(recipe.tool));
  return usage;
}

/** Recipes of one type arranged by group path: the tree the library shows. */
export interface RecipeGroup {
  /** The full path, "" at the root. */
  path: string;
  label: string;
  recipes: Recipe[];
  children: RecipeGroup[];
}

export function groupRecipes(recipes: Recipe[]): RecipeGroup {
  const root: RecipeGroup = { path: "", label: "", recipes: [], children: [] };
  const sorted = [...recipes].sort(
    (a, b) => a.group.localeCompare(b.group) || a.name.localeCompare(b.name),
  );
  for (const recipe of sorted) {
    let node = root;
    const parts = recipe.group ? recipe.group.split("/") : [];
    for (const [index, part] of parts.entries()) {
      const path = parts.slice(0, index + 1).join("/");
      let child = node.children.find((item) => item.path === path);
      if (!child) {
        child = { path, label: part, recipes: [], children: [] };
        node.children.push(child);
      }
      node = child;
    }
    node.recipes.push(recipe);
  }
  return root;
}

// -- several steps at once -----------------------------------------------------

/** The given ids in flow order, dropping any that are not on the branch. */
export function orderedSelection(document: WorkspaceDocument, ids: Iterable<string>): string[] {
  const wanted = new Set(ids);
  return getSteps(document)
    .filter((step) => wanted.has(step.id))
    .map((step) => step.id);
}

/** A fresh copy of a step: same settings, a new id, nothing run yet. */
export function cloneStep(source: ProcessStep, name = source.name): ProcessStep {
  return {
    ...source,
    id: newId("step"),
    name,
    parameters: { ...source.parameters },
    materialResponses: copyResponses(source.materialResponses),
    loop: source.loop ? { ...source.loop } : null,
  };
}

/**
 * Insert steps after `afterStepId` (at the end when it is not on the
 * branch); they get new ids, so a pasted or duplicated block never shares
 * identity with its source. Returns the inserted copies.
 */
export function insertSteps(
  document: WorkspaceDocument,
  sources: ProcessStep[],
  afterStepId: string | undefined,
  rename: (name: string) => string = (name) => name,
): { document: WorkspaceDocument; steps: ProcessStep[] } {
  if (sources.length === 0) return { document, steps: [] };
  const copies = relinkLoops(sources.map((source) => cloneStep(source, rename(source.name))));
  const steps = [...getSteps(document)];
  // After a step inside a loop means after the whole loop: a pasted block
  // never lands inside one iteration.
  const after = afterStepId ? steps.find((step) => step.id === afterStepId) : undefined;
  const anchor = after?.loop ? loopSteps(steps, after.loop.id).at(-1)! : after;
  const index = anchor ? steps.indexOf(anchor) : -1;
  const at = index >= 0 ? index + 1 : steps.length;
  steps.splice(at, 0, ...copies);
  const branch = getActiveBranch(document);
  const statuses = { ...(document.stepStatuses[branch.id] ?? {}) };
  for (const copy of copies) statuses[copy.id] = "dirty";
  const updated = {
    ...withSteps(document, steps),
    stepStatuses: { ...document.stepStatuses, [branch.id]: statuses },
  };
  return { document: invalidateFrom(updated, copies[0].id), steps: copies };
}

/**
 * Copies of the selected steps, placed right after the last of them. When
 * the selection reaches into a loop without covering it, those steps are
 * copied in place instead, in every iteration.
 */
export function duplicateSteps(
  document: WorkspaceDocument,
  ids: Iterable<string>,
): { document: WorkspaceDocument; steps: ProcessStep[] } {
  const ordered = orderedSelection(document, ids);
  const steps = getSteps(document);
  const selected = new Set(ordered);
  const partial = ordered.some((id) => {
    const loop = steps.find((step) => step.id === id)?.loop;
    return loop && !loopSteps(steps, loop.id).every((step) => selected.has(step.id));
  });
  if (partial) {
    const expanded = orderedSelection(document, ordered.flatMap((id) => loopCounterparts(steps, id)));
    return insertAfterEach(document, expanded, (anchor) => cloneStep(anchor, `${anchor.name} copy`));
  }
  const sources = ordered.map((id) => steps.find((step) => step.id === id)!);
  return insertSteps(document, sources, ordered.at(-1), (name) => `${name} copy`);
}

/** Delete steps; a step inside a loop takes its counterpart in every iteration with it. */
export function removeSteps(document: WorkspaceDocument, ids: Iterable<string>): WorkspaceDocument {
  const all = getSteps(document);
  const ordered = orderedSelection(
    document,
    [...ids].flatMap((id) => loopCounterparts(all, id)),
  );
  if (ordered.length === 0) return document;
  const invalidated = invalidateFrom(document, ordered[0]);
  const gone = new Set(ordered);
  return withSteps(
    invalidated,
    getSteps(document).filter((step) => !gone.has(step.id)),
  );
}

/**
 * Move every selected step one place up (-1) or down (+1), keeping their
 * order. A selected step at the edge, or one blocked by another selected
 * step, stays; the block moves as far as it can. A loop moves as one unit
 * when it is selected whole; a step selected inside a loop moves within
 * its iteration, together with its counterparts.
 */
export function moveSteps(
  document: WorkspaceDocument,
  ids: Iterable<string>,
  direction: -1 | 1,
): WorkspaceDocument {
  const selected = new Set(orderedSelection(document, ids));
  if (selected.size === 0) return document;
  const before = getSteps(document);
  let steps = [...before];

  // Steps inside a loop that is not selected whole move within their iteration.
  const whole = new Set(
    flowUnits(steps)
      .filter((unit) => unit.loop && unit.steps.every((step) => selected.has(step.id)))
      .map((unit) => unit.loop!.id),
  );
  const inside = steps.filter((step) => selected.has(step.id) && step.loop && !whole.has(step.loop.id));
  const movingInside = new Set(inside.flatMap((step) => loopCounterparts(steps, step.id)));
  const positions = steps.map((_, index) => index).filter((index) => movingInside.has(steps[index].id));
  if (direction === 1) positions.reverse();
  for (const index of positions) {
    const target = index + direction;
    const neighbour = steps[target];
    const own = steps[index].loop!;
    if (!neighbour?.loop || neighbour.loop.id !== own.id || neighbour.loop.iteration !== own.iteration) continue;
    if (movingInside.has(neighbour.id)) continue;
    [steps[index], steps[target]] = [steps[target], steps[index]];
  }

  // Everything else moves by units: a step on its own or a whole loop.
  const units = flowUnits(steps);
  const isSelected = (unit: FlowUnit) =>
    unit.loop ? whole.has(unit.loop.id) : selected.has(unit.steps[0].id);
  const order = units.map((_, index) => index).filter((index) => isSelected(units[index]));
  if (direction === 1) order.reverse();
  for (const index of order) {
    const target = index + direction;
    if (target < 0 || target >= units.length || isSelected(units[target])) continue;
    [units[index], units[target]] = [units[target], units[index]];
  }
  steps = units.flatMap((unit) => unit.steps);
  return withReordered(document, before, steps);
}

export function setStepsEnabled(
  document: WorkspaceDocument,
  ids: Iterable<string>,
  enabled: boolean,
): WorkspaceDocument {
  const ordered = orderedSelection(document, ids);
  if (ordered.length === 0) return document;
  const wanted = new Set(ordered);
  const updated = withSteps(
    document,
    getSteps(document).map((step) => (wanted.has(step.id) ? { ...step, enabled } : step)),
  );
  return invalidateFrom(updated, ordered[0]);
}

// -- loops: a block of steps repeated N times -----------------------------------
//
// Every iteration is a real step in the flow (so each has its own result
// and the view can stop after any of them), tagged with the loop it belongs
// to and its 0-based iteration. The editor keeps iterations identical: an
// edit, a new step or a deletion inside a loop is repeated in every
// iteration, and changing the count adds copies of the first iteration or
// drops the last ones. Nothing about a loop reaches a result: the worker
// only sees the flat list of steps.

/** A step on its own, or a whole loop: what the list drags and what moves as one. */
export interface FlowUnit {
  /** The step's id, or `loop:ID` for a loop. */
  id: string;
  loop: StepLoop | null;
  steps: ProcessStep[];
}

export function loopUnitId(loopId: string): string {
  return `loop:${loopId}`;
}

/** The flow as units, in order. A loop's steps are contiguous by construction. */
export function flowUnits(steps: ProcessStep[]): FlowUnit[] {
  const units: FlowUnit[] = [];
  const byLoop = new Map<string, FlowUnit>();
  for (const step of steps) {
    if (!step.loop) {
      units.push({ id: step.id, loop: null, steps: [step] });
      continue;
    }
    let unit = byLoop.get(step.loop.id);
    if (!unit) {
      unit = { id: loopUnitId(step.loop.id), loop: step.loop, steps: [] };
      byLoop.set(step.loop.id, unit);
      units.push(unit);
    }
    unit.steps.push(step);
  }
  return units;
}

/** All steps of a loop, in flow order. */
export function loopSteps(steps: ProcessStep[], loopId: string): ProcessStep[] {
  return steps.filter((step) => step.loop?.id === loopId);
}

/** The steps of a loop split per iteration. */
export function loopIterations(steps: ProcessStep[], loopId: string): ProcessStep[][] {
  const members = loopSteps(steps, loopId);
  const repeat = members[0]?.loop?.repeat ?? 0;
  const iterations: ProcessStep[][] = Array.from({ length: repeat }, () => []);
  for (const step of members) iterations[step.loop!.iteration]?.push(step);
  return iterations;
}

/**
 * A step's counterpart in every iteration of its loop (itself included),
 * in flow order: the step at the same position within each iteration. A
 * step outside a loop is its own only counterpart.
 */
export function loopCounterparts(steps: ProcessStep[], stepId: string): string[] {
  const step = steps.find((item) => item.id === stepId);
  if (!step) return [];
  if (!step.loop) return [stepId];
  const iterations = loopIterations(steps, step.loop.id);
  const position = iterations[step.loop.iteration]?.findIndex((item) => item.id === stepId) ?? -1;
  if (position < 0) return [stepId];
  return iterations.map((iteration) => iteration[position]?.id).filter((id): id is string => !!id);
}

/**
 * Copies of steps that carry loop tags get their own loop ids, and only
 * keep the tags when the copies hold every iteration of the loop; a partial
 * copy stands on its own.
 */
function relinkLoops(copies: ProcessStep[]): ProcessStep[] {
  const fresh = new Map<string, string>();
  const complete = new Set<string>();
  for (const loop of new Set(copies.filter((step) => step.loop).map((step) => step.loop!.id))) {
    const iterations = loopIterations(copies, loop);
    const sizes = iterations.map((iteration) => iteration.length);
    if (sizes.length > 0 && sizes.every((size) => size > 0 && size === sizes[0])) complete.add(loop);
    fresh.set(loop, newId("loop"));
  }
  return copies.map((step) => {
    if (!step.loop) return step;
    if (!complete.has(step.loop.id)) return { ...step, loop: null };
    return { ...step, loop: { ...step.loop, id: fresh.get(step.loop.id)! } };
  });
}

/** Why a selection cannot become a loop, or null when it can. */
export function loopObstacle(document: WorkspaceDocument, ids: Iterable<string>): string | null {
  const steps = getSteps(document);
  const ordered = orderedSelection(document, ids);
  if (ordered.length === 0) return "Select the steps to repeat first.";
  if (ordered.some((id) => steps.find((step) => step.id === id)?.loop)) {
    return "A step is already in a loop; take that loop apart first.";
  }
  const first = steps.findIndex((step) => step.id === ordered[0]);
  const contiguous = ordered.every((id, offset) => steps[first + offset]?.id === id);
  return contiguous ? null : "The steps of a loop must be next to each other.";
}

/**
 * Repeat a contiguous block of steps: the block becomes iteration 1 and
 * `repeat - 1` copies of it follow, each with nothing run yet.
 */
export function makeLoop(
  document: WorkspaceDocument,
  ids: Iterable<string>,
  name: string,
  repeat: number,
): { document: WorkspaceDocument; loop: StepLoop } | null {
  if (loopObstacle(document, ids) !== null) return null;
  const count = Math.max(1, Math.floor(repeat));
  const ordered = orderedSelection(document, ids);
  const loop: StepLoop = { id: newId("loop"), name: name.trim() || "Loop", repeat: count, iteration: 0 };
  const wanted = new Set(ordered);
  const tagged = withSteps(
    document,
    getSteps(document).map((step) => (wanted.has(step.id) ? { ...step, loop: { ...loop } } : step)),
  );
  return { document: growLoop(tagged, loop.id, count), loop };
}

/** Bring a loop to `repeat` iterations by copying the first one after the last. */
function growLoop(document: WorkspaceDocument, loopId: string, repeat: number): WorkspaceDocument {
  const steps = [...getSteps(document)];
  const members = loopSteps(steps, loopId);
  const template = members.filter((step) => step.loop!.iteration === 0);
  const have = members.reduce((count, step) => Math.max(count, step.loop!.iteration + 1), 0);
  const last = members.at(-1);
  if (!template.length || !last) return document;
  const copies: ProcessStep[] = [];
  for (let iteration = have; iteration < repeat; iteration += 1) {
    for (const source of template) {
      copies.push({ ...cloneStep(source), loop: { ...source.loop!, repeat, iteration } });
    }
  }
  const retagged = steps.map((step) =>
    step.loop?.id === loopId ? { ...step, loop: { ...step.loop, repeat } } : step,
  );
  if (copies.length === 0) return withSteps(document, retagged);
  retagged.splice(retagged.findIndex((step) => step.id === last.id) + 1, 0, ...copies);
  const branch = getActiveBranch(document);
  const statuses = { ...(document.stepStatuses[branch.id] ?? {}) };
  for (const copy of copies) statuses[copy.id] = "dirty";
  const updated = {
    ...withSteps(document, retagged),
    stepStatuses: { ...document.stepStatuses, [branch.id]: statuses },
  };
  return invalidateFrom(updated, copies[0].id);
}

/** Change how many times a loop runs: more copies of the first iteration, or the last ones dropped. */
export function setLoopRepeat(
  document: WorkspaceDocument,
  loopId: string,
  repeat: number,
): WorkspaceDocument {
  const count = Math.max(1, Math.floor(repeat));
  const steps = getSteps(document);
  const members = loopSteps(steps, loopId);
  if (members.length === 0) return document;
  const current = members[0].loop!.repeat;
  if (count >= current) return growLoop(document, loopId, count);
  const dropped = members.filter((step) => step.loop!.iteration >= count);
  const gone = new Set(dropped.map((step) => step.id));
  const invalidated = invalidateFrom(document, dropped[0].id);
  return withSteps(
    invalidated,
    getSteps(invalidated)
      .filter((step) => !gone.has(step.id))
      .map((step) => (step.loop?.id === loopId ? { ...step, loop: { ...step.loop, repeat: count } } : step)),
  );
}

export function renameLoop(document: WorkspaceDocument, loopId: string, name: string): WorkspaceDocument {
  const trimmed = name.trim();
  if (!trimmed) return document;
  return withSteps(
    document,
    getSteps(document).map((step) =>
      step.loop?.id === loopId ? { ...step, loop: { ...step.loop, name: trimmed } } : step,
    ),
  );
}

/** Take a loop apart: every iteration stays, as ordinary steps. Results are kept. */
export function dissolveLoop(document: WorkspaceDocument, loopId: string): WorkspaceDocument {
  return withSteps(
    document,
    getSteps(document).map((step) => (step.loop?.id === loopId ? { ...step, loop: null } : step)),
  );
}
