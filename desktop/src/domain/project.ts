import type {
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

/** Mark a step and everything after it dirty, the way the worker's digest chain does. */
function invalidateFrom(document: WorkspaceDocument, stepId: string): WorkspaceDocument {
  const branch = getActiveBranch(document);
  const index = branch.steps.findIndex((step) => step.id === stepId);
  if (index < 0) return document;
  const statuses = { ...(document.stepStatuses[branch.id] ?? {}) };
  branch.steps.slice(index).forEach((step) => {
    statuses[step.id] = "dirty";
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
