import { Check, Copy, Play, Square, TerminalSquare, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { CliResult, ProcessStep, SectionLine, WorkerCapabilities } from "../types";

interface CliPanelProps {
  cli: WorkerCapabilities["cli"] | undefined;
  root: string;
  steps: ProcessStep[];
  selectedStepId: string;
  sectionLine: SectionLine | null;
  busy: boolean;
  /** Run one command line (without the program) against the open workspace. */
  onRun: (argv: string[], stdin?: string) => Promise<CliResult>;
  onStop: () => void;
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

/** Split one line the way a shell would: double and single quotes group words. */
export function tokenize(line: string): string[] {
  const words: string[] = [];
  let word = "";
  let quoteChar: '"' | "'" | null = null;
  let open = false;
  for (let i = 0; i < line.length; i++) {
    const char = line[i];
    if (quoteChar) {
      if (char === "\\" && quoteChar === '"' && i + 1 < line.length) {
        word += line[++i];
      } else if (char === quoteChar) {
        quoteChar = null;
      } else {
        word += char;
      }
    } else if (char === '"' || char === "'") {
      quoteChar = char;
      open = true;
    } else if (/\s/.test(char)) {
      if (word || open) words.push(word);
      word = "";
      open = false;
    } else {
      word += char;
    }
  }
  if (word || open) words.push(word);
  return words;
}

const COMMANDS = new Set([
  "new", "kernels", "info", "status", "steps", "run", "window", "view", "materials",
  "templates", "recipes", "tools", "sketch", "lines", "flow", "log", "rpc",
]);

/**
 * The CLI arguments in a pasted line: the program (`process-studio`, the
 * worker binary, `python -m process_studio.cli`, a PowerShell `&`) and any
 * `--root` are dropped, since the console runs against the open workspace;
 * `--json` and `-q` before the command are kept.
 */
export function stripProgram(tokens: string[]): string[] {
  const at = tokens.findIndex((token) => COMMANDS.has(token));
  if (at < 0) return tokens;
  const globals: string[] = [];
  for (let i = 0; i < at; i++) {
    if (tokens[i] === "--json" || tokens[i] === "-q" || tokens[i] === "--quiet") globals.push(tokens[i]);
    if (tokens[i] === "--root") i++;
  }
  return [...globals, ...tokens.slice(at)];
}

export type Script =
  | { kind: "flow"; text: string }
  | { kind: "commands"; lines: string[][] };

/**
 * What was pasted: a whole flow file (JSON or YAML, applied with
 * `flow apply -`) or command lines, one per line; `#` starts a comment.
 */
export function parseScript(text: string): Script {
  const trimmed = text.trim();
  if (trimmed.startsWith("{")) return { kind: "flow", text: trimmed };
  const lines = trimmed
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
  const looksLikeYaml =
    lines.some((line) => /^(name|kernel|window|steps|materials|sketches):/.test(line)) &&
    !lines.some((line) => COMMANDS.has(tokenize(line)[0] ?? ""));
  if (looksLikeYaml) return { kind: "flow", text: trimmed };
  return { kind: "commands", lines: lines.map((line) => stripProgram(tokenize(line))) };
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

const PLACEHOLDER = `Paste commands, one per line, or a whole flow file (JSON or YAML):
steps add deposit --name "TiN liner" --material TiN --set target=0.02 --set mode=conformal
run
# a pasted flow file is applied with "flow apply -" and then run`;

/** The command-line equivalents of the open workspace, and a console to run more. */
export function CliPanel({
  cli, root, steps, selectedStepId, sectionLine, busy, onRun, onStop, onClose,
}: CliPanelProps) {
  const [copied, setCopied] = useState<number | null>(null);
  const [script, setScript] = useState("");
  const [runAfterApply, setRunAfterApply] = useState(true);
  const [transcript, setTranscript] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const outputRef = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (copied === null) return;
    const timer = window.setTimeout(() => setCopied(null), 1600);
    return () => window.clearTimeout(timer);
  }, [copied]);
  useEffect(() => {
    outputRef.current?.scrollTo({ top: outputRef.current.scrollHeight });
  }, [transcript]);

  const windows = WINDOWS_PATH.test(root);
  const program = cli?.command ?? ["process-studio"];
  const entries = cliEntries(steps, selectedStepId, sectionLine);
  const lines = entries.map((entry) => commandLine(program, root, entry.args, windows));

  const say = (text: string) => setTranscript((current) => [...current, text]);
  const runOne = async (argv: string[], stdin?: string): Promise<boolean> => {
    say(`$ ${argv.map(quote).join(" ")}`);
    const result = await onRun(argv, stdin);
    const output = [result.stdout, result.stderr].filter(Boolean).join("").trimEnd();
    if (output) say(output);
    if (result.exitCode !== 0) say(`exit code ${result.exitCode}`);
    return result.exitCode === 0;
  };
  const runScript = async () => {
    const parsed = parseScript(script);
    if (parsed.kind === "commands" && parsed.lines.length === 0) return;
    setRunning(true);
    try {
      if (parsed.kind === "flow") {
        if ((await runOne(["flow", "apply", "-"], parsed.text)) && runAfterApply) await runOne(["run"]);
      } else {
        for (const argv of parsed.lines) {
          if (argv.length === 0) continue;
          if (!(await runOne(argv))) break;
        }
      }
    } catch (reason) {
      say(`error: ${reason instanceof Error ? reason.message : String(reason)}`);
    } finally {
      setRunning(false);
    }
  };

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
          <section className="cli-console">
            <div className="cli-console-head">
              <span className="section-label">RUN HERE</span>
              <label className="cli-check">
                <input
                  type="checkbox"
                  checked={runAfterApply}
                  onChange={(event) => setRunAfterApply(event.target.checked)}
                />
                Run the flow after applying a pasted flow file
              </label>
              {running || busy ? (
                <button type="button" className="secondary-button" onClick={onStop} disabled={!running}>
                  <Square size={12} />
                  Stop
                </button>
              ) : (
                <button
                  type="button"
                  className="primary-button cli-run"
                  disabled={!script.trim()}
                  onClick={() => void runScript()}
                >
                  <Play size={12} />
                  Run
                </button>
              )}
            </div>
            <textarea
              className="cli-script"
              rows={5}
              spellCheck={false}
              placeholder={PLACEHOLDER}
              value={script}
              disabled={running}
              onChange={(event) => setScript(event.target.value)}
              onKeyDown={(event) => {
                if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
                  event.preventDefault();
                  if (!running && !busy && script.trim()) void runScript();
                }
              }}
            />
            <pre className="cli-output" ref={outputRef}>
              {transcript.length === 0
                ? "Output appears here. Ctrl+Enter runs. Every command works the open workspace, and the desktop follows: steps, materials and results update as they run."
                : transcript.join("\n")}
            </pre>
          </section>
          <p className="cli-note">
            <TerminalSquare size={13} />
            {cli?.packaged
              ? "The worker of this build is also the command-line tool: with arguments it runs the command instead of serving the desktop. These lines do from a terminal what the desktop is showing."
              : "These commands use the interpreter this build's worker runs on, with -m process_studio.cli. These lines do from a terminal what the desktop is showing."}
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
