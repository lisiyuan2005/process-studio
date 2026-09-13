import { describe, expect, it } from "vitest";
import { demoDocument } from "../bridge/browserBridge";
import {
  addStep,
  dissolveLoop,
  duplicateStep,
  duplicateSteps,
  flowUnits,
  insertSteps,
  loopCounterparts,
  loopIterations,
  loopObstacle,
  makeLoop,
  moveSteps,
  removeSteps,
  setLoopRepeat,
  getActiveBranch,
  groupRecipes,
  getSteps,
  hasDirtySteps,
  loadRecipeIntoStep,
  moveStep,
  nextSectionLineName,
  removeSectionLine,
  removeTool,
  toolUsage,
  upsertSectionLine,
  upsertTool,
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
      id: "recipe-boe", name: "BOE", processType: "etch" as const, tool: "Wet Bench", group: "",
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

  it("duplicates a step right after the original, with nothing run for the copy", () => {
    const result = duplicateStep(cleanDocument(), "step-etch");
    expect(result).not.toBeNull();
    const steps = getSteps(result!.document);
    const index = steps.findIndex((step) => step.id === "step-etch");
    const copy = steps[index + 1];
    expect(copy.id).toBe(result!.step.id);
    expect(copy.id).not.toBe("step-etch");
    expect(copy.name).toBe(`${steps[index].name} copy`);
    expect(copy.parameters).toEqual(steps[index].parameters);
    expect(copy.materialResponses).toEqual(steps[index].materialResponses);
    // The copy has never run; the original keeps its result, and what follows
    // the copy is built on a different stack now.
    expect(stepStatus(result!.document, copy.id)).toBe("dirty");
    expect(stepStatus(result!.document, "step-etch")).toBe("clean");
    expect(stepStatus(result!.document, "step-strip")).toBe("stale");
    expect(duplicateStep(cleanDocument(), "missing")).toBeNull();
  });

  it("moves a step one place and leaves the ends where they are", () => {
    const document = cleanDocument();
    const moved = moveStep(document, "step-strip", -1);
    expect(getSteps(moved).map((step) => step.id)).toEqual([
      "step-litho",
      "step-strip",
      "step-etch",
      "step-ald",
    ]);
    expect(stepStatus(moved, "step-litho")).toBe("clean");
    expect(stepStatus(moved, "step-strip")).toBe("stale");
    expect(stepStatus(moved, "step-etch")).toBe("stale");
    expect(moveStep(document, "step-litho", -1)).toBe(document);
    expect(moveStep(document, "step-ald", 1)).toBe(document);
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

describe("section lines", () => {
  it("saves, replaces and forgets named lines on the project", () => {
    const document = demoDocument();
    expect(nextSectionLineName(document)).toBe("Line 1");
    const one = upsertSectionLine(document, { id: "a", name: "Line 1", start: [-0.5, 0], end: [0.5, 0] });
    expect(one.project.sectionLines).toHaveLength(1);
    expect(nextSectionLineName(one)).toBe("Line 2");
    const moved = upsertSectionLine(one, { id: "a", name: "Across", start: [-0.6, 0.1], end: [0.6, 0.1] });
    expect(moved.project.sectionLines).toEqual([
      { id: "a", name: "Across", start: [-0.6, 0.1], end: [0.6, 0.1] },
    ]);
    expect(removeSectionLine(moved, "a").project.sectionLines).toEqual([]);
    // Editing lines never touches the flow or its results.
    expect(moved.branches).toBe(document.branches);
    expect(moved.stepStatuses).toBe(document.stepStatuses);
  });
});

describe("libraries", () => {
  it("arranges recipes as a tree of group paths", () => {
    const base = { processType: "deposit" as const, tool: "", outputMaterial: null, parameters: {}, materialResponses: {} };
    const tree = groupRecipes([
      { ...base, id: "a", name: "Al2O3", group: "ALD/Oxides" },
      { ...base, id: "b", name: "TiN", group: "ALD" },
      { ...base, id: "c", name: "Sputter W", group: "" },
    ]);
    expect(tree.recipes.map((r) => r.name)).toEqual(["Sputter W"]);
    expect(tree.children.map((g) => g.path)).toEqual(["ALD"]);
    expect(tree.children[0].recipes.map((r) => r.name)).toEqual(["TiN"]);
    expect(tree.children[0].children[0]).toMatchObject({ path: "ALD/Oxides", label: "Oxides" });
    expect(tree.children[0].children[0].recipes.map((r) => r.name)).toEqual(["Al2O3"]);
  });

  it("keeps tools on the document and counts who names them", () => {
    const document = upsertTool(demoDocument(), { id: "t1", name: "ICP-RIE", group: "Etch/Dry", notes: "" });
    expect(document.tools).toHaveLength(1);
    expect(upsertTool(document, { id: "t1", name: "ICP-RIE 2", group: "Etch", notes: "" }).tools[0].name).toBe("ICP-RIE 2");
    expect(removeTool(document, "t1").tools).toEqual([]);
    expect(toolUsage(document).get("ICP-RIE")).toBeGreaterThan(0);
  });
});

describe("loops", () => {
  const names = (document: WorkspaceDocument) => getSteps(document).map((step) => step.name);
  const looped = () => {
    // The etch and the strip become a pair run three times, before the ALD.
    const result = makeLoop(cleanDocument(), ["step-etch", "step-strip"], "Trench pair", 3)!;
    return { document: result.document, loop: result.loop };
  };

  it("repeats a contiguous block: the block is iteration 1, copies follow it", () => {
    expect(loopObstacle(cleanDocument(), ["step-litho", "step-strip"])).toMatch(/next to each other/);
    const { document, loop } = looped();
    expect(names(document)).toEqual([
      "Lithography", "Trench Etch", "Resist Strip", "Trench Etch", "Resist Strip", "Trench Etch", "Resist Strip", "Conformal Al2O3",
    ]);
    const steps = getSteps(document);
    expect(steps.slice(1, 7).map((step) => step.loop?.iteration)).toEqual([0, 0, 1, 1, 2, 2]);
    expect(steps.slice(1, 7).every((step) => step.loop?.id === loop.id && step.loop.repeat === 3)).toBe(true);
    // The original pair keeps its results; the copies and what follows do not.
    expect(stepStatus(document, "step-etch")).toBe("clean");
    expect(stepStatus(document, steps[3].id)).toBe("dirty");
    expect(stepStatus(document, "step-ald")).toBe("stale");
    expect(loopObstacle(document, ["step-etch"])).toMatch(/already in a loop/);
    expect(flowUnits(steps).map((unit) => unit.steps.length)).toEqual([1, 6, 1]);
  });

  it("finds a step's counterpart in every iteration", () => {
    const { document } = looped();
    const steps = getSteps(document);
    expect(loopCounterparts(steps, "step-strip")).toEqual([steps[2].id, steps[4].id, steps[6].id]);
    expect(loopCounterparts(steps, "step-litho")).toEqual(["step-litho"]);
    expect(loopIterations(steps, steps[1].loop!.id).map((iteration) => iteration.length)).toEqual([2, 2, 2]);
  });

  it("repeats an edit, a rename, a skip, a new step and a deletion in every iteration", () => {
    const { document } = looped();
    let steps = getSteps(document);
    const edited = updateStepParameters(document, steps[3].id, { target: 0.42 });
    steps = getSteps(edited);
    expect([steps[1], steps[3], steps[5]].map((step) => step.parameters.target)).toEqual([0.42, 0.42, 0.42]);
    // Invalidation starts at the first iteration, whichever copy was edited.
    expect(stepStatus(edited, "step-etch")).toBe("stale");
    expect(stepStatus(edited, "step-litho")).toBe("clean");

    steps = getSteps(renameStep(edited, "step-strip", "Ash"));
    expect(steps.filter((step) => step.name === "Ash")).toHaveLength(3);
    steps = getSteps(toggleStep(edited, steps[4].id));
    expect([steps[2], steps[4], steps[6]].map((step) => step.enabled)).toEqual([false, false, false]);

    const added = addStep(edited, "cmp", steps[4].id);
    steps = getSteps(added.document);
    expect(names(added.document)).toEqual([
      "Lithography", "Trench Etch", "Resist Strip", "New CMP", "Trench Etch", "Resist Strip", "New CMP",
      "Trench Etch", "Resist Strip", "New CMP", "Conformal Al2O3",
    ]);
    expect(steps[6].id).toBe(added.step.id);
    expect(steps.slice(1, 10).every((step) => step.loop)).toBe(true);

    const removed = removeStep(added.document, added.step.id);
    expect(names(removed)).toEqual(names(edited));
    const duplicated = duplicateStep(edited, "step-etch")!;
    expect(names(duplicated.document).filter((name) => name === "Trench Etch copy")).toHaveLength(3);
  });

  it("changes the count by copying the first iteration or dropping the last ones", () => {
    const { document, loop } = looped();
    const grown = setLoopRepeat(document, loop.id, 4);
    expect(names(grown).filter((name) => name === "Trench Etch")).toHaveLength(4);
    expect(getSteps(grown).every((step) => !step.loop || step.loop.repeat === 4)).toBe(true);
    const shrunk = setLoopRepeat(grown, loop.id, 2);
    expect(names(shrunk)).toEqual([
      "Lithography", "Trench Etch", "Resist Strip", "Trench Etch", "Resist Strip", "Conformal Al2O3",
    ]);
    expect(getSteps(shrunk)[1].loop?.repeat).toBe(2);
    expect(stepStatus(shrunk, "step-etch")).toBe("clean");
    const apart = dissolveLoop(shrunk, loop.id);
    expect(getSteps(apart).every((step) => !step.loop)).toBe(true);
    expect(names(apart)).toEqual(names(shrunk));
  });

  it("moves a loop as one unit and a step inside it within its iteration", () => {
    const { document, loop } = looped();
    const steps = getSteps(document);
    const loopIds = steps.slice(1, 7).map((step) => step.id);
    // The lithography passes the whole loop in one move.
    expect(names(moveSteps(document, ["step-litho"], 1))).toEqual([
      "Trench Etch", "Resist Strip", "Trench Etch", "Resist Strip", "Trench Etch", "Resist Strip", "Lithography", "Conformal Al2O3",
    ]);
    // The whole loop, selected, moves up past it.
    expect(names(moveSteps(document, loopIds, -1))[0]).toBe("Trench Etch");
    expect(names(moveSteps(document, loopIds, -1))[6]).toBe("Lithography");
    // One step inside moves within every iteration, and stops at the iteration's edge.
    const swapped = moveSteps(document, [steps[4].id], -1);
    expect(names(swapped)).toEqual([
      "Lithography", "Resist Strip", "Trench Etch", "Resist Strip", "Trench Etch", "Resist Strip", "Trench Etch", "Conformal Al2O3",
    ]);
    expect(moveSteps(swapped, [getSteps(swapped)[1].id], -1)).toBe(swapped);
    // Dragging by units: the loop dropped where the lithography is.
    expect(names(reorderSteps(document, `loop:${loop.id}`, "step-litho"))[0]).toBe("Trench Etch");
  });

  it("copies a whole loop as a new loop and a partial copy as plain steps", () => {
    const { document, loop } = looped();
    const steps = getSteps(document);
    const whole = insertSteps(document, steps.slice(1, 7), "step-ald");
    const copies = whole.steps;
    expect(copies.every((step) => step.loop && step.loop.id !== loop.id)).toBe(true);
    expect(new Set(copies.map((step) => step.loop!.id)).size).toBe(1);
    const partial = insertSteps(document, steps.slice(1, 4), "step-ald");
    expect(partial.steps.every((step) => !step.loop)).toBe(true);
    // Pasting after a step inside the loop lands after the loop.
    const pasted = insertSteps(document, [steps[0]], steps[3].id);
    expect(names(pasted.document)[7]).toBe("Lithography");
    // Duplicating one selected step inside the loop copies it in every iteration.
    const duplicated = duplicateSteps(document, [steps[3].id]);
    expect(names(duplicated.document).filter((name) => name === "Trench Etch copy")).toHaveLength(3);
    // Deleting the loop's steps removes all of them.
    expect(names(removeSteps(document, [steps[1].id, steps[2].id]))).toEqual(["Lithography", "Conformal Al2O3"]);
  });
});
