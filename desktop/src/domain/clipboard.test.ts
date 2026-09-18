import { describe, expect, it } from "vitest";
import { demoDocument } from "../bridge/browserBridge";

import { CLIP_KIND, clipFrom, parseClip, pasteClip } from "./clipboard";
import { getSteps, updateStepParameters } from "./project";
import type { QuickSketch, WorkspaceDocument } from "../types";

function withSketch(document: WorkspaceDocument, sketch: QuickSketch): WorkspaceDocument {
  return { ...document, sketches: [...document.sketches, sketch] };
}

const drawing = (id: string, radius: number): QuickSketch => ({
  id,
  name: "holes",
  shapes: [
    {
      kind: "circle",
      operation: "merge",
      parameters: { center: [0, 0], radius },
      array: [1, 1, 0, 0],
    } as QuickSketch["shapes"][number],
  ],
});

describe("copying steps between projects", () => {
  it("carries what the steps name, not just the steps", () => {
    const document = demoDocument();
    const steps = getSteps(document);
    const clip = clipFrom(document, [steps[0].id, steps[1].id])!;

    expect(clip.kind).toBe(CLIP_KIND);
    expect(clip.steps.map((step) => step.id)).toEqual([steps[0].id, steps[1].id]);
    // Every material the two steps name is in the payload, and nothing else.
    const named = new Set(
      clip.steps.flatMap((step) => [
        ...(step.outputMaterial ? [step.outputMaterial] : []),
        ...Object.keys(step.materialResponses),
      ]),
    );
    expect(new Set(clip.materials.map((item) => item.name))).toEqual(named);
    expect(clip.project).toBe(document.project.name);

    expect(clipFrom(document, [])).toBeNull();
    expect(clipFrom(document, ["nothing-like-this"])).toBeNull();
  });

  it("is JSON anything can read, and nothing else is mistaken for it", () => {
    const document = demoDocument();
    const clip = clipFrom(document, [getSteps(document)[0].id])!;
    const read = parseClip(JSON.stringify(clip))!;
    expect(read.steps).toEqual(clip.steps);
    expect(read.materials).toEqual(clip.materials);

    expect(parseClip("just some text")).toBeNull();
    expect(parseClip("{}")).toBeNull();
    expect(parseClip(JSON.stringify({ kind: CLIP_KIND, steps: [] }))).toBeNull();
    // What an older build wrote: a bare array of steps, with no libraries.
    expect(parseClip(JSON.stringify(clip.steps))).toBeNull();
  });

  it("adds the materials the receiving project is missing", () => {
    const source = demoDocument();
    const step = getSteps(source).find((item) => item.outputMaterial)!;
    const clip = clipFrom(source, [step.id])!;

    const empty: WorkspaceDocument = { ...source, materials: [], tools: [] };
    const pasted = pasteClip(empty, clip, undefined);

    expect(pasted.addedMaterials).toContain(step.outputMaterial);
    expect(pasted.document.materials.map((item) => item.name)).toContain(step.outputMaterial);
    expect(getSteps(pasted.document).at(-1)!.outputMaterial).toBe(step.outputMaterial);
  });

  it("never overwrites a definition the receiving project already has", () => {
    const source = demoDocument();
    const step = getSteps(source).find((item) => item.outputMaterial)!;
    const clip = clipFrom(source, [step.id])!;

    const mine = { ...clip.materials[0], color: "#123456" };
    const target: WorkspaceDocument = { ...source, materials: [mine] };
    const pasted = pasteClip(target, clip, undefined);

    expect(pasted.addedMaterials).toEqual([]);
    expect(pasted.document.materials.find((item) => item.name === mine.name)!.color).toBe("#123456");
  });

  it("brings a sketch over, and keeps one that is only the same by id apart", () => {
    const source = withSketch(demoDocument(), drawing("sketch-1", 0.2));
    const step = updateStepParameters(source, getSteps(source)[0].id, { sketch_id: "sketch-1" });
    const clip = clipFrom(step, [getSteps(step)[0].id])!;
    expect(clip.sketches.map((item) => item.id)).toEqual(["sketch-1"]);

    // A project without it gets it as it is, ready to be saved.
    const fresh = pasteClip({ ...source, sketches: [] }, clip, undefined);
    expect(fresh.newSketches.map((item) => item.id)).toEqual(["sketch-1"]);
    expect(getSteps(fresh.document).at(-1)!.parameters.sketch_id).toBe("sketch-1");

    // A project holding the same id with a different drawing keeps its own,
    // and the pasted step points at a copy so it draws what it drew.
    const clash = pasteClip(withSketch({ ...source, sketches: [] }, drawing("sketch-1", 0.9)), clip, undefined);
    expect(clash.newSketches).toHaveLength(1);
    expect(clash.newSketches[0].id).not.toBe("sketch-1");
    expect(getSteps(clash.document).at(-1)!.parameters.sketch_id).toBe(clash.newSketches[0].id);
    expect(clash.document.sketches.find((item) => item.id === "sketch-1")!.shapes).toEqual(
      drawing("sketch-1", 0.9).shapes,
    );

    // The same drawing under the same id is the same sketch: nothing to save.
    const same = pasteClip(source, clip, undefined);
    expect(same.newSketches).toEqual([]);
  });
});
