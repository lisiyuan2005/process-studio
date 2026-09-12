import { Check, Copy, TerminalSquare, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { ProcessStep, SectionLine, WorkerCapabilities } from "../types";

interface CliPanelProps {
  cli: WorkerCapabilities["cli"] | undefined;
  root: string;
  steps: ProcessStep[];
  selectedStepId: string;
  sectionLine: SectionLine | null;
  onClose: () => void;
}

interface CliEntry {
  label: string;
  args: string[];
}

const WINDOWS_PATH = /^[A-Za-z]:[\\/]|\\/;

/** Quote one argument so PowerShell, cmd and a POSIX shell all read it as one word. */
function quote(argument: string): string {
  if (argument === "") return '""';
  if (/^[A-Za-z0-9_@%+=:,./\\-]+$/.test(argument)) return argument;
  return `"${argument.replace(/"/g, '\\"')}"`;
}

/** The command line for ``args``, run through the worker's own entry point. */
export function commandLine(
  program: string[],
  root: string,
  args: string[],
  windows: boolean,
): string {
  const words = [...program, "--root", root, ...args].map(quote);
  // PowerShell runs a quoted program path only behind the call operator.
  if (windows && words[0].startsWith('"')) words[0] = `& ${words[0]}`;
  return words.join(" ");
}

/** What the desktop is looking at, as command lines that do the same thing. */
export function cliEntries(
  steps: ProcessStep[],
  selectedStepId: string,
  sectionLine: SectionLine | null,
): CliEntry[] {
  const index = steps.findIndex((step) => step.id === selectedStepId);
  const stepArgs = index >= 0 ? ["--step", String(index + 1)] : [];
  const section = sectionLine
    ? ["--named", sectionLine.name]
    : ["--axis", "y", "--at", "0"];
  return [
    { label: "Step list and status", args: ["status"] },
    { label: "Run every step; results still valid are reused", args: ["run"] },
    ...(index >= 0
      ? [{ label: `Run through step ${index + 1} only`, args: ["run", "--through", String(index + 1)] }]
      : []),
    {
      label: sectionLine ? `Section along "${sectionLine.name}" as PNG` : "Section along y = 0 as PNG",
      args: ["view", "section", ...stepArgs, ...section, "-o", "section.png"],
    },
    { label: "Top view as PNG", args: ["view", "top", ...stepArgs, "-o", "top.png"] },
    { label: "3D surfaces as glTF (also .obj, .stl, .ply)", args: ["view", "mesh", ...stepArgs, "-o", "step.glb"] },
    { label: "Write the whole flow to one file", args: ["flow", "dump", "flow.json"] },
    { label: "Set the workspace from a flow file", args: ["flow", "apply", "flow.json"] },
  ];
}

async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch {
    // Fall through: an older webview, or the page is not a secure context.
  }
  const scratch = document.createElement("textarea");
  scratch.value = text;
  scratch.setAttribute("readonly", "");
  scratch.style.position = "fixed";
  scratch.style.opacity = "0";
  document.body.appendChild(scratch);
  scratch.select();
  document.execCommand("copy");
  document.body.removeChild(scratch);
}

/** The command-line equivalents of the open workspace, ready to paste. */
export function CliPanel({ cli, root, steps, selectedStepId, sectionLine, onClose }: CliPanelProps) {
  const [copied, setCopied] = useState<number | null>(null);
  useEffect(() => {
    if (copied === null) return;
    const timer = window.setTimeout(() => setCopied(null), 1600);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const windows = WINDOWS_PATH.test(root);
  const program = cli?.command ?? ["process-studio"];
  const entries = cliEntries(steps, selectedStepId, sectionLine);
  const lines = entries.map((entry) => commandLine(program, root, entry.args, windows));

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Command line">
      <div className="modal-card cli-modal">
        <header className="modal-header">
          <div>
            <span className="eyebrow">COMMAND LINE</span>
            <h2>This workspace from a shell</h2>
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>
        <div className="cli-body">
          <p className="cli-note">
            <TerminalSquare size={13} />
            {cli?.packaged
              ? "The worker of this build is also the command-line tool: with arguments it runs the command instead of serving the desktop. Every command reads and writes the same workspace the desktop has open."
              : "This build runs from a source checkout; the commands use the interpreter the worker runs on. Every command reads and writes the same workspace the desktop has open."}
          </p>
          {entries.map((entry, index) => (
            <div key={entry.label} className="cli-row">
              <div>
                <small>{entry.label}</small>
                <code>{lines[index]}</code>
              </div>
              <button
                type="button"
                className="secondary-button"
                title="Copy this command"
                onClick={() => copyText(lines[index]).then(() => setCopied(index))}
              >
                {copied === index ? <Check size={13} /> : <Copy size={13} />}
                {copied === index ? "Copied" : "Copy"}
              </button>
            </div>
          ))}
          <p className="cli-footnote">
            Output files land in the directory the shell is in. <code>--json</code> after the program
            prints results as JSON; <code>steps show N</code>, <code>steps set</code> and the other
            commands are listed in docs/CLI.md.
          </p>
        </div>
      </div>
    </div>
  );
}
