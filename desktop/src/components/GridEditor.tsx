import { TriangleAlert, X } from "lucide-react";
import { useState } from "react";
import type { GridDefinition } from "../types";

interface GridEditorProps {
  grid: GridDefinition;
  busy: boolean;
  onApply: (grid: GridDefinition) => void;
  onClose: () => void;
}

type Editable = Omit<GridDefinition, "spacingUm" | "nodeCount">;

const BOUNDS: Array<[keyof Editable, string]> = [
  ["xMin", "x min"],
  ["xMax", "x max"],
  ["yMin", "y min"],
  ["yMax", "y max"],
  ["zMin", "z min"],
  ["zMax", "z max"],
];

const COUNTS: Array<[keyof Editable, string]> = [
  ["nx", "nx"],
  ["ny", "ny"],
  ["nz", "nz"],
];

function spacing(value: Editable) {
  return {
    dx: (value.xMax - value.xMin) / Math.max(1, value.nx - 1),
    dy: (value.yMax - value.yMin) / Math.max(1, value.ny - 1),
    dz: (value.zMax - value.zMin) / Math.max(1, value.nz - 1),
  };
}

export function GridEditor({ grid, busy, onApply, onClose }: GridEditorProps) {
  const [draft, setDraft] = useState<Editable>({
    xMin: grid.xMin,
    xMax: grid.xMax,
    yMin: grid.yMin,
    yMax: grid.yMax,
    zMin: grid.zMin,
    zMax: grid.zMax,
    nx: grid.nx,
    ny: grid.ny,
    nz: grid.nz,
  });

  const { dx, dy, dz } = spacing(draft);
  const nodes = draft.nx * draft.ny * draft.nz;
  const equalSpacing =
    Math.abs(dx - dy) < 1e-9 && Math.abs(dx - dz) < 1e-9 && Number.isFinite(dx) && dx > 0;
  const validCounts = draft.nx >= 3 && draft.ny >= 3 && draft.nz >= 3;
  const validBounds = draft.xMax > draft.xMin && draft.yMax > draft.yMin && draft.zMax > draft.zMin;
  const valid = equalSpacing && validCounts && validBounds;

  const field = (key: keyof Editable, label: string, integer: boolean) => (
    <label className="field-row" key={key}>
      <span>{label}</span>
      <input
        type="number"
        step={integer ? 1 : 0.01}
        value={draft[key]}
        onChange={(event) => {
          const value = Number(event.target.value);
          if (Number.isFinite(value)) {
            setDraft({ ...draft, [key]: integer ? Math.round(value) : value });
          }
        }}
      />
    </label>
  );

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Grid">
      <div className="modal-card grid-modal">
        <header className="modal-header">
          <div>
            <span className="eyebrow">NUMERICS</span>
            <h2>Simulation grid</h2>
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <div className="modal-body">
          <span className="section-label">DOMAIN (µm)</span>
          <div className="grid-grid">{BOUNDS.map(([key, label]) => field(key, label, false))}</div>
          <span className="section-label" style={{ marginTop: 16 }}>
            NODES
          </span>
          <div className="grid-grid">{COUNTS.map(([key, label]) => field(key, label, true))}</div>

          <dl className="grid-summary">
            <dt>Spacing x / y / z</dt>
            <dd>
              {(dx * 1000).toFixed(2)} / {(dy * 1000).toFixed(2)} / {(dz * 1000).toFixed(2)} nm
            </dd>
            <dt>Total nodes</dt>
            <dd>{nodes.toLocaleString()}</dd>
            <dt>Memory per material field</dt>
            <dd>{((nodes * 8) / 1024 ** 2).toFixed(1)} MiB</dd>
          </dl>

          {!equalSpacing && (
            <div className="error-box">
              <TriangleAlert size={13} />
              <span>
                The kernel requires equal x, y and z spacing. Adjust the bounds or node counts
                until the three match.
              </span>
            </div>
          )}
          {!validCounts && (
            <div className="error-box">
              <TriangleAlert size={13} />
              <span>Each axis needs at least three nodes.</span>
            </div>
          )}
          {!validBounds && (
            <div className="error-box">
              <TriangleAlert size={13} />
              <span>Every axis needs a positive extent.</span>
            </div>
          )}

          <div className="warning-box">
            <TriangleAlert size={13} />
            <span>
              Changing the grid discards every stored result: the flow is replayed from the bare
              wafer on the new grid rather than interpolating the old one. A finer grid costs
              memory for one double-precision field per material plus solver temporaries.
            </span>
          </div>
        </div>

        <div className="modal-actions">
          <span>{valid ? "Ready to apply" : "Fix the highlighted values first"}</span>
          <button type="button" className="secondary-button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="primary-button modal-save"
            disabled={!valid || busy}
            onClick={() =>
              onApply({ ...draft, spacingUm: dx, nodeCount: nodes })
            }
          >
            Apply grid
          </button>
        </div>
      </div>
    </div>
  );
}
