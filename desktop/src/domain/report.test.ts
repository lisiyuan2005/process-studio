import { describe, expect, it } from "vitest";
import { ISSUES_URL, baseName, issueUrl, type IssueDetails } from "./report";

const details: IssueDetails = {
  description: "The metal climbs the resist\nat 40 nm from the wall",
  flow: "simulation",
  branch: "main",
  version: "0.1.5",
  kernel: "slab",
  marked: [{ index: 3, id: "s3", name: "Deposit TiN", type: "deposit", loop: null, status: "clean" }],
  fileName: "demo-report.zip",
};

describe("the issue page", () => {
  it("is the repository's issue form with the problem filled in", () => {
    const url = new URL(issueUrl(details));
    expect(`${url.origin}${url.pathname}`).toBe(ISSUES_URL);
    expect(url.searchParams.get("title")).toBe("The metal climbs the resist");
    const body = url.searchParams.get("body")!;
    expect(body).toContain("at 40 nm from the wall");
    expect(body).toContain("- 3. Deposit TiN (deposit)");
    expect(body).toContain("the branch main");
    expect(body).toContain("demo-report.zip");
    expect(body).toContain("Process Studio 0.1.5");
  });

  it("names a marked step when there is no description, and stays short enough to open", () => {
    const untitled = new URL(issueUrl({ ...details, description: "" }));
    expect(untitled.searchParams.get("title")).toBe("Problem at step 3: Deposit TiN");
    const long = issueUrl({ ...details, description: "线宽不对 ".repeat(400), flow: "experiment" });
    expect(long.length).toBeLessThanOrEqual(1900);
    const body = new URL(long).searchParams.get("body")!;
    expect(body).toContain("the rest is in the report");
    expect(body).toContain("the experiment record");
  });

  it("takes a file name from either kind of path", () => {
    expect(baseName("C:\\Users\\li\\demo-report.zip")).toBe("demo-report.zip");
    expect(baseName("/Users/li/demo-report.zip")).toBe("demo-report.zip");
  });
});
