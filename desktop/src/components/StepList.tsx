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
  ArrowDown,
  ArrowUp,
  Check,
  ClipboardList,
  CircleAlert,
  Clock3,
  Copy,
  EllipsisVertical,
  GripVertical,
  Layers3,
  LoaderCircle,
  Minimize2,
  Play,
  Plus,
  Scissors,
  Sparkles,
  Square,
  SquareCheck,
  Trash,
} from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type MouseEvent } from "react";
import { createPortal } from "react-dom";
import type { ProcessType, ProcessStep, StepStatus } from "../types";
import { ContextMenu } from "./ContextMenu";

/** Which keys were held on a click: Ctrl (or Cmd) toggles, Shift extends. */
export interface SelectModifiers {
  ctrl: boolean;
  shift: boolean;
}

interface StepListProps {
  steps: ProcessStep[];
  statuses: Record<string, StepStatus>;
  accentFor: (step: ProcessStep) => string;
  /** The step the inspector and the views show. */
  selectedStepId: string;
  /** Every selected step, the focused one included; more than one enables the batch actions. */
  selectedIds: string[];
  busy: boolean;
  canPaste: boolean;
  onSelect: (stepId: string, modifiers: SelectModifiers) => void;
  onClearSelection: () => void;
  onAdd: (processType: ProcessType) => void;
  onToggle: (stepId: string) => void;
  onReorder: (activeId: string, overId: string) => void;
  onRunToHere: (stepId: string) => void;
  onDuplicate: (stepId: string) => void;
  onMove: (stepId: string, direction: -1 | 1) => void;
  onRemove: (stepId: string) => void;
  /** The batch actions work on `selectedIds`. */
  onDuplicateSelected: () => void;
  onRemoveSelected: () => void;
  onMoveSelected: (direction: -1 | 1) => void;
  onEnableSelected: (enabled: boolean) => void;
  onCopySelected: () => void;
  onPaste: () => void;
}

/** Where a step's menu was asked for: the pointer, or the step's own button. */
interface MenuAnchor {
  stepId: string;
  x: number;
  y: number;
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
  focused,
  onSelect,
  onToggle,
  onMenu,
}: {
  step: ProcessStep;
  index: number;
  status: StepStatus;
  accent: string;
  selected: boolean;
  focused: boolean;
  onSelect: (modifiers: SelectModifiers) => void;
  onToggle: () => void;
  onMenu: (x: number, y: number) => void;
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
      className={`step-card ${selected ? "selected" : ""} ${selected && !focused ? "secondary" : ""} ${step.enabled ? "" : "muted"}`}
      onClick={(event) => onSelect({ ctrl: event.ctrlKey || event.metaKey, shift: event.shiftKey })}
      onContextMenu={(event) => {
        event.preventDefault();
        // A right-click on a step outside the selection selects it alone;
        // inside the selection it keeps the selection for the batch menu.
        if (!selected) onSelect({ ctrl: false, shift: false });
        onMenu(event.clientX, event.clientY);
      }}
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
      <button
        type="button"
        className="step-menu-button"
        aria-label={`Actions for ${step.name}`}
        aria-haspopup="menu"
        title="Actions (or right-click the step)"
        onClick={(event: MouseEvent<HTMLButtonElement>) => {
          event.stopPropagation();
          if (!selected) onSelect({ ctrl: false, shift: false });
          const box = event.currentTarget.getBoundingClientRect();
          onMenu(box.right, box.bottom + 4);
        }}
      >
        <EllipsisVertical size={14} />
      </button>
    </div>
  );
}

function StepMenu({
  step,
  index,
  count,
  busy,
  anchor,
  onClose,
  onRunToHere,
  onDuplicate,
  onCopy,
  onPaste,
  canPaste,
  onMove,
  onToggle,
  onRemove,
}: {
  step: ProcessStep;
  index: number;
  count: number;
  busy: boolean;
  anchor: MenuAnchor;
  onClose: () => void;
  onRunToHere: () => void;
  onDuplicate: () => void;
  onCopy: () => void;
  onPaste: () => void;
  canPaste: boolean;
  onMove: (direction: -1 | 1) => void;
  onToggle: () => void;
  onRemove: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState({ left: anchor.x, top: anchor.y });

  // Keep the whole menu on screen: near the bottom or right edge it opens
  // towards the middle instead of running off.
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const box = element.getBoundingClientRect();
    setPlace({
      left: Math.max(8, Math.min(anchor.x, window.innerWidth - box.width - 8)),
      top: Math.max(8, Math.min(anchor.y, window.innerHeight - box.height - 8)),
    });
  }, [anchor]);

  useEffect(() => {
    const away = (event: Event) => {
      if (event.target instanceof Node && ref.current?.contains(event.target)) return;
      onClose();
    };
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("pointerdown", away, true);
    window.addEventListener("keydown", key);
    window.addEventListener("resize", onClose);
    window.addEventListener("scroll", onClose, true);
    return () => {
      window.removeEventListener("pointerdown", away, true);
      window.removeEventListener("keydown", key);
      window.removeEventListener("resize", onClose);
      window.removeEventListener("scroll", onClose, true);
    };
  }, [onClose]);

  const item = (
    label: string,
    icon: React.ReactNode,
    action: () => void,
    options: { disabled?: boolean; danger?: boolean } = {},
  ) => (
    <button
      type="button"
      role="menuitem"
      className={options.danger ? "danger" : ""}
      disabled={options.disabled}
      onClick={() => {
        onClose();
        action();
      }}
    >
      {icon}
      <span>{label}</span>
    </button>
  );

  return createPortal(
    <div
      ref={ref}
      className="context-menu"
      role="menu"
      aria-label={`Actions for ${step.name}`}
      style={{ left: place.left, top: place.top }}
      onContextMenu={(event) => event.preventDefault()}
    >
      <div className="context-menu-title">
        <span className="step-index">{index + 1}</span>
        <span>{step.name}</span>
      </div>
      {item("Run to here", <Play size={13} />, onRunToHere, { disabled: busy })}
      {item("Duplicate", <Copy size={13} />, onDuplicate)}
      {item("Copy", <ClipboardList size={13} />, onCopy)}
      {item("Paste after", <ClipboardList size={13} />, onPaste, { disabled: !canPaste })}
      {item("Move up", <ArrowUp size={13} />, () => onMove(-1), { disabled: index === 0 })}
      {item("Move down", <ArrowDown size={13} />, () => onMove(1), {
        disabled: index === count - 1,
      })}
      {item(
        step.enabled ? "Skip in the run" : "Include in the run",
        step.enabled ? <Square size={13} /> : <SquareCheck size={13} />,
        onToggle,
      )}
      <div className="context-menu-divider" />
      {item("Delete…", <Trash size={13} />, onRemove, { danger: true, disabled: busy })}
    </div>,
    document.body,
  );
}

