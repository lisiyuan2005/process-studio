import type {
  SectionLine,
  ToolDefinition,
  FlowBranch,
  MaterialDefinition,
  ParameterValue,
  ProcessType,
  ProcessStep,
  Recipe,
  StepStatus,
  WorkspaceDocument,
} from "../types";

/** Every document edit goes through these pure helpers so the UI never mutates state in place. */

export function newId(prefix: string) {
  const random = globalThis.crypto?.randomUUID?.() ?? Math.random().toString(16).slice(2);
  return `${prefix}-${random.replace(/-/g, "").slice(0, 16)}`;
}

export function getActiveBranch(document: WorkspaceDocument): FlowBranch {
  const active = document.branches.find((branch) => branch.id === document.project.activeBranchId);
  return active ?? document.branches[0];
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

const STEP_DEFAULTS: Record<ProcessType, { name: string; parameters: Record<string, ParameterValue> }> = {
  deposit: { name: "New deposition", parameters: { target: 0.05 } },
  etch: { name: "New etch", parameters: { target: 0.1, directional_fraction: 1 } },
  cmp: { name: "New CMP", parameters: { target_z: 0 } },
  no_geometry: { name: "New process note", parameters: {} },
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
  const defaults = STEP_DEFAULTS[processType];
  const step: ProcessStep = {
    id: newId("step"),
    name: defaults.name,
    processType,
    tool: "",
    outputMaterial: null,
    parameters: { ...defaults.parameters },
    materialResponses: {},
    maskSource: "none",
    layer: null,
    datatype: null,
    keep: "inside",
    enabled: true,
  };
  const steps = [...getSteps(document)];
  const index = afterStepId ? steps.findIndex((item) => item.id === afterStepId) : -1;
  if (index >= 0) steps.splice(index + 1, 0, step);
  else steps.push(step);
  const branch = getActiveBranch(document);
  const statuses = { ...(document.stepStatuses[branch.id] ?? {}), [step.id]: "dirty" as StepStatus };
  const updated = {
    ...withSteps(document, steps),
    stepStatuses: { ...document.stepStatuses, [branch.id]: statuses },
  };
  return { document: invalidateFrom(updated, step.id), step };
}

export function removeStep(document: WorkspaceDocument, stepId: string): WorkspaceDocument {
  const steps = getSteps(document);
  const index = steps.findIndex((step) => step.id === stepId);
  if (index < 0) return document;
  const invalidated = invalidateFrom(document, stepId);
  return withSteps(
    invalidated,
    steps.filter((step) => step.id !== stepId),
  );
}

/** A copy of a step, placed right after it: the same settings under a new id, nothing run yet. */
export function duplicateStep(
  document: WorkspaceDocument,
  stepId: string,
): { document: WorkspaceDocument; step: ProcessStep } | null {
  const steps = [...getSteps(document)];
  const index = steps.findIndex((step) => step.id === stepId);
  if (index < 0) return null;
  const source = steps[index];
  const step: ProcessStep = {
    ...source,
    id: newId("step"),
    name: `${source.name} copy`,
    parameters: { ...source.parameters },
    materialResponses: copyResponses(source.materialResponses),
  };
  steps.splice(index + 1, 0, step);
  const branch = getActiveBranch(document);
  const statuses = { ...(document.stepStatuses[branch.id] ?? {}), [step.id]: "dirty" as StepStatus };
  const updated = {
    ...withSteps(document, steps),
    stepStatuses: { ...document.stepStatuses, [branch.id]: statuses },
  };
  return { document: invalidateFrom(updated, step.id), step };
}

/** Move a step one place up (-1) or down (+1); it and the step it passes need a re-run. */
export function moveStep(
  document: WorkspaceDocument,
  stepId: string,
  direction: -1 | 1,
): WorkspaceDocument {
  const steps = getSteps(document);
  const index = steps.findIndex((step) => step.id === stepId);
  const target = index < 0 ? undefined : steps[index + direction];
  if (!target) return document;
  return reorderSteps(document, stepId, target.id);
}

export function renameStep(
  document: WorkspaceDocument,
  stepId: string,
  name: string,
): WorkspaceDocument {
  const trimmed = name.trim();
  if (!trimmed) return document;
  // A name is metadata: it never invalidates a stored result.
  return withSteps(
    document,
    getSteps(document).map((step) => (step.id === stepId ? { ...step, name: trimmed } : step)),
  );
}

export function toggleStep(document: WorkspaceDocument, stepId: string): WorkspaceDocument {
  const updated = withSteps(
    document,
    getSteps(document).map((step) =>
      step.id === stepId ? { ...step, enabled: !step.enabled } : step,
    ),
  );
  return invalidateFrom(updated, stepId);
}

export function updateStep(
  document: WorkspaceDocument,
  stepId: string,
  patch: Partial<Omit<ProcessStep, "id">>,
): WorkspaceDocument {
  const updated = withSteps(
    document,
    getSteps(document).map((step) => (step.id === stepId ? { ...step, ...patch } : step)),
  );
  return invalidateFrom(updated, stepId);
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
    if (value === null || value === "") delete merged[key];
  }
  return updateStep(document, stepId, { parameters: merged });
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
    parameters: { ...STEP_DEFAULTS[processType].parameters },
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

export function reorderSteps(
  document: WorkspaceDocument,
  activeId: string,
  overId: string,
): WorkspaceDocument {
  const steps = [...getSteps(document)];
  const from = steps.findIndex((step) => step.id === activeId);
  const to = steps.findIndex((step) => step.id === overId);
  if (from < 0 || to < 0 || from === to) return document;
  const [moved] = steps.splice(from, 1);
  steps.splice(to, 0, moved);
  const earliest = steps[Math.min(from, to)];
  return invalidateFrom(withSteps(document, steps), earliest.id);
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
  const copies = sources.map((source) => cloneStep(source, rename(source.name)));
  const steps = [...getSteps(document)];
  const index = afterStepId ? steps.findIndex((step) => step.id === afterStepId) : -1;
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

/** Copies of the selected steps, placed right after the last of them. */
export function duplicateSteps(
  document: WorkspaceDocument,
  ids: Iterable<string>,
): { document: WorkspaceDocument; steps: ProcessStep[] } {
  const ordered = orderedSelection(document, ids);
  const steps = getSteps(document);
  const sources = ordered.map((id) => steps.find((step) => step.id === id)!);
  return insertSteps(document, sources, ordered.at(-1), (name) => `${name} copy`);
}

export function removeSteps(document: WorkspaceDocument, ids: Iterable<string>): WorkspaceDocument {
  const ordered = orderedSelection(document, ids);
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
 * step, stays; the block moves as far as it can.
 */
export function moveSteps(
  document: WorkspaceDocument,
  ids: Iterable<string>,
  direction: -1 | 1,
): WorkspaceDocument {
  const selected = new Set(orderedSelection(document, ids));
  if (selected.size === 0) return document;
  const steps = [...getSteps(document)];
  const indices = steps.map((_, index) => index).filter((index) => selected.has(steps[index].id));
  if (direction === 1) indices.reverse();
  let earliest: string | undefined;
  for (const index of indices) {
    const target = index + direction;
    if (target < 0 || target >= steps.length || selected.has(steps[target].id)) continue;
    [steps[index], steps[target]] = [steps[target], steps[index]];
    const first = Math.min(index, target);
    if (!earliest || steps.findIndex((step) => step.id === earliest) > first) earliest = steps[first].id;
  }
  if (!earliest) return document;
  return invalidateFrom(withSteps(document, steps), earliest);
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
