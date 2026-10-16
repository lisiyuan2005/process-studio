import { ArrowDown, ArrowUp, Copy, FlaskConical, GitBranchPlus, Plus, Trash2 } from "lucide-react";
import type { ProcessStep, ProcessType } from "../types";
import { PROCESS_LABELS } from "./StepList";

/**
 * The experiment record's steps: what was done in the lab, in order. Nothing
 * here runs; the record is turned into a simulation branch on request.
 */
export function ExperimentList({
  steps,
  selectedId,
  processTypes,
  accentFor,
  onSelect,
  onAdd,
  onMove,
  onDuplicate,
  onRemove,
  onExport,
  onStartFromFlow,
}: {
  steps: ProcessStep[];
  selectedId: string;
  processTypes: ProcessType[];
  accentFor: (step: ProcessStep) => string;
  onSelect: (id: string) => void;
  onAdd: (type: ProcessType) => void;
  onMove: (id: string, direction: -1 | 1) => void;
  onDuplicate: (id: string) => void;
  onRemove: (id: string) => void;
  onExport: () => void;
  /** Fill an empty record from the simulation flow on screen. */
  onStartFromFlow: () => void;
}) {
  return (
    <aside className="steps-panel experiment-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">EXPERIMENT</span>
          <h2>What was done</h2>
        </div>
        <span className="count-badge">{steps.length}</span>
      </div>
      <div className="step-scroll">
        {steps.length === 0 ? (
          <div className="experiment-empty">
            <FlaskConical size={20} />
            <p>
              The experiment record is a flow of its own: each step with its tool and every setting the
              machine had. It is never run, and nothing in it changes the simulation.
            </p>
            <button type="button" className="secondary-button" onClick={onStartFromFlow}>
              Start from the simulation flow
            </button>
          </div>
        ) : (
          <div className="step-list">
            {steps.map((step, index) => (
              <div
                key={step.id}
                role="button"
                tabIndex={0}
                className={`step-card experiment-card${step.id === selectedId ? " selected" : ""}`}
                onClick={() => onSelect(step.id)}
                onKeyDown={(event) => event.key === "Enter" && onSelect(step.id)}
              >
                <span className="step-index experiment-index">{index + 1}</span>
                <span className="step-accent" style={{ background: accentFor(step) }} />
                <div className="step-copy">
                  <div className="step-title-row">
                    <span className="step-title">{step.name}</span>
                  </div>
                  <div className="step-subtitle">
                    {PROCESS_LABELS[step.processType] ?? step.processType}
                    {step.tool ? ` · ${step.tool}` : ""}
                  </div>
                </div>
                <span className="experiment-actions" onClick={(event) => event.stopPropagation()}>
                  <button type="button" title="Move up" disabled={index === 0} onClick={() => onMove(step.id, -1)}>
                    <ArrowUp size={12} />
                  </button>
                  <button
                    type="button"
                    title="Move down"
                    disabled={index === steps.length - 1}
                    onClick={() => onMove(step.id, 1)}
                  >
                    <ArrowDown size={12} />
                  </button>
                  <button type="button" title="Duplicate" onClick={() => onDuplicate(step.id)}>
                    <Copy size={12} />
                  </button>
                  <button type="button" title="Delete" onClick={() => onRemove(step.id)}>
                    <Trash2 size={12} />
                  </button>
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="add-step-wrap experiment-footer">
        <label className="add-step-control">
          <Plus size={14} />
          <span>Add step</span>
          <select
            aria-label="Add an experiment step"
            value=""
            onChange={(event) => {
              if (event.target.value) onAdd(event.target.value as ProcessType);
              event.target.value = "";
            }}
          >
            <option value="" disabled>
              Choose a process type…
            </option>
            {processTypes.map((type) => (
              <option key={type} value={type}>
                {PROCESS_LABELS[type] ?? type}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="secondary-button export-experiment"
          disabled={steps.length === 0}
          title="A new simulation branch with these steps: thickness from the target, cycles × rate per cycle or time × rate; etches by time and each material's rate. The flows already there are left as they are."
          onClick={onExport}
        >
          <GitBranchPlus size={13} /> Export to a simulation branch
        </button>
      </div>
    </aside>
  );
}
