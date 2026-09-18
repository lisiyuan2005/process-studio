/**
 * Copying steps, including to another project.
 *
 * A step is not self-contained: it names a tool, an output material, a
 * material per response, and sometimes a Quick Sketch. Pasted into the
 * project it came from those are all there; pasted into another one they
 * may be missing, and a step naming a material the project does not have
 * cannot run. So what goes on the clipboard is the steps *and* the
 * definitions they name, and pasting adds whatever the receiving project
 * lacks. (Recipes are not among them: a recipe is a template that was
 * loaded into the step, and the step carries the result.)
 *
 * The payload is JSON on the system clipboard, so it survives between
 * tabs, between windows, and out to anything else that reads text -- a
 * script, a diff, a message to a colleague. A copy is kept here as well,
 * because reading the system clipboard can be refused and because plain
 * text someone else copied must not look like an empty clipboard.
 */
import type {
  MaterialDefinition,
  ProcessStep,
  QuickSketch,
  ToolDefinition,
  WorkspaceDocument,
} from "../types";
import { getSteps, insertSteps, newId, upsertMaterial, upsertTool } from "./project";

export const CLIP_KIND = "process-studio/steps";

export interface StepClip {
  kind: typeof CLIP_KIND;
  version: 1;
  /** Where it was copied from, for the person reading the JSON. */
  project?: string;
  steps: ProcessStep[];
  materials: MaterialDefinition[];
  tools: ToolDefinition[];
  sketches: QuickSketch[];
}

/** The steps named by ``ids``, in flow order, with what they refer to. */
export function clipFrom(document: WorkspaceDocument, ids: string[]): StepClip | null {
  const wanted = new Set(ids);
  const steps = getSteps(document).filter((step) => wanted.has(step.id));
  if (steps.length === 0) return null;

  const materialNames = new Set<string>();
  const toolNames = new Set<string>();
  const sketchIds = new Set<string>();
  for (const step of steps) {
    if (step.outputMaterial) materialNames.add(step.outputMaterial);
    Object.keys(step.materialResponses).forEach((name) => materialNames.add(name));
    if (step.tool) toolNames.add(step.tool);
    const sketch = step.parameters.sketch_id;
    if (typeof sketch === "string" && sketch) sketchIds.add(sketch);
  }
  return {
    kind: CLIP_KIND,
    version: 1,
    project: document.project.name,
    steps,
    materials: document.materials.filter((item) => materialNames.has(item.name)),
    tools: document.tools.filter((item) => toolNames.has(item.name)),
    sketches: document.sketches.filter((item) => sketchIds.has(item.id)),
  };
}

/** Whether some text is one of these payloads. */
export function parseClip(text: string): StepClip | null {
  try {
    const parsed = JSON.parse(text) as StepClip;
    if (parsed?.kind !== CLIP_KIND || !Array.isArray(parsed.steps) || parsed.steps.length === 0) {
      return null;
    }
    return {
      kind: CLIP_KIND,
      version: 1,
      project: typeof parsed.project === "string" ? parsed.project : undefined,
      steps: parsed.steps,
      materials: Array.isArray(parsed.materials) ? parsed.materials : [],
      tools: Array.isArray(parsed.tools) ? parsed.tools : [],
      sketches: Array.isArray(parsed.sketches) ? parsed.sketches : [],
    };
  } catch {
    return null;
  }
}

/** What a paste has to do besides inserting the steps. */
export interface Pasted {
  document: WorkspaceDocument;
  steps: ProcessStep[];
  /** Sketches the receiving project did not have; they need saving. */
  newSketches: QuickSketch[];
  /** What was added to the libraries, for the log. */
  addedMaterials: string[];
  addedTools: string[];
}

/**
 * Insert a clipboard's steps after ``afterStepId``, bringing in whatever
 * the receiving project is missing.
 *
 * A definition already there wins: pasting must not quietly change the
 * colour of the receiving project's oxide, or a tool's throughput. A
 * sketch is matched by id *and* content -- the same id holding a different
 * drawing is a different sketch, and gets an id of its own here so the
 * pasted step draws what it drew before.
 */
export function pasteClip(
  document: WorkspaceDocument,
  clip: StepClip,
  afterStepId: string | undefined,
): Pasted {
  let next = document;
  const addedMaterials: string[] = [];
  const addedTools: string[] = [];

  const haveMaterial = new Set(document.materials.map((item) => item.name));
  for (const material of clip.materials) {
    if (haveMaterial.has(material.name)) continue;
    next = upsertMaterial(next, { ...material, id: newId("material") });
    addedMaterials.push(material.name);
  }
  const haveTool = new Set(document.tools.map((item) => item.name));
  for (const tool of clip.tools) {
    if (haveTool.has(tool.name)) continue;
    next = upsertTool(next, { ...tool, id: newId("tool") });
    addedTools.push(tool.name);
  }

  const newSketches: QuickSketch[] = [];
  const renamedSketches = new Map<string, string>();
  for (const sketch of clip.sketches) {
    const here = next.sketches.find((item) => item.id === sketch.id);
    if (here && JSON.stringify(here.shapes) === JSON.stringify(sketch.shapes)) continue;
    const copy = here ? { ...sketch, id: newId("sketch"), name: `${sketch.name} (copy)` } : sketch;
    if (here) renamedSketches.set(sketch.id, copy.id);
    next = { ...next, sketches: [...next.sketches, copy] };
    newSketches.push(copy);
  }

  const steps = clip.steps.map((step) => {
    const sketch = step.parameters.sketch_id;
    const moved = typeof sketch === "string" ? renamedSketches.get(sketch) : undefined;
    return moved ? { ...step, parameters: { ...step.parameters, sketch_id: moved } } : step;
  });

  const inserted = insertSteps(next, steps, afterStepId);
  return {
    document: inserted.document,
    steps: inserted.steps,
    newSketches,
    addedMaterials,
    addedTools,
  };
}

/** The last copy made in this window, for when the system clipboard cannot be read. */
let held: StepClip | null = null;

export function rememberClip(clip: StepClip) {
  held = clip;
}

export function heldClip(): StepClip | null {
  return held;
}

/**
 * What to paste: the system clipboard when it holds one of these payloads
 * (so a copy from another window or another tab wins), otherwise the last
 * copy made here.
 */
export async function clipToPaste(): Promise<StepClip | null> {
  try {
    const text = await navigator.clipboard?.readText();
    const parsed = text ? parseClip(text) : null;
    if (parsed) return parsed;
  } catch {
    // reading it can be refused; the copy kept here is the fallback
  }
  return held;
}
