import { describe, expect, it } from "vitest";
import { demoDocument } from "../bridge/browserBridge";
import {
  addExperimentStep,
  duplicateExperimentStep,
  experimentSteps,
  exportExperimentToBranch,
  moveExperimentStep,
  recipeFromExperiment,
  removeExperimentStep,
  simulationParameters,
  updateExperimentParameters,
  updateExperimentStep,
} from "./experiment";
import { getActiveBranch } from "./project";

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
});
