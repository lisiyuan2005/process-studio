import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  Check,
  ClipboardList,
  CircleAlert,
  Clock3,
  GripVertical,
  Layers3,
  LoaderCircle,
  Minimize2,
  Plus,
  Scissors,
  Sparkles,
  Square,
  SquareCheck,
} from "lucide-react";
import type { ProcessType, ProcessStep, StepStatus } from "../types";

interface StepListProps {
  steps: ProcessStep[];
  statuses: Record<string, StepStatus>;
  accentFor: (step: ProcessStep) => string;
  selectedStepId: string;
  onSelect: (stepId: string) => void;
  onAdd: (processType: ProcessType) => void;
  onToggle: (stepId: string) => void;
  onReorder: (activeId: string, overId: string) => void;
}

const STATUS_LABELS: Record<StepStatus, string> = {
  clean: "Ready",
  stale: "Stale",
  dirty: "Not run",
  running: "Running",
  failed: "Failed",
};

function ProcessIcon({ type }: { type: ProcessType | undefined }) {
  if (type === "etch") return <Scissors size={15} />;
  if (type === "deposit") return <Sparkles size={15} />;
  if (type === "cmp") return <Minimize2 size={15} />;
  if (type === "no_geometry") return <ClipboardList size={15} />;
  return <Layers3 size={15} />;
}

function StatusIcon({ status }: { status: StepStatus }) {
  if (status === "running") return <LoaderCircle className="spin" size={12} />;
  if (status === "failed") return <CircleAlert size={12} />;
  if (status === "clean") return <Check size={12} />;
  return <Clock3 size={12} />;
}

function SortableStep({
  step,
  index,
  status,
  accent,
  selected,
  onSelect,
  onToggle,
}: {
  step: ProcessStep;
  index: number;
  status: StepStatus;
  accent: string;
  selected: boolean;
  onSelect: () => void;
  onToggle: () => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: step.id,
  });
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.45 : 1,
  };
  const mask = step.maskSource === "none" ? "no mask" : step.maskSource.replace("_", " ");

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`step-card ${selected ? "selected" : ""} ${step.enabled ? "" : "muted"}`}
      onClick={onSelect}
    >
      <button
        type="button"
        className="drag-handle"
        aria-label={`Reorder ${step.name}`}
        {...attributes}
        {...listeners}
      >
        <GripVertical size={16} />
      </button>
      <div className="step-accent" style={{ background: accent }} />
      <div className="step-copy">
        <div className="step-title-row">
          <span className="step-kind-icon">
            <ProcessIcon type={step.processType} />
          </span>
          <span className="step-title">{step.name}</span>
          <span className="step-index">{index + 1}</span>
        </div>
        <div className="step-subtitle">
          {step.processType.replace("_", " ")} · {mask}
          {step.enabled ? "" : " · skipped"}
        </div>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={step.enabled}
        className="step-toggle"
        title={
          step.enabled
            ? "In the run. Click to skip it; this step and the ones after it then need a re-run."
            : "Skipped. Click to put it back in the run; this step and the ones after it then need a re-run."
        }
        aria-label={
          step.enabled ? `Skip ${step.name} in the run` : `Include ${step.name} in the run`
        }
        onClick={(event) => {
          event.stopPropagation();
          onToggle();
        }}
      >
        {step.enabled ? <SquareCheck size={14} /> : <Square size={14} />}
      </button>
      <div className={`status-chip status-${status}`}>
        <StatusIcon status={status} />
        <span>{STATUS_LABELS[status]}</span>
      </div>
    </div>
  );
}

export function StepList({
  steps,
  statuses,
  accentFor,
  selectedStepId,
  onSelect,
  onAdd,
  onToggle,
  onReorder,
}: StepListProps) {
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = ({ active, over }: DragEndEvent) => {
    if (over && active.id !== over.id) onReorder(String(active.id), String(over.id));
  };

  return (
    <aside className="steps-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">FLOW</span>
          <h2>Process steps</h2>
        </div>
        <span className="count-badge">{steps.length}</span>
      </div>

      <div className="step-scroll">
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
          <SortableContext items={steps.map((step) => step.id)} strategy={verticalListSortingStrategy}>
            <div className="step-list">
              {steps.map((step, index) => (
                <SortableStep
                  key={step.id}
                  step={step}
                  index={index}
                  status={statuses[step.id] ?? "dirty"}
                  accent={accentFor(step)}
                  selected={step.id === selectedStepId}
                  onSelect={() => onSelect(step.id)}
                  onToggle={() => onToggle(step.id)}
                />
              ))}
              {steps.length === 0 && (
                <p className="step-subtitle">This branch has no steps yet.</p>
              )}
            </div>
          </SortableContext>
        </DndContext>
      </div>

      <div className="add-step-wrap">
        <label className="add-step-control">
          <Plus size={15} />
          <span>Add process</span>
          <select
            aria-label="Add a process step"
            value=""
            onChange={(event) => {
              if (event.target.value) onAdd(event.target.value as ProcessType);
              event.target.value = "";
            }}
          >
            <option value="" disabled>
              Choose a process type…
            </option>
            {(["deposit", "etch", "cmp", "no_geometry"] as ProcessType[]).map((type) => (
              <option key={type} value={type}>
                {type === "deposit"
                  ? "Deposition"
                  : type === "etch"
                    ? "Etch"
                    : type === "cmp"
                      ? "CMP"
                      : "No geometry change"}
              </option>
            ))}
          </select>
        </label>
      </div>
    </aside>
  );
}
