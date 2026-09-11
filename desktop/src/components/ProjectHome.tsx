import { ArrowRight, Boxes, Clock3, FolderOpen, Layers3, Lock, Plus, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { RecentWorkspace } from "../domain/recent";
import type { KernelDescription } from "../types";

interface ProjectHomeProps {
  runtime: "tauri" | "browser";
  busy: boolean;
  error?: string;
  kernels: KernelDescription[];
  defaultKernel: string;
  recent: RecentWorkspace[];
  onCreate: (name: string, kernel: string) => void;
  onOpen: () => void;
  onOpenRecent: (root: string) => void;
  onForgetRecent: (root: string) => void;
}

export function ProjectHome({
  runtime,
  busy,
  error,
  kernels,
  defaultKernel,
  recent,
  onCreate,
  onOpen,
  onOpenRecent,
  onForgetRecent,
}: ProjectHomeProps) {
  const [name, setName] = useState("Process Studio Project");
  const [kernel, setKernel] = useState(defaultKernel);

  // The kernel list arrives from the worker, so the default may land later.
  useEffect(() => setKernel(defaultKernel), [defaultKernel]);
  const chosen = kernels.find((item) => item.id === kernel);

  return (
    <div className="home-shell">
      <header className="home-header">
        <div className="brand-mark">
          <Layers3 size={21} />
        </div>
        <span>Process Studio</span>
        <span className="version-tag">0.5</span>
      </header>

      <main className="home-main">
        <section className="home-intro">
          <span className="eyebrow">3D PROCESS FLOW WORKBENCH</span>
          <h1>
            Run the flow.
            <br />
            Inspect every step.
          </h1>
          <p>
            Level-set etch, conformal and directional deposition, and ideal CMP over a shared 3D
            material state. Each step keeps its own snapshot, so an edit replays only what changed.
          </p>
          <div className="feature-row">
            <span>
              <Boxes size={14} />
              Multi-material level sets
            </span>
            <span>
              <Layers3 size={14} />
              Per-step snapshots
            </span>
          </div>
        </section>

        <section className="home-card">
          <div className="home-card-heading">
            <span className="card-icon">
              <Plus size={18} />
            </span>
            <div>
              <h2>New workspace</h2>
              <p>Creates a directory holding the project database and its snapshots.</p>
            </div>
          </div>
          <label className="home-field">
            <span>Workspace name</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && name.trim() && !busy) onCreate(name.trim(), kernel);
              }}
            />
          </label>

          {kernels.length === 1 && (
            <p className="kernel-lock">
              <Lock size={12} />
              This build ships the {kernels[0].name} kernel only. New workspaces use it, and a
              workspace made with another kernel needs the build that includes that kernel.
            </p>
          )}

          {kernels.length > 1 && (
            <div className="kernel-choice">
              <span className="section-label">SIMULATION KERNEL</span>
              {kernels.map((item) => (
                <label key={item.id} className={item.id === kernel ? "kernel-card active" : "kernel-card"}>
                  <input
                    type="radio"
                    name="kernel"
                    value={item.id}
                    checked={item.id === kernel}
                    onChange={() => setKernel(item.id)}
                  />
                  <span className="kernel-name">
                    {item.name}
                    <em>{item.version}</em>
                  </span>
                  <span className="kernel-summary">{item.summary}</span>
                </label>
              ))}
              <p className="kernel-lock">
                <Lock size={12} />
                The kernel is part of the project: it cannot be changed once the workspace
                exists, because the two store geometry differently and neither can read the
                other&apos;s results.
              </p>
            </div>
          )}

          <button
            type="button"
            className="primary-button home-primary"
            disabled={busy || !name.trim() || (kernels.length > 0 && !chosen)}
            onClick={() => onCreate(name.trim(), kernel)}
          >
            Create workspace <ArrowRight size={15} />
          </button>
          <div className="home-divider">
            <span>or</span>
          </div>
          <button type="button" className="open-button" disabled={busy} onClick={onOpen}>
            <FolderOpen size={16} />
            Open existing workspace
          </button>
          {recent.length > 0 && (
            <div className="recent-list">
              <span className="section-label">RECENT</span>
              {recent.map((item) => (
                <div key={item.root} className="recent-row">
                  <button
                    type="button"
                    className="recent-open"
                    disabled={busy}
                    title={item.root}
                    onClick={() => onOpenRecent(item.root)}
                  >
                    <Clock3 size={13} />
                    <span className="recent-name">{item.name}</span>
                    <span className="recent-root">{item.root}</span>
                  </button>
                  <button
                    type="button"
                    className="recent-forget"
                    aria-label={`Forget ${item.name}`}
                    title="Remove from this list"
                    onClick={() => onForgetRecent(item.root)}
                  >
                    <X size={12} />
                  </button>
                </div>
              ))}
            </div>
          )}
          {error && <p className="home-error">{error}</p>}
          {runtime === "browser" && (
            <p className="browser-note">
              This browser preview shows the interface with one fixed document. It has no process
              kernel, so runs and views need the desktop shell.
            </p>
          )}
        </section>
      </main>
    </div>
  );
}
