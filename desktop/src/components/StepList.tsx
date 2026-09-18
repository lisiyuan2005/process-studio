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
  ChevronDown,
  ChevronRight,
  ClipboardList,
  CircleAlert,
  Clock3,
  Copy,
  EllipsisVertical,
  Flame,
  GripVertical,
  Layers3,
  LoaderCircle,
  Minimize2,
  Pencil,
  Play,
  Plus,
  Repeat,
  Scissors,
  Sparkles,
  Square,
  SquareCheck,
  Trash,
  Ungroup,
  X,
} from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { ProcessType, ProcessStep, StepLoop, StepStatus } from "../types";
import { flowUnits, loopIterations, type FlowUnit } from "../domain/project";
import { ContextMenu, type MenuItem } from "./ContextMenu";
import { NumberField } from "./NumberField";

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
  /** The step types this project's kernel runs. */
  processTypes: ProcessType[];
  /** The loop whose header is selected, if any; its settings show in the inspector. */
  selectedLoopId: string;
  /** Why the current selection cannot become a loop, or null when it can. */
  loopObstacle: string | null;
  loops: LoopActions;
  /** The "repeat as a loop" dialog, opened here or by the Edit menu. */
  loopDialogOpen: boolean;
  onLoopDialogChange: (open: boolean) => void;
}

/** What the list can do with loops; each acts on the loop's id. */
export interface LoopActions {
  /** Repeat the current selection: `name` and how many times in all. */
  create: (name: string, repeat: number) => void;
  select: (loopId: string) => void;
  rename: (loopId: string, name: string) => void;
  setRepeat: (loopId: string, repeat: number) => void;
  /** Take the loop apart; every iteration stays as plain steps. */
  dissolve: (loopId: string) => void;
  remove: (loopId: string) => void;
  duplicate: (loopId: string) => void;
  copy: (loopId: string) => void;
  move: (loopId: string, direction: -1 | 1) => void;
  setEnabled: (loopId: string, enabled: boolean) => void;
  runToEnd: (loopId: string) => void;
}

/** One status for several steps: what most needs attention wins. */
export function summarizeStatuses(statuses: StepStatus[]): StepStatus {
  for (const status of ["running", "failed", "dirty", "stale"] as const) {
    if (statuses.includes(status)) return status;
  }
  return "clean";
}

/** Where a step's menu was asked for: the pointer, or the step's own button. */
interface MenuAnchor {
  stepId: string;
  x: number;
  y: number;
}

export const PROCESS_LABELS: Record<ProcessType, string> = {
  deposit: "Deposition",
  etch: "Etch",
  cmp: "CMP",
  no_geometry: "No geometry change",
  oxidation: "Oxidation",
};

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
  if (type === "oxidation") return <Flame size={15} />;
  return <Layers3 size={15} />;
}

function StatusIcon({ status }: { status: StepStatus }) {
  if (status === "running") return <LoaderCircle className="spin" size={12} />;
  if (status === "failed") return <CircleAlert size={12} />;
  if (status === "clean") return <Check size={12} />;
  return <Clock3 size={12} />;
}

interface StepCardProps {
  step: ProcessStep;
  index: number;
  status: StepStatus;
  accent: string;
  selected: boolean;
  focused: boolean;
  onSelect: (modifiers: SelectModifiers) => void;
  onToggle: () => void;
  onMenu: (x: number, y: number) => void;
}

/** A step in the list. Outside a loop it is wrapped to be dragged; inside one it sits still. */
function StepCard({
  step,
  index,
  status,
  accent,
  selected,
  focused,
  onSelect,
  onToggle,
  onMenu,
  handle,
  nodeRef,
  style,
}: StepCardProps & {
  handle?: ReactNode;
  nodeRef?: (node: HTMLElement | null) => void;
  style?: CSSProperties;
}) {
  const mask = step.maskSource === "none" ? "no mask" : step.maskSource.replace("_", " ");

  return (
    <div
      ref={nodeRef}
      style={style}
      className={`step-card ${selected ? "selected" : ""} ${selected && !focused ? "secondary" : ""} ${step.enabled ? "" : "muted"} ${step.loop ? "in-loop" : ""}`}
      onClick={(event) => onSelect({ ctrl: event.ctrlKey || event.metaKey, shift: event.shiftKey })}
      onContextMenu={(event) => {
        event.preventDefault();
        // A right-click on a step outside the selection selects it alone;
        // inside the selection it keeps the selection for the batch menu.
        if (!selected) onSelect({ ctrl: false, shift: false });
        onMenu(event.clientX, event.clientY);
      }}
    >
      {handle ?? <span className="loop-tick" aria-hidden="true" />}
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

function SortableStep(props: StepCardProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: props.step.id,
  });
  return (
    <StepCard
      {...props}
      nodeRef={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.45 : 1 }}
      handle={
        <button
          type="button"
          className="drag-handle"
          aria-label={`Reorder ${props.step.name}`}
          {...attributes}
          {...listeners}
        >
          <GripVertical size={16} />
        </button>
      }
    />
  );
}

