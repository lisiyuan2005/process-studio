import { describe, expect, it } from "vitest";
import { demoDocument } from "../bridge/browserBridge";
import {
  addStep,
  getActiveBranch,
  getSteps,
  hasDirtySteps,
  removeRecipe,
  removeStep,
  renameStep,
  reorderSteps,
  resolvedParameters,
  setStepStatuses,
  stepAccentColor,
  stepStatus,
  toggleStep,
  updateStepOverrides,
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

  it("resolves recipe parameters under step overrides", () => {
    const document = demoDocument();
    const etch = getSteps(document).find((step) => step.recipeId === "recipe-si-trench")!;
    const overridden = updateStepOverrides(document, etch.id, { target: 0.5 });
    const step = getSteps(overridden).find((item) => item.id === etch.id)!;
    expect(resolvedParameters(overridden, step)).toMatchObject({
      target: 0.5,
      directional_fraction: 0.9,
    });
  });

  it("treats a cleared override as a return to the recipe value", () => {
    const document = updateStepOverrides(demoDocument(), "step-etch", { target: 0.5 });
    const cleared = updateStepOverrides(document, "step-etch", { target: null });
    const step = getSteps(cleared).find((item) => item.id === "step-etch")!;
    expect(step.overrides).not.toHaveProperty("target");
    expect(resolvedParameters(cleared, step).target).toBe(0.32);
  });
});

describe("result invalidation", () => {
  it("dirties the edited step and everything after it", () => {
    const document = updateStepOverrides(cleanDocument(), "step-etch", { target: 0.4 });
    expect(stepStatus(document, "step-litho")).toBe("clean");
    expect(stepStatus(document, "step-etch")).toBe("dirty");
    expect(stepStatus(document, "step-strip")).toBe("dirty");
    expect(stepStatus(document, "step-ald")).toBe("dirty");
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
      "step-litho",
      "step-ald",
      "step-etch",
      "step-strip",
    ]);
    expect(stepStatus(document, "step-litho")).toBe("clean");
    expect(stepStatus(document, "step-ald")).toBe("dirty");
  });

  it("leaves the flow untouched when a reorder target is unknown", () => {
    const document = cleanDocument();
    expect(reorderSteps(document, "step-ald", "nope")).toBe(document);
  });

  it("dirties the rest of the flow when a step is disabled", () => {
    const document = toggleStep(cleanDocument(), "step-etch");
    expect(getSteps(document)[1].enabled).toBe(false);
    expect(stepStatus(document, "step-strip")).toBe("dirty");
  });

  it("dirties the steps that use an edited recipe", () => {
    const document = cleanDocument();
    const recipe = document.recipes.find((item) => item.id === "recipe-si-trench")!;
    const updated = upsertRecipe(document, {
      ...recipe,
      parameters: { ...recipe.parameters, target: 0.6 },
    });
    expect(stepStatus(updated, "step-litho")).toBe("clean");
    expect(stepStatus(updated, "step-etch")).toBe("dirty");
  });
});

describe("flow editing", () => {
  it("inserts a new step after the selected one", () => {
    const document = cleanDocument();
    const recipe = document.recipes.find((item) => item.id === "recipe-ald-al2o3")!;
    const { document: updated, step } = addStep(document, recipe, "step-litho");
    expect(getSteps(updated).map((item) => item.id)).toEqual([
      "step-litho",
      step.id,
      "step-etch",
      "step-strip",
      "step-ald",
    ]);
    expect(step.name).toBe(recipe.name);
    expect(stepStatus(updated, step.id)).toBe("dirty");
    expect(stepStatus(updated, "step-litho")).toBe("clean");
  });

  it("appends when no anchor is given", () => {
    const document = cleanDocument();
    const recipe = document.recipes[0];
    const { document: updated, step } = addStep(document, recipe);
    expect(getSteps(updated).at(-1)!.id).toBe(step.id);
  });

  it("removes a step and dirties what followed it", () => {
    const document = removeStep(cleanDocument(), "step-etch");
    expect(getSteps(document).map((step) => step.id)).toEqual([
      "step-litho",
      "step-strip",
      "step-ald",
    ]);
    expect(stepStatus(document, "step-litho")).toBe("clean");
    expect(stepStatus(document, "step-strip")).toBe("dirty");
  });

  it("refuses to delete a recipe a step still uses", () => {
    const document = cleanDocument();
    expect(removeRecipe(document, "recipe-si-trench")).toBe(document);
    expect(removeRecipe(document, "recipe-ald-al2o3")).toBe(document);
  });

  it("colours a step by the material it produces", () => {
    const document = demoDocument();
    const deposit = getSteps(document).find((step) => step.recipeId === "recipe-ald-al2o3")!;
    const etch = getSteps(document).find((step) => step.recipeId === "recipe-si-trench")!;
    const litho = getSteps(document).find((step) => step.recipeId === "recipe-litho")!;
    expect(stepAccentColor(document, deposit)).toBe("#ef9b35");
    expect(stepAccentColor(document, etch)).toBe("#7b68b8");
    expect(stepAccentColor(document, litho)).toBe("#98a5b1");
  });
});
