import { describe, expect, it } from "vitest";
import { demoDocument } from "../bridge/browserBridge";
import {
  addStep,
  getActiveBranch,
  getSteps,
  hasDirtySteps,
  loadRecipeIntoStep,
  recipeFromStep,
  removeRecipe,
  removeStep,
  renameStep,
  reorderSteps,
  setStepStatuses,
  stepAccentColor,
  stepStatus,
  toggleStep,
  updateStepParameters,
  upsertRecipe,
  validateDocument,
} from "./project";
import type { WorkspaceDocument } from "../types";

function cleanDocument(): WorkspaceDocument {
  const document = demoDocument();
  const branch = getActiveBranch(document);
  return setStepStatuses(
    document,
    branch.id,
    Object.fromEntries(branch.steps.map((step) => [step.id, "clean" as const])),
  );
}

describe("workspace document", () => {
  it("accepts the shape the worker returns", () => {
    expect(validateDocument(demoDocument())).toBe(true);
    expect(validateDocument({ root: "x" })).toBe(false);
    expect(validateDocument(null)).toBe(false);
  });

  it("stores process parameters directly on a step", () => {
    const updated = updateStepParameters(demoDocument(), "step-etch", { target: 0.5 });
    const step = getSteps(updated).find((item) => item.id === "step-etch")!;
    expect(step.parameters).toMatchObject({ target: 0.5, directional_fraction: 0.9 });
  });

  it("removes a parameter when its field is cleared", () => {
    const updated = updateStepParameters(demoDocument(), "step-etch", { target: null });
    const step = getSteps(updated).find((item) => item.id === "step-etch")!;
    expect(step.parameters).not.toHaveProperty("target");
  });

  it("copies a recipe without retaining a library relationship", () => {
    const document = demoDocument();
    const recipe = document.recipes.find((item) => item.id === "recipe-boe") ?? {
      id: "recipe-boe", name: "BOE", processType: "etch" as const, tool: "Wet Bench",
      outputMaterial: null, parameters: { time_min: 1, temperature_c: 25 },
      materialResponses: {},
    };
    const loaded = loadRecipeIntoStep(document, "step-etch", recipe);
    const step = getSteps(loaded).find((item) => item.id === "step-etch")!;
    expect(step.tool).toBe("Wet Bench");
    expect(step.parameters).toEqual({ ...recipe.parameters, sketch_id: "default" });
    expect(step).not.toHaveProperty("recipeId");
  });

  it("keeps mask selection out of reusable recipe definitions", () => {
    const document = demoDocument();
    const source = document.branches[0].steps.find((step) => step.id === "step-etch")!;
    const recipe = recipeFromStep(source, "Reusable etch");
    expect(recipe.parameters).not.toHaveProperty("sketch_id");

    const template = { ...document.recipes[0], parameters: { target: 0.42 } };
    const loaded = loadRecipeIntoStep(document, source.id, template);
    const step = loaded.branches[0].steps.find((item) => item.id === source.id)!;
    expect(step.parameters.sketch_id).toBe(source.parameters.sketch_id);
  });
});

describe("result invalidation", () => {
  it("marks the edited step and everything after it out of date", () => {
    const document = updateStepParameters(cleanDocument(), "step-etch", { target: 0.4 });
    expect(stepStatus(document, "step-litho")).toBe("clean");
    expect(stepStatus(document, "step-etch")).toBe("stale");
    expect(stepStatus(document, "step-strip")).toBe("stale");
    expect(stepStatus(document, "step-ald")).toBe("stale");
  });

  it("leaves a step that never ran with nothing stored", () => {
    const { document, step } = addStep(cleanDocument(), "etch", "step-litho");
    expect(stepStatus(document, step.id)).toBe("dirty");
    const edited = updateStepParameters(document, step.id, { target: 0.4 });
    // An edit cannot give a step a result it never had.
    expect(stepStatus(edited, step.id)).toBe("dirty");
    expect(stepStatus(edited, "step-etch")).toBe("stale");
  });

  it("keeps results when only the step name changes", () => {
    const document = renameStep(cleanDocument(), "step-etch", "Deep trench");
    expect(hasDirtySteps(document)).toBe(false);
    expect(getSteps(document)[1].name).toBe("Deep trench");
  });

  it("ignores an empty rename", () => {
    const document = renameStep(cleanDocument(), "step-etch", "   ");
    expect(getSteps(document)[1].name).toBe("Trench Etch");
  });

  it("dirties from the earliest moved position when steps are reordered", () => {
    const document = reorderSteps(cleanDocument(), "step-ald", "step-etch");
    expect(getSteps(document).map((step) => step.id)).toEqual([
      "step-litho", "step-ald", "step-etch", "step-strip",
    ]);
    expect(stepStatus(document, "step-litho")).toBe("clean");
    expect(stepStatus(document, "step-ald")).toBe("stale");
  });

  it("invalidates the rest of the flow when a step is skipped", () => {
    const document = toggleStep(cleanDocument(), "step-etch");
    expect(getSteps(document)[1].enabled).toBe(false);
    expect(stepStatus(document, "step-strip")).toBe("stale");
  });

  it("does not dirty a step when a library recipe changes", () => {
    const document = cleanDocument();
    const recipe = document.recipes[0];
    const updated = upsertRecipe(document, {
      ...recipe,
      parameters: { ...recipe.parameters, target: 0.6 },
    });
    expect(hasDirtySteps(updated)).toBe(false);
  });
});

describe("flow editing", () => {
  it("adds a named-by-type step rather than a recipe instance", () => {
    const { document, step } = addStep(cleanDocument(), "deposit", "step-litho");
    expect(getSteps(document).map((item) => item.id)).toEqual([
      "step-litho", step.id, "step-etch", "step-strip", "step-ald",
    ]);
    expect(step.name).toBe("New deposition");
    expect(step.processType).toBe("deposit");
    expect(step).not.toHaveProperty("recipeId");
  });

  it("removes a step and invalidates what followed it", () => {
    const document = removeStep(cleanDocument(), "step-etch");
    expect(getSteps(document).map((step) => step.id)).toEqual([
      "step-litho", "step-strip", "step-ald",
    ]);
    expect(stepStatus(document, "step-strip")).toBe("stale");
  });

  it("allows deleting a library recipe because steps own copies", () => {
    const document = cleanDocument();
    const updated = removeRecipe(document, "recipe-si-trench");
    expect(updated.recipes.some((recipe) => recipe.id === "recipe-si-trench")).toBe(false);
    expect(getSteps(updated).find((step) => step.id === "step-etch")?.tool).toBe("ICP-RIE");
  });

  it("colours a step by the material it owns", () => {
    const document = demoDocument();
    expect(stepAccentColor(document, getSteps(document).find((s) => s.id === "step-ald")!)).toBe("#ef9b35");
    expect(stepAccentColor(document, getSteps(document).find((s) => s.id === "step-etch")!)).toBe("#7b68b8");
    expect(stepAccentColor(document, getSteps(document).find((s) => s.id === "step-litho")!)).toBe("#98a5b1");
  });
});
