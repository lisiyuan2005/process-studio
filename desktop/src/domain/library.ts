/**
 * The materials, tools and recipes, which every tab shares.
 *
 * The worker keeps one library for the user rather than one per project,
 * so two tabs showing two projects are showing the same materials. If they
 * each kept their own copy of them, editing a colour in one tab and then
 * doing anything at all in the other would write the old colour back over
 * it -- the other tab's autosave sends the whole document, library and all.
 *
 * So the shell holds what a tab last reported and hands it to the rest.
 */
import type { MaterialDefinition, Recipe, ToolDefinition, WorkspaceDocument } from "../types";

export interface Library {
  materials: MaterialDefinition[];
  tools: ToolDefinition[];
  recipes: Recipe[];
}

export function libraryOf(document: WorkspaceDocument): Library {
  return {
    materials: document.materials,
    tools: document.tools,
    recipes: document.recipes,
  };
}

/** Whether two libraries hold the same thing, values and order alike. */
export function sameLibrary(a: Library, b: Library): boolean {
  const same = <T>(left: T[], right: T[]) =>
    left === right || JSON.stringify(left) === JSON.stringify(right);
  return (
    same(a.materials, b.materials) && same(a.tools, b.tools) && same(a.recipes, b.recipes)
  );
}

export function withLibrary(document: WorkspaceDocument, library: Library): WorkspaceDocument {
  return {
    ...document,
    materials: library.materials,
    tools: library.tools,
    recipes: library.recipes,
  };
}