export function StepList({
  steps,
  statuses,
  accentFor,
  selectedStepId,
  selectedIds,
  busy,
  canPaste,
  onSelect,
  onClearSelection,
  onAdd,
  onToggle,
  onReorder,
  onRunToHere,
  onDuplicate,
  onMove,
  onRemove,
  onDuplicateSelected,
  onRemoveSelected,
  onMoveSelected,
  onEnableSelected,
  onCopySelected,
  onPaste,
}: StepListProps) {
  const [menu, setMenu] = useState<MenuAnchor | null>(null);
  const menuIndex = menu ? steps.findIndex((step) => step.id === menu.stepId) : -1;
  const menuStep = menuIndex >= 0 ? steps[menuIndex] : undefined;
  const selection = new Set(selectedIds);
  const batch = selectedIds.length > 1;
  const batchMenu = batch && menuStep !== undefined && selection.has(menuStep.id);
  const allEnabled = selectedIds.every((id) => steps.find((step) => step.id === id)?.enabled ?? true);
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

      {batch && (
        <div className="step-selection-bar">
          <span>{selectedIds.length} steps selected</span>
          <button type="button" title="Copies after the last selected step (Ctrl+D)" onClick={onDuplicateSelected}>
            Duplicate
          </button>
          <button type="button" title="Move the selection up (Alt+↑)" onClick={() => onMoveSelected(-1)}>
            ↑
          </button>
          <button type="button" title="Move the selection down (Alt+↓)" onClick={() => onMoveSelected(1)}>
            ↓
          </button>
          <button type="button" title="Delete the selected steps (Delete)" onClick={onRemoveSelected} disabled={busy}>
            Delete
          </button>
          <button type="button" title="Keep only the focused step selected (Esc)" onClick={onClearSelection}>
            ×
          </button>
        </div>
      )}

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
                  selected={selection.has(step.id) || step.id === selectedStepId}
                  focused={step.id === selectedStepId}
                  onSelect={(modifiers) => onSelect(step.id, modifiers)}
                  onToggle={() => onToggle(step.id)}
                  onMenu={(x, y) => setMenu({ stepId: step.id, x, y })}
                />
              ))}
              {steps.length === 0 && (
                <p className="step-subtitle">This branch has no steps yet.</p>
              )}
            </div>
          </SortableContext>
        </DndContext>
      </div>

      {menu && menuStep && batchMenu && (
        <ContextMenu
          anchor={menu}
          title={`${selectedIds.length} steps selected`}
          onClose={() => setMenu(null)}
          items={[
            { label: `Duplicate ${selectedIds.length} steps`, icon: <Copy size={13} />, action: onDuplicateSelected, shortcut: "Ctrl+D" },
            { label: `Copy ${selectedIds.length} steps`, icon: <ClipboardList size={13} />, action: onCopySelected, shortcut: "Ctrl+C" },
            { label: "Move up", icon: <ArrowUp size={13} />, action: () => onMoveSelected(-1), shortcut: "Alt+↑" },
            { label: "Move down", icon: <ArrowDown size={13} />, action: () => onMoveSelected(1), shortcut: "Alt+↓" },
            {
              label: allEnabled ? "Skip in the run" : "Include in the run",
              icon: allEnabled ? <Square size={13} /> : <SquareCheck size={13} />,
              action: () => onEnableSelected(!allEnabled),
            },
            { label: "Clear selection", icon: <Check size={13} />, action: onClearSelection, shortcut: "Esc", separated: true },
            { label: `Delete ${selectedIds.length} steps…`, icon: <Trash size={13} />, action: onRemoveSelected, danger: true, disabled: busy, shortcut: "Del" },
          ]}
        />
      )}
      {menu && menuStep && !batchMenu && (
        <StepMenu
          step={menuStep}
          index={menuIndex}
          count={steps.length}
          busy={busy}
          anchor={menu}
          onClose={() => setMenu(null)}
          onRunToHere={() => onRunToHere(menuStep.id)}
          onDuplicate={() => onDuplicate(menuStep.id)}
          onCopy={onCopySelected}
          onPaste={onPaste}
          canPaste={canPaste}
          onMove={(direction) => onMove(menuStep.id, direction)}
          onToggle={() => onToggle(menuStep.id)}
          onRemove={() => onRemove(menuStep.id)}
        />
      )}

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
