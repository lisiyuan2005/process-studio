import type { FlowBranch, ParameterValue, ProcessStep, ProcessType, Recipe, WorkspaceDocument } from "../types";
import { SIMULATION_KEYS, defaultParameters, depositThickness } from "./parameters";
import { STEP_NAMES, newId } from "./project";

/**
 * The experiment record: what was done in the lab, step by step, with the
 * tool and every setting. It is a flow of its own, kept with the project
 * and apart from the simulation's -- nothing here is run, nothing here makes
 * a result stale -- and it can be turned into a simulation branch.
 */

export function experimentSteps(document: WorkspaceDocument): ProcessStep[] {
  return document.project.experimentSteps ?? [];
}

function withExperiment(document: WorkspaceDocument, steps: ProcessStep[]): WorkspaceDocument {
  return { ...document, project: { ...document.project, experimentSteps: steps } };
}

function copyResponses(responses: ProcessStep["materialResponses"]) {
  return Object.fromEntries(Object.entries(responses).map(([name, response]) => [name, { ...response }]));
}

export function addExperimentStep(
  document: WorkspaceDocument,
  processType: ProcessType,
  afterId?: string,
): { document: WorkspaceDocument; step: ProcessStep } {
  const step: ProcessStep = {
    id: newId("exp"),
    name: STEP_NAMES[processType].replace(/^New /, ""),
    processType,
    tool: "",
    outputMaterial: processType === "oxidation" ? "SiO2" : null,
    parameters: defaultParameters(processType),
    experimentParameters: null,
    materialResponses: {},
    maskSource: "none",
    layer: null,
    datatype: null,
    keep: "inside",
    enabled: true,
    loop: null,
  };
  const steps = experimentSteps(document);
  const at = afterId ? steps.findIndex((item) => item.id === afterId) : -1;
  const next = at < 0 ? [...steps, step] : [...steps.slice(0, at + 1), step, ...steps.slice(at + 1)];
  return { document: withExperiment(document, next), step };
}

export function updateExperimentStep(
  document: WorkspaceDocument,
  id: string,
  patch: Partial<Omit<ProcessStep, "id">>,
): WorkspaceDocument {
  return withExperiment(
    document,
    experimentSteps(document).map((step) => (step.id === id ? { ...step, ...patch } : step)),
  );
}

/** Merge into a step's numbers; null removes one (the row's remove button). */
export function updateExperimentParameters(
  document: WorkspaceDocument,
  id: string,
  patch: Record<string, ParameterValue>,
): WorkspaceDocument {
  const step = experimentSteps(document).find((item) => item.id === id);
  if (!step) return document;
  const merged: Record<string, ParameterValue> = { ...step.parameters, ...patch };
  for (const [key, value] of Object.entries(patch)) if (value === null) delete merged[key];
  return updateExperimentStep(document, id, { parameters: merged });
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
  return withExperiment(document, experimentSteps(document).filter((step) => step.id !== id));
}

export function moveExperimentStep(document: WorkspaceDocument, id: string, direction: -1 | 1): WorkspaceDocument {
  const steps = [...experimentSteps(document)];
  const at = steps.findIndex((step) => step.id === id);
  const to = at + direction;
  if (at < 0 || to < 0 || to >= steps.length) return document;
  [steps[at], steps[to]] = [steps[to], steps[at]];
  return withExperiment(document, steps);
}

export function duplicateExperimentStep(
  document: WorkspaceDocument,
  id: string,
): { document: WorkspaceDocument; step: ProcessStep } | null {
  const steps = experimentSteps(document);
  const at = steps.findIndex((step) => step.id === id);
  if (at < 0) return null;
  const source = steps[at];
  const step: ProcessStep = {
    ...source,
    id: newId("exp"),
    parameters: { ...source.parameters },
    materialResponses: copyResponses(source.materialResponses),
  };
  return { document: withExperiment(document, [...steps.slice(0, at + 1), step, ...steps.slice(at + 1)]), step };
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
    enabled: true,
    loop: null,
  };
}

/** A new simulation branch made from the experiment record, made active;
 * the flows already there are left as they are. */
export function exportExperimentToBranch(
  document: WorkspaceDocument,
  name: string,
): { document: WorkspaceDocument; branch: FlowBranch } {
  const branch: FlowBranch = {
    id: newId("branch"),
    name,
    parentBranchId: null,
    parentStepId: null,
    steps: experimentSteps(document).map(simulationStepFrom),
  };
  return {
    document: {
      ...document,
      branches: [...document.branches, branch],
      project: { ...document.project, activeBranchId: branch.id },
    },
    branch,
  };
}

/** Experiment steps, in the shape a step template or a pasted step has. */
export function experimentFromSimulation(steps: ProcessStep[]): ProcessStep[] {
  return steps.map((step) => ({
    ...step,
    id: newId("exp"),
    parameters: { ...step.parameters, ...(step.experimentParameters ?? {}) },
    experimentParameters: null,
    materialResponses: copyResponses(step.materialResponses),
    enabled: true,
    loop: null,
  }));
}
