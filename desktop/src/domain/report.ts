import type { ReportResult } from "../types";

/** Where a problem is reported: the repository's own issue form. */
export const ISSUES_URL = "https://github.com/lisiyuan2005/process-studio/issues/new";

/**
 * The longest issue address handed to the browser. Windows opens a link
 * through the shell, which gives up somewhere past 2 000 characters; the
 * whole description is in the report anyway, so the page only needs enough
 * to say what is wrong.
 */
const MAX_URL = 1900;

export interface IssueDetails {
  description: string;
  flow: "simulation" | "experiment";
  branch: string;
  version: string;
  kernel: string;
  marked: ReportResult["markedSteps"];
  /** The report's file name, for the line that asks for it to be attached. */
  fileName: string;
}

function title(details: IssueDetails): string {
  const first = details.description.trim().split(/\r?\n/)[0]?.trim() ?? "";
  if (first) return first.length > 80 ? `${first.slice(0, 77)}…` : first;
  const step = details.marked[0];
  return step ? `Problem at step ${step.index}: ${step.name}` : "Problem report";
}

function body(details: IssueDetails, description: string): string {
  const where = details.flow === "experiment" ? "the experiment record" : `the branch ${details.branch}`;
  const steps = details.marked.length
    ? details.marked
        .map((step) => `- ${step.index}. ${step.name} (${step.type}${step.loop ? `, ${step.loop.name} ${step.loop.iteration + 1}/${step.loop.repeat}` : ""})`)
        .join("\n")
    : "- (none marked)";
  return [
    "**What went wrong**",
    description || "(no description)",
    "",
    `**Steps marked** in ${where}`,
    steps,
    "",
    `**Report**: please drag \`${details.fileName}\` into this box before submitting. It holds the flow, the marked steps and the log.`,
    "",
    `Process Studio ${details.version} · ${details.kernel} kernel`,
  ].join("\n");
}

/** The issue form, filled in; the description is cut short to fit a link. */
export function issueUrl(details: IssueDetails): string {
  const build = (description: string) =>
    `${ISSUES_URL}?title=${encodeURIComponent(title(details))}&body=${encodeURIComponent(body(details, description))}`;
  let description = details.description.trim();
  let url = build(description);
  while (url.length > MAX_URL && description.length > 0) {
    description = description.slice(0, Math.max(0, Math.floor(description.length * 0.8) - 1));
    url = build(`${description}… (the rest is in the report)`);
  }
  return url;
}

/** A file name from a path, whichever separator it uses. */
export function baseName(path: string): string {
  return path.split(/[\\/]/).pop() || path;
}
