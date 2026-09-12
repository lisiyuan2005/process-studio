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

import { parseScript, stripProgram, tokenize } from "./CliPanel";

describe("tokenize", () => {
  it("groups quoted words and keeps escaped quotes", () => {
    expect(tokenize('steps add deposit --name "TiN liner" --set target=0.02')).toEqual([
      "steps", "add", "deposit", "--name", "TiN liner", "--set", "target=0.02",
    ]);
    expect(tokenize(`lines add 'Across the stairs' 0 0 1 0`)).toEqual([
      "lines", "add", "Across the stairs", "0", "0", "1", "0",
    ]);
    expect(tokenize('view section --named "say \\"hi\\"" -o a.png')).toEqual([
      "view", "section", "--named", 'say "hi"', "-o", "a.png",
    ]);
  });
});

describe("stripProgram", () => {
  it("drops the program and the root but keeps global flags", () => {
    expect(stripProgram(tokenize("process-studio --root /x --json status"))).toEqual(["--json", "status"]);
    expect(stripProgram(tokenize("/usr/bin/python -m process_studio.cli --root /x run"))).toEqual(["run"]);
    expect(stripProgram(tokenize('& "C:\\Program Files\\PS\\worker.exe" --root C:\\nand -q run'))).toEqual([
      "-q", "run",
    ]);
    expect(stripProgram(tokenize("steps list"))).toEqual(["steps", "list"]);
  });
});

describe("parseScript", () => {
  it("takes a JSON or YAML flow as a flow file", () => {
    expect(parseScript('  {"name": "x", "steps": []} ')).toEqual({ kind: "flow", text: '{"name": "x", "steps": []}' });
    const yaml = "name: 1T1C\nkernel: slab\nsteps:\n  - {name: A, type: cmp}";
    expect(parseScript(yaml)).toEqual({ kind: "flow", text: yaml });
  });

  it("takes anything else as command lines, skipping comments and blanks", () => {
    expect(parseScript("# set up\nsteps add cmp --name Polish\n\nprocess-studio --root /x run\n")).toEqual({
      kind: "commands",
      lines: [["steps", "add", "cmp", "--name", "Polish"], ["run"]],
    });
  });
});
