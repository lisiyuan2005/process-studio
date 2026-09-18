import { describe, expect, it } from "vitest";
import { libraryOf, sameLibrary, withLibrary } from "./library";
import type { WorkspaceDocument } from "../types";

function documentWith(color: string): WorkspaceDocument {
  return {
    root: "/tmp/one",
    project: {} as WorkspaceDocument["project"],
    branches: [],
    recipes: [{ id: "r", name: "Etch" } as WorkspaceDocument["recipes"][number]],
    tools: [{ id: "t", name: "ICP" } as WorkspaceDocument["tools"][number]],
    materials: [{ id: "m", name: "Si", category: "", color, opacity: 1 }],
    sketches: [],
    stepStatuses: {},
  };
}

describe("the library every tab shares", () => {
  it("is the three lists a document carries", () => {
    const library = libraryOf(documentWith("#111111"));
    expect(library.materials[0].color).toBe("#111111");
    expect(library.tools).toHaveLength(1);
    expect(library.recipes).toHaveLength(1);
  });

  it("tells an edit from the same thing again", () => {
    const mine = libraryOf(documentWith("#111111"));
    expect(sameLibrary(mine, libraryOf(documentWith("#111111")))).toBe(true);
    expect(sameLibrary(mine, libraryOf(documentWith("#222222")))).toBe(false);
  });

  it("takes another tab's library without touching the rest of the document", () => {
    const document = { ...documentWith("#111111"), sketches: [{ id: "s", name: "s", shapes: [] }] };
    const taken = withLibrary(document, libraryOf(documentWith("#222222")));
    expect(taken.materials[0].color).toBe("#222222");
    expect(taken.sketches).toBe(document.sketches);
    expect(taken.root).toBe("/tmp/one");
  });
});
