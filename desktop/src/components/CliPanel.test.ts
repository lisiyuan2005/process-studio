import { describe, expect, it } from "vitest";
import { cliEntries, commandLine } from "./CliPanel";
import type { ProcessStep } from "../types";

const step = (id: string): ProcessStep =>
  ({ id, name: id, processType: "deposit", tool: "", outputMaterial: null, parameters: {} }) as ProcessStep;

describe("commandLine", () => {
  it("quotes only what a shell would split", () => {
    expect(commandLine(["process-studio"], "/devices/dram", ["run"], false)).toBe(
      "process-studio --root /devices/dram run",
    );
    expect(commandLine(["/opt/py thon"], "/my devices/dram", ["status"], false)).toBe(
      '"/opt/py thon" --root "/my devices/dram" status',
    );
  });

  it("calls a quoted program path the PowerShell way", () => {
    expect(
      commandLine(["C:\\Program Files\\PS\\worker.exe"], "C:\\all\\NAND", ["run"], true),
    ).toBe('& "C:\\Program Files\\PS\\worker.exe" --root C:\\all\\NAND run');
  });
});

describe("cliEntries", () => {
  it("names the selected step and the active section line", () => {
    const entries = cliEntries([step("a"), step("b")], "b", {
      id: "l", name: "Across the staircase", start: [0, 0], end: [1, 0],
    });
    expect(entries.find((entry) => entry.args[0] === "run" && entry.args[1] === "--through")?.args).toEqual([
      "run", "--through", "2",
    ]);
    expect(entries.find((entry) => entry.args[1] === "section")?.args).toEqual([
      "view", "section", "--step", "2", "--named", "Across the staircase", "-o", "section.png",
    ]);
  });

  it("falls back to the last step and y = 0 when nothing is selected", () => {
    const entries = cliEntries([step("a")], "", null);
    expect(entries.some((entry) => entry.args[1] === "--through")).toBe(false);
    expect(entries.find((entry) => entry.args[1] === "section")?.args).toEqual([
      "view", "section", "--axis", "y", "--at", "0", "-o", "section.png",
    ]);
  });
});