/**
 * A loop in the list: a header that drags, selects and collapses the
 * whole block, then its iterations. The first iteration is open by default
 * and the others folded to one line each, since they are the same steps.
 */
function SortableLoop({
  unit,
  firstIndex,
  statuses,
  selected,
  collapsed,
  openIterations,
  onToggleCollapsed,
  onToggleIteration,
  onSelect,
  onMenu,
  renderStep,
}: {
  unit: FlowUnit;
  firstIndex: number;
  statuses: Record<string, StepStatus>;
  selected: boolean;
  collapsed: boolean;
  openIterations: Set<string>;
  onToggleCollapsed: () => void;
  onToggleIteration: (iteration: number) => void;
  onSelect: () => void;
  onMenu: (x: number, y: number) => void;
  renderStep: (step: ProcessStep, index: number) => ReactNode;
}) {
  const loop = unit.loop!;
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: unit.id });
  const iterations = loopIterations(unit.steps, loop.id);
  const status = summarizeStatuses(unit.steps.map((step) => statuses[step.id] ?? "dirty"));
  const allSkipped = unit.steps.every((step) => !step.enabled);
  let index = firstIndex;

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.45 : 1 }}
      className={`loop-block ${selected ? "selected" : ""} ${allSkipped ? "muted" : ""}`}
    >
      <div
        className="loop-head"
        onClick={onSelect}
        onContextMenu={(event) => {
          event.preventDefault();
          onSelect();
          onMenu(event.clientX, event.clientY);
        }}
      >
        <button type="button" className="drag-handle" aria-label={`Reorder the loop ${loop.name}`} {...attributes} {...listeners}>
          <GripVertical size={16} />
        </button>
        <button
          type="button"
          className="loop-fold"
          aria-label={collapsed ? `Expand ${loop.name}` : `Collapse ${loop.name}`}
          aria-expanded={!collapsed}
          onClick={(event) => {
            event.stopPropagation();
            onToggleCollapsed();
          }}
        >
          {collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
        </button>
        <div className="loop-copy">
          <div className="step-title-row">
            <span className="loop-icon"><Repeat size={14} /></span>
            <span className="loop-name">{loop.name}</span>
            <span className="loop-repeat" title={`${iterations[0]?.length ?? 0} step(s), ${loop.repeat} times`}>×{loop.repeat}</span>
          </div>
          <div className="step-subtitle">
            {iterations[0]?.length ?? 0} step{(iterations[0]?.length ?? 0) === 1 ? "" : "s"} × {loop.repeat} · steps {firstIndex + 1}–{firstIndex + unit.steps.length}
            {allSkipped ? " · skipped" : ""}
          </div>
        </div>
        <div className={`status-chip status-${status}`}>
          <StatusIcon status={status} />
          <span>{STATUS_LABELS[status]}</span>
        </div>
        <button
          type="button"
          className="step-menu-button loop-menu-button"
          aria-label={`Actions for the loop ${loop.name}`}
          aria-haspopup="menu"
          onClick={(event: MouseEvent<HTMLButtonElement>) => {
            event.stopPropagation();
            onSelect();
            const box = event.currentTarget.getBoundingClientRect();
            onMenu(box.right, box.bottom + 4);
          }}
        >
          <EllipsisVertical size={14} />
        </button>
      </div>
      {!collapsed && (
        <div className="loop-body">
          {iterations.map((steps, iteration) => {
            const open = (iteration === 0) !== openIterations.has(`${loop.id}:${iteration}`);
            const start = index;
            index += steps.length;
            const own = summarizeStatuses(steps.map((step) => statuses[step.id] ?? "dirty"));
            return (
              <div key={iteration} className="loop-iteration">
                <button
                  type="button"
                  className="loop-iteration-head"
                  aria-expanded={open}
                  onClick={() => onToggleIteration(iteration)}
                >
                  {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                  <span>Iteration {iteration + 1} of {loop.repeat}</span>
                  <span className="step-index">{start + 1}–{start + steps.length}</span>
                  {!open && (
                    <span className={`status-dot status-${own}`} title={STATUS_LABELS[own]} />
                  )}
                </button>
                {open && steps.map((step, offset) => renderStep(step, start + offset))}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** Name and count for a new loop. */
function LoopDialog({
  count,
  onCancel,
  onConfirm,
}: {
  count: number;
  onCancel: () => void;
  onConfirm: (name: string, repeat: number) => void;
}) {
  const [name, setName] = useState("Loop");
  const [repeat, setRepeat] = useState(2);
  const submit = () => onConfirm(name.trim() || "Loop", Math.max(1, Math.floor(repeat || 1)));
  return createPortal(
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Repeat as a loop">
      <div className="modal-card line-modal">
        <header className="modal-header">
          <div>
            <span className="eyebrow">LOOP</span>
            <h2>Repeat {count} step{count === 1 ? "" : "s"}</h2>
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onCancel}>
            <X size={16} />
          </button>
        </header>
        <form
          className="modal-body loop-form"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <label className="field-row">
            <span>Name</span>
            <input autoFocus value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. ON pair" />
          </label>
          <label className="field-row">
            <span>Times in all</span>
            <NumberField min={1} step={1} value={repeat} onChange={setRepeat} />
            <small>
              The selected steps are the first time through; the copies that follow stay identical to them.
              Each time through has its own result, so the view can stop after any of them.
            </small>
          </label>
        </form>
        <div className="modal-actions">
          <button type="button" className="secondary-button" onClick={onCancel}>Cancel</button>
          <button type="button" className="modal-save primary-button" onClick={submit}>Repeat</button>
        </div>
      </div>
    </div>,
    document.body,
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
  onLoop,
  loopObstacle,
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
  onLoop: () => void;
  loopObstacle: string | null;
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
    options: { disabled?: boolean; danger?: boolean; title?: string } = {},
  ) => (
    <button
      type="button"
      role="menuitem"
      className={options.danger ? "danger" : ""}
      disabled={options.disabled}
      title={options.title}
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
      {item("Repeat as a loop…", <Repeat size={13} />, onLoop, {
        disabled: loopObstacle !== null,
        title: loopObstacle ?? "Run this step several times in a row",
      })}
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
  processTypes,
  selectedLoopId,
  loopObstacle,
  loops,
  loopDialogOpen,
  onLoopDialogChange,
}: StepListProps) {
  const [menu, setMenu] = useState<MenuAnchor | null>(null);
  const [loopMenu, setLoopMenu] = useState<{ loopId: string; x: number; y: number } | null>(null);
  const setLoopDialog = onLoopDialogChange;
  const loopDialog = loopDialogOpen;
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [openIterations, setOpenIterations] = useState<Set<string>>(() => new Set());
  const menuIndex = menu ? steps.findIndex((step) => step.id === menu.stepId) : -1;
  const menuStep = menuIndex >= 0 ? steps[menuIndex] : undefined;
  const selection = new Set(selectedIds);
  const batch = selectedIds.length > 1;
  const batchMenu = batch && menuStep !== undefined && selection.has(menuStep.id);
  const allEnabled = selectedIds.every((id) => steps.find((step) => step.id === id)?.enabled ?? true);
  const units = flowUnits(steps);
  const menuLoop = loopMenu ? units.find((unit) => unit.loop?.id === loopMenu.loopId) : undefined;
  const toggleIn = (set: Set<string>, key: string) => {
    const next = new Set(set);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    return next;
  };
  const loopMenuItems = (unit: FlowUnit): MenuItem[] => {
    const loop = unit.loop as StepLoop;
    const at = units.indexOf(unit);
    const enabled = unit.steps.some((step) => step.enabled);
    const isCollapsed = collapsed.has(loop.id);
    return [
      { label: "Run to the end of the loop", icon: <Play size={13} />, action: () => loops.runToEnd(loop.id), disabled: busy },
      {
        label: "Times through…",
        icon: <Repeat size={13} />,
        action: () => {
          const answer = window.prompt(`How many times in all should ${loop.name} run?`, String(loop.repeat));
          const repeat = answer === null ? NaN : Number(answer);
          if (Number.isFinite(repeat) && repeat >= 1) loops.setRepeat(loop.id, Math.floor(repeat));
        },
      },
      {
        label: "Rename…",
        icon: <Pencil size={13} />,
        action: () => {
          const answer = window.prompt("Name of the loop", loop.name);
          if (answer !== null && answer.trim()) loops.rename(loop.id, answer);
        },
      },
      { label: "Duplicate the loop", icon: <Copy size={13} />, action: () => loops.duplicate(loop.id), separated: true },
      { label: "Copy the loop", icon: <ClipboardList size={13} />, action: () => loops.copy(loop.id) },
      { label: "Move up", icon: <ArrowUp size={13} />, action: () => loops.move(loop.id, -1), disabled: at <= 0 },
      { label: "Move down", icon: <ArrowDown size={13} />, action: () => loops.move(loop.id, 1), disabled: at >= units.length - 1 },
      {
        label: enabled ? "Skip in the run" : "Include in the run",
        icon: enabled ? <Square size={13} /> : <SquareCheck size={13} />,
        action: () => loops.setEnabled(loop.id, !enabled),
      },
      {
        label: isCollapsed ? "Expand" : "Collapse",
        icon: isCollapsed ? <ChevronDown size={13} /> : <ChevronRight size={13} />,
        action: () => setCollapsed((current) => toggleIn(current, loop.id)),
        separated: true,
      },
      { label: "Take the loop apart", icon: <Ungroup size={13} />, action: () => loops.dissolve(loop.id) },
      { label: "Delete the loop…", icon: <Trash size={13} />, action: () => loops.remove(loop.id), danger: true, disabled: busy, separated: true },
    ];
  };
  const renderStep = (step: ProcessStep, index: number, sortable: boolean) => {
    const props: StepCardProps = {
      step,
      index,
      status: statuses[step.id] ?? "dirty",
      accent: accentFor(step),
      selected: selection.has(step.id) || step.id === selectedStepId,
      focused: step.id === selectedStepId && !selectedLoopId,
      onSelect: (modifiers) => onSelect(step.id, modifiers),
      onToggle: () => onToggle(step.id),
      onMenu: (x, y) => setMenu({ stepId: step.id, x, y }),
    };
    return sortable ? <SortableStep key={step.id} {...props} /> : <StepCard key={step.id} {...props} />;
  };
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
          <button type="button" title={loopObstacle ?? "Repeat the selected steps as a loop (Ctrl+G)"} onClick={() => setLoopDialog(true)} disabled={loopObstacle !== null}>
            Loop…
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
          <SortableContext items={units.map((unit) => unit.id)} strategy={verticalListSortingStrategy}>
            <div className="step-list">
              {units.map((unit) => {
                const firstIndex = steps.indexOf(unit.steps[0]);
                if (!unit.loop) return renderStep(unit.steps[0], firstIndex, true);
                const loopId = unit.loop.id;
                return (
                  <SortableLoop
                    key={unit.id}
                    unit={unit}
                    firstIndex={firstIndex}
                    statuses={statuses}
                    selected={selectedLoopId === loopId}
                    collapsed={collapsed.has(loopId)}
                    openIterations={openIterations}
                    onToggleCollapsed={() => setCollapsed((current) => toggleIn(current, loopId))}
                    onToggleIteration={(iteration) =>
                      setOpenIterations((current) => toggleIn(current, `${loopId}:${iteration}`))
                    }
                    onSelect={() => loops.select(loopId)}
                    onMenu={(x, y) => setLoopMenu({ loopId, x, y })}
                    renderStep={(step, index) => renderStep(step, index, false)}
                  />
                );
              })}
              {steps.length === 0 && (
                <p className="step-subtitle">This branch has no steps yet.</p>
              )}
            </div>
          </SortableContext>
        </DndContext>
      </div>

      {loopMenu && menuLoop && (
        <ContextMenu
          anchor={loopMenu}
          title={`${menuLoop.loop!.name} · ${menuLoop.steps.length / menuLoop.loop!.repeat} step(s) × ${menuLoop.loop!.repeat}`}
          onClose={() => setLoopMenu(null)}
          items={loopMenuItems(menuLoop)}
        />
      )}
      {loopDialog && (
        <LoopDialog
          count={selectedIds.length || 1}
          onCancel={() => setLoopDialog(false)}
          onConfirm={(name, repeat) => {
            setLoopDialog(false);
            loops.create(name, repeat);
          }}
        />
      )}

      {menu && menuStep && batchMenu && (
        <ContextMenu
          anchor={menu}
          title={`${selectedIds.length} steps selected`}
          onClose={() => setMenu(null)}
          items={[
            { label: `Duplicate ${selectedIds.length} steps`, icon: <Copy size={13} />, action: onDuplicateSelected, shortcut: "Ctrl+D" },
            { label: `Copy ${selectedIds.length} steps`, icon: <ClipboardList size={13} />, action: onCopySelected, shortcut: "Ctrl+C" },
            { label: `Repeat ${selectedIds.length} steps as a loop…`, icon: <Repeat size={13} />, action: () => setLoopDialog(true), disabled: loopObstacle !== null, shortcut: "Ctrl+G" },
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
          onLoop={() => setLoopDialog(true)}
          loopObstacle={loopObstacle}
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
            {processTypes.map((type) => (
              <option key={type} value={type}>
                {PROCESS_LABELS[type] ?? type}
              </option>
            ))}
          </select>
        </label>
      </div>
    </aside>
  );
}
