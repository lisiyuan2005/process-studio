import type { FlowBranch, ParameterValue, ProcessStep, ProcessType, Recipe, WorkspaceDocument } from "../types";
import type { StepClip } from "./clipboard";
import { SIMULATION_KEYS, defaultParameters, depositThickness } from "./parameters";
import {
  addStep,
  duplicateStep,
  getActiveBranch,
  getSteps,
  insertSteps,
  moveStep,
  newId,
  orderedSelection,
  removeStep,
  updateStep,
  updateStepParameters,
} from "./project";

/**
 * The experiment record: what was done in the lab, step by step, with the
 * tool and every setting. It is a flow of its own, kept with the project
 * and apart from the simulation's -- nothing here is run, nothing here makes
 * a result stale -- and it can be turned into a simulation branch.
 *
 * It is edited with the same step helpers as a branch (loops, several
 * steps at once, paste), through a view of the document in which the
 * record is the active branch; what the edit did to the record is taken
 * back out of the view, and the branches and their results are never
 * touched.
 */

export function experimentSteps(document: WorkspaceDocument): ProcessStep[] {
  return document.project.experimentSteps ?? [];
}

/** The id the record has as the view's only branch. */
export const EXPERIMENT_FLOW = "experiment-record";

/** The document with the experiment record as its only, active branch. */
export function experimentView(document: WorkspaceDocument): WorkspaceDocument {
  const record: FlowBranch = {
    id: EXPERIMENT_FLOW,
    name: "Experiment",
    parentBranchId: null,
    parentStepId: null,
    steps: experimentSteps(document),
  };
  return {
    ...document,
    branches: [record],
    stepStatuses: {},
    project: { ...document.project, activeBranchId: EXPERIMENT_FLOW },
  };
}

/**
 * An edit made on the view, back in the document: the record from the
 * view, and whatever else the edit changed there (a material or a tool a
 * paste brought in); the branches and their results as they were.
 */
export function fromExperimentView(document: WorkspaceDocument, view: WorkspaceDocument): WorkspaceDocument {
  return {
    ...view,
    branches: document.branches,
    stepStatuses: document.stepStatuses,
    project: {
      ...view.project,
      activeBranchId: document.project.activeBranchId,
      experimentSteps: getSteps(view),
    },
  };
}

/** Apply a branch edit to the experiment record. */
export function onExperiment(
  document: WorkspaceDocument,
  edit: (view: WorkspaceDocument) => WorkspaceDocument,
): WorkspaceDocument {
  return fromExperimentView(document, edit(experimentView(document)));
}

function copyResponses(responses: ProcessStep["materialResponses"]) {
  return Object.fromEntries(Object.entries(responses).map(([name, response]) => [name, { ...response }]));
}

export function addExperimentStep(
  document: WorkspaceDocument,
  processType: ProcessType,
  afterId?: string,
): { document: WorkspaceDocument; step: ProcessStep } {
  const result = addStep(experimentView(document), processType, afterId);
  return { document: fromExperimentView(document, result.document), step: result.step };
}

/** Change a step; inside a loop, every time through gets the change. */
export function updateExperimentStep(
  document: WorkspaceDocument,
  id: string,
  patch: Partial<Omit<ProcessStep, "id">>,
): WorkspaceDocument {
  return onExperiment(document, (view) => updateStep(view, id, patch));
}

/** Merge into a step's numbers; null removes one (the row's remove button). */
export function updateExperimentParameters(
  document: WorkspaceDocument,
  id: string,
  patch: Record<string, ParameterValue>,
): WorkspaceDocument {
  return onExperiment(document, (view) => updateStepParameters(view, id, patch));
}

export function setExperimentProcessType(
  document: WorkspaceDocument,
  id: string,
  processType: ProcessType,
): WorkspaceDocument {
  const step = experimentSteps(document).find((item) => item.id === id);
  if (!step) return document;
  return updateExperimentStep(document, id, {
    processType,
    outputMaterial: processType === "oxidation" ? "SiO2" : null,
    parameters: {
      ...defaultParameters(processType, step.tool),
      ...(step.parameters.sketch_id == null ? {} : { sketch_id: step.parameters.sketch_id }),
    },
    materialResponses: {},
  });
}

export function removeExperimentStep(document: WorkspaceDocument, id: string): WorkspaceDocument {
  return onExperiment(document, (view) => removeStep(view, id));
}

export function moveExperimentStep(document: WorkspaceDocument, id: string, direction: -1 | 1): WorkspaceDocument {
  return onExperiment(document, (view) => moveStep(view, id, direction));
}

export function duplicateExperimentStep(
  document: WorkspaceDocument,
  id: string,
): { document: WorkspaceDocument; step: ProcessStep } | null {
  const result = duplicateStep(experimentView(document), id);
  return result && { document: fromExperimentView(document, result.document), step: result.step };
}

/** A step template into an experiment step: its experiment numbers, or its
 * simulation numbers where it has none, and its tool. */
export function loadRecipeIntoExperiment(document: WorkspaceDocument, id: string, recipe: Recipe): WorkspaceDocument {
  const step = experimentSteps(document).find((item) => item.id === id);
  if (!step) return document;
  const numbers = { ...(recipe.experimentParameters ?? recipe.parameters) };
  delete numbers.sketch_id;
  return updateExperimentStep(document, id, {
    processType: recipe.processType,
    tool: recipe.tool,
    outputMaterial: recipe.outputMaterial,
    parameters: { ...numbers, ...(step.parameters.sketch_id == null ? {} : { sketch_id: step.parameters.sketch_id }) },
    materialResponses: copyResponses(recipe.materialResponses),
  });
}

