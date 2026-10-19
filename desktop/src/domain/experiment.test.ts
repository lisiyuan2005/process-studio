import { describe, expect, it } from "vitest";
import { demoDocument } from "../bridge/browserBridge";
import { CLIP_KIND, type StepClip } from "./clipboard";
import {
  addExperimentStep,
  clipFor,
  duplicateExperimentStep,
  experimentSteps,
  experimentView,
  exportExperimentToBranch,
  importIntoExperiment,
  moveExperimentStep,
  recipeFromExperiment,
  removeExperimentStep,
  simulationParameters,
  updateExperimentParameters,
  updateExperimentStep,
} from "./experiment";
import { getActiveBranch, getSteps, loopSteps, makeLoop, removeSteps } from "./project";
import { fromExperimentView } from "./experiment";

describe("the experiment record", () => {
  it("is edited apart from the simulation flow", () => {
    const start = demoDocument();
    const before = JSON.stringify(start.branches);
    let { document, step } = addExperimentStep(start, "deposit");
    document = updateExperimentStep(document, step.id, { tool: "Savannah ALD", outputMaterial: "HfO2" });
    document = updateExperimentParameters(document, step.id, {
      target: null,
      cycles: 240,
      rate_per_cycle: 0.0001,
      temperature_c: 250,
    });
    expect(JSON.stringify(document.branches)).toBe(before);
    const [saved] = experimentSteps(document);
    expect(saved.tool).toBe("Savannah ALD");
    expect(saved.parameters).toEqual({ cycles: 240, rate_per_cycle: 0.0001, temperature_c: 250 });

    const copy = duplicateExperimentStep(document, step.id)!;
    document = moveExperimentStep(copy.document, copy.step.id, -1);
    expect(experimentSteps(document).map((item) => item.id)).toEqual([copy.step.id, step.id]);
    document = removeExperimentStep(document, copy.step.id);
    expect(experimentSteps(document)).toHaveLength(1);
  });

  it("becomes a simulation branch with what the kernel reads", () => {
    let { document, step } = addExperimentStep(demoDocument(), "deposit");
    document = updateExperimentParameters(document, step.id, {
      target: null,
      cycles: 240,
      rate_per_cycle: 0.0001,
      temperature_c: 250,
      mode: "conformal",
      sketch_id: "default",
    });
    const etch = addExperimentStep(document, "etch", step.id);
    document = updateExperimentParameters(etch.document, etch.step.id, {
      target: null,
      time_min: 2,
      pressure_mtorr: 5,
    });
    document = updateExperimentStep(document, etch.step.id, {
      tool: "ICP",
      materialResponses: { Si: { material: "Si", rateUmPerMin: 0.05, stopLayer: false } },
    });
    const branchCount = document.branches.length;
    const { document: exported, branch } = exportExperimentToBranch(document, "From experiment");
    expect(exported.branches).toHaveLength(branchCount + 1);
    expect(getActiveBranch(exported).id).toBe(branch.id);
    const [film, cut] = branch.steps;
    expect(film.parameters).toEqual({ target: 0.024, mode: "conformal", sketch_id: "default" });
    expect(film.tool).toBe("");
    expect(cut.parameters).toEqual({ time_min: 2, directional_fraction: 1 });
    expect(cut.materialResponses.Si.rateUmPerMin).toBe(0.05);
    // the record itself is untouched
    expect(experimentSteps(exported)).toEqual(experimentSteps(document));
  });

  it("saves a template holding both kinds of numbers", () => {
    let { document, step } = addExperimentStep(demoDocument(), "deposit");
    document = updateExperimentParameters(document, step.id, { target: 0.01, temperature_c: 300 });
    const recipe = recipeFromExperiment(experimentSteps(document)[0], "HfO2 ALD");
    expect(recipe.parameters).toEqual({ target: 0.01 });
    expect(recipe.experimentParameters).toEqual({ target: 0.01, temperature_c: 300 });
    expect(simulationParameters(experimentSteps(document)[0])).toEqual({ target: 0.01 });
  });

  it("is edited with the flow's own step tools, loops included, and never touches a branch", () => {
    const start = demoDocument();
    const branches = JSON.stringify(start.branches);
    const statuses = JSON.stringify(start.stepStatuses);
    let document = start;
    const ids: string[] = [];
    for (const type of ["deposit", "etch", "cmp"] as const) {
      const added = addExperimentStep(document, type, ids.at(-1));
      document = added.document;
      ids.push(added.step.id);
    }
    const looped = makeLoop(experimentView(document), ids.slice(0, 2), "ALD pair", 3)!;
    document = fromExperimentView(document, looped.document);
    const record = experimentSteps(document);
    expect(record).toHaveLength(7);
    expect(loopSteps(record, looped.loop.id)).toHaveLength(6);
    // an edit inside the loop is made every time through
    document = updateExperimentParameters(document, ids[0], { temperature_c: 250 });
    expect(
      loopSteps(experimentSteps(document), looped.loop.id)
        .filter((step) => step.processType === "deposit")
        .map((step) => step.parameters.temperature_c),
    ).toEqual([250, 250, 250]);
    // several at once
    document = fromExperimentView(document, removeSteps(experimentView(document), [ids[2]]));
    expect(experimentSteps(document)).toHaveLength(6);
    expect(JSON.stringify(document.branches)).toBe(branches);
    expect(JSON.stringify(document.stepStatuses)).toBe(statuses);
    expect(document.project.activeBranchId).toBe(start.project.activeBranchId);
  });

  it("takes in a simulation branch at any time, loops and experiment numbers included", () => {
    let document = demoDocument();
    const flow = getSteps(document);
    const looped = makeLoop(document, [flow[0].id], "Twice", 2)!;
    document = looped.document;
    const source = getSteps(document);
    const first = addExperimentStep(document, "no_geometry");
    document = first.document;
    const imported = importIntoExperiment(document, source, first.step.id);
    document = imported.document;
    const record = experimentSteps(document);
    expect(record).toHaveLength(source.length + 1);
    expect(record[0].id).toBe(first.step.id);
    expect(record.slice(1).map((step) => step.name)).toEqual(source.map((step) => step.name));
    expect(record.filter((step) => step.loop).map((step) => step.loop!.iteration)).toEqual([0, 1]);
    // new ids: the record and the branch share nothing
    expect(record.some((step) => source.some((item) => item.id === step.id))).toBe(false);
    // and again, after what is already there
    document = importIntoExperiment(document, source.slice(-1), record.at(-1)!.id).document;
    expect(experimentSteps(document)).toHaveLength(record.length + 1);
  });

  it("exports the selected steps only, with their loop", () => {
    let document = demoDocument();
    const ids: string[] = [];
    for (const type of ["deposit", "etch", "cmp"] as const) {
      const added = addExperimentStep(document, type, ids.at(-1));
      document = added.document;
      ids.push(added.step.id);
    }
    const looped = makeLoop(experimentView(document), ids.slice(0, 2), "Pair", 2)!;
    document = fromExperimentView(document, looped.document);
    const loopIds = loopSteps(experimentSteps(document), looped.loop.id).map((step) => step.id);
    const { branch } = exportExperimentToBranch(document, "Pair only", loopIds);
    expect(branch.steps.map((step) => step.processType)).toEqual(["deposit", "etch", "deposit", "etch"]);
    expect(branch.steps.every((step) => step.loop?.repeat === 2)).toBe(true);
    expect(branch.steps.every((step) => step.tool === "")).toBe(true);
  });

  it("converts copied steps for the flow they are pasted into", () => {
    let { document, step } = addExperimentStep(demoDocument(), "deposit");
    document = updateExperimentParameters(document, step.id, { target: null, cycles: 100, rate_per_cycle: 0.0001, temperature_c: 250 });
    document = updateExperimentStep(document, step.id, { tool: "ALD" });
    const clip: StepClip = {
      kind: CLIP_KIND,
      version: 1,
      flow: "experiment",
      steps: experimentSteps(document),
      materials: [],
      tools: [],
      sketches: [],
    };
    expect(clipFor(clip, "experiment")).toBe(clip);
    const [simulated] = clipFor(clip, "simulation").steps;
    expect(simulated.parameters).toEqual({ target: 0.01 });
    expect(simulated.tool).toBe("");
    const back = clipFor({ ...clip, flow: "simulation", steps: [{ ...simulated, experimentParameters: { power_w: 30 } }] }, "experiment");
    expect(back.steps[0].parameters).toEqual({ target: 0.01, power_w: 30 });
    expect(back.steps[0].experimentParameters).toBeNull();
  });
});
