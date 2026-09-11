/** The widths the user gave the side panels, remembered on this machine. */

export interface PanelWidths {
  steps: number;
  inspector: number;
}

const KEY = "processStudio.panelWidths";

export const PANEL_LIMITS = {
  steps: { min: 220, max: 560 },
  inspector: { min: 240, max: 640 },
  /** The viewport in the middle never gets narrower than this. */
  viewport: 360,
} as const;

export function loadPanelWidths(): PanelWidths | null {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed !== "object" || parsed === null ||
      typeof (parsed as PanelWidths).steps !== "number" ||
      typeof (parsed as PanelWidths).inspector !== "number"
    ) {
      return null;
    }
    return clampPanelWidths(parsed as PanelWidths, window.innerWidth);
  } catch {
    return null;
  }
}

export function savePanelWidths(widths: PanelWidths | null) {
  try {
    if (widths) window.localStorage.setItem(KEY, JSON.stringify(widths));
    else window.localStorage.removeItem(KEY);
  } catch {
    // a convenience only
  }
}

/** Keep both panels within their limits and leave the viewport its minimum. */
export function clampPanelWidths(widths: PanelWidths, windowWidth: number): PanelWidths {
  const steps = Math.min(
    PANEL_LIMITS.steps.max,
    Math.max(PANEL_LIMITS.steps.min, Math.round(widths.steps)),
  );
  const inspector = Math.min(
    PANEL_LIMITS.inspector.max,
    Math.max(PANEL_LIMITS.inspector.min, Math.round(widths.inspector)),
  );
  const spare = windowWidth - PANEL_LIMITS.viewport - steps - inspector;
  if (spare >= 0) return { steps, inspector };
  // Too wide together: give back what is missing, the larger panel first.
  const inspectorRoom = inspector - PANEL_LIMITS.inspector.min;
  const fromInspector = Math.min(inspectorRoom, -spare);
  const fromSteps = Math.min(steps - PANEL_LIMITS.steps.min, -spare - fromInspector);
  return { steps: steps - fromSteps, inspector: inspector - fromInspector };
}