/** A step template from an experiment step: both kinds of numbers, the
 * experiment's as they are and the simulation's worked out from them. */
export function recipeFromExperiment(step: ProcessStep, name: string): Recipe {
  const numbers = { ...step.parameters };
  delete numbers.sketch_id;
  const simulation = { ...simulationParameters(step) };
  delete simulation.sketch_id;
  return {
    id: newId("recipe"),
    name: name.trim() || step.name,
    processType: step.processType,
    tool: step.tool,
    group: "",
    outputMaterial: step.outputMaterial,
    parameters: simulation,
    experimentParameters: numbers,
    materialResponses: copyResponses(step.materialResponses),
  };
}

/** Fields a kernel reads that have no row of their own (a mask's sketch, a
 * rate table written as a field); carried over as they are. */
const KERNEL_EXTRAS = ["sketch_id", "material_rates", "stop_materials", "material", "materials", "stop_material"];

/**
 * What the simulation needs from an experiment step. A deposition's
 * thickness is its target, or cycles × rate per cycle, or time × rate;
 * an etch keeps its time (each material at its rate) or a target depth.
 * Temperature, pressure, power and the rest change nothing that is built.
 */
export function simulationParameters(step: ProcessStep): Record<string, ParameterValue> {
  const out: Record<string, ParameterValue> = {};
  for (const key of [...SIMULATION_KEYS[step.processType], ...KERNEL_EXTRAS]) {
    if (key in step.parameters) out[key] = step.parameters[key];
  }
  if (step.processType === "deposit" && out.target == null) {
    const resolved = depositThickness(step.parameters);
    if (resolved) out.target = resolved.um;
  }
  // an etch's single rate for a named material, the older way to write it
  if ((step.processType === "etch" || step.processType === "oxidation") && "material" in step.parameters) {
    if ("rate" in step.parameters) out.rate = step.parameters.rate;
  }
  return out;
}

export function simulationStepFrom(step: ProcessStep): ProcessStep {
  return {
    id: newId("step"),
    name: step.name,
    processType: step.processType,
    tool: "",
    outputMaterial: step.outputMaterial,
    parameters: simulationParameters(step),
    experimentParameters: null,
    materialResponses: copyResponses(step.materialResponses),
    maskSource: step.maskSource,
    layer: step.layer,
    datatype: step.datatype,
    keep: step.keep,
    enabled: step.enabled,
    loop: step.loop ? { ...step.loop } : null,
  };
}

/**
 * A new simulation branch made from the experiment record -- the steps
 * `ids` names, or all of them -- and made active; the flows already there
 * are left as they are. Loops come along as loops.
 */
export function exportExperimentToBranch(
  document: WorkspaceDocument,
  name: string,
  ids?: Iterable<string>,
): { document: WorkspaceDocument; branch: FlowBranch } {
  const record = experimentSteps(document);
  const wanted = ids === undefined ? null : new Set(orderedSelection(experimentView(document), ids));
  const sources = record.filter((step) => !wanted || wanted.has(step.id)).map(simulationStepFrom);
  const empty: FlowBranch = { id: newId("branch"), name, parentBranchId: null, parentStepId: null, steps: [] };
  const withBranch: WorkspaceDocument = {
    ...document,
    branches: [...document.branches, empty],
    project: { ...document.project, activeBranchId: empty.id },
  };
  const filled = insertSteps(withBranch, sources, undefined).document;
  return { document: filled, branch: getActiveBranch(filled) };
}

/** A simulation step as an experiment step: its numbers and its experiment
 * numbers in one set, since the record keeps every setting. */
export function experimentStepFrom(step: ProcessStep): ProcessStep {
  return {
    ...step,
    id: newId("step"),
    parameters: { ...step.parameters, ...(step.experimentParameters ?? {}) },
    experimentParameters: null,
    materialResponses: copyResponses(step.materialResponses),
    loop: step.loop ? { ...step.loop } : null,
  };
}

/** Experiment steps, in the shape a step template or a pasted step has. */
export function experimentFromSimulation(steps: ProcessStep[]): ProcessStep[] {
  return steps.map(experimentStepFrom);
}

/**
 * Simulation steps into the record after `afterId` (at the end when it is
 * not there): a whole branch or some of its steps. Loops stay loops.
 */
export function importIntoExperiment(
  document: WorkspaceDocument,
  sources: ProcessStep[],
  afterId?: string,
): { document: WorkspaceDocument; steps: ProcessStep[] } {
  const result = insertSteps(experimentView(document), experimentFromSimulation(sources), afterId);
  return { document: fromExperimentView(document, result.document), steps: result.steps };
}

/** A clipboard's steps for the flow they are pasted into: copied from the
 * simulation and pasted into the record, or the other way, they are
 * converted the way an import or an export would. */
export function clipFor(clip: StepClip, flow: "simulation" | "experiment"): StepClip {
  const from = clip.flow ?? "simulation";
  if (from === flow) return clip;
  const steps = flow === "experiment" ? clip.steps.map(experimentStepFrom) : clip.steps.map(simulationStepFrom);
  return { ...clip, flow, steps };
}
