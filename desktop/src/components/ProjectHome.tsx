import { ArrowRight, Boxes, FolderOpen, Layers3, Plus } from "lucide-react";
import { useState } from "react";

interface ProjectHomeProps {
  runtime: "tauri" | "browser";
  busy: boolean;
  error?: string;
  onCreate: (name: string) => void;
  onOpen: () => void;
}

export function ProjectHome({ runtime, busy, error, onCreate, onOpen }: ProjectHomeProps) {
  const [name, setName] = useState("Process Studio Project");

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
                if (event.key === "Enter" && name.trim() && !busy) onCreate(name.trim());
              }}
            />
          </label>
          <button
            type="button"
            className="primary-button home-primary"
            disabled={busy || !name.trim()}
            onClick={() => onCreate(name.trim())}
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
