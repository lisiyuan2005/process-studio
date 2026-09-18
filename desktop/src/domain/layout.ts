/**
 * How the window's width is shared between the two side panels and the
 * viewport, remembered on this machine.
 *
 * Shares, not pixels. Pixel widths meant the columns did not fit every
 * window, so there were breakpoints that stepped them down -- and any
 * width the breakpoints did not foresee ended with a panel over the edge
 * of the screen. A share fits by construction, at any width, with nothing
 * to foresee.
 */

export interface PanelShares {
  /** Fraction of the window the steps panel takes, 0 to 1. */
  steps: number;
  /** Fraction of the window the inspector takes. */
  inspector: number;
}

const KEY = "processStudio.panelWidths";

export const PANEL_LIMITS = {
  // Room to make one panel genuinely wide -- reading a long flow, or a step
  // with many material responses -- with the viewport's share as the only
  // thing holding the line. Maxima that could not add up past that line
  // would make the giving-back below unreachable.
  steps: { min: 0.13, max: 0.45 },
  inspector: { min: 0.14, max: 0.45 },
  /** The viewport in the middle always keeps at least this share. */
  viewport: 0.26,
} as const;

export const DEFAULT_PANELS: PanelShares = { steps: 0.21, inspector: 0.22 };

export function percent(share: number): string {
  return `${(share * 100).toFixed(3)}%`;
}

export function loadPanelShares(): PanelShares | null {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      typeof (parsed as PanelShares).steps !== "number" ||
      typeof (parsed as PanelShares).inspector !== "number"
    ) {
      return null;
    }
    return clampPanelShares(asShares(parsed as PanelShares));
  } catch {
    return null;
  }
}

/**
 * What a build that stored pixels left behind: anything above 1 is pixels,
 * read as a share of the window it is being opened in.
 */
function asShares(stored: PanelShares): PanelShares {
  const width = window.innerWidth || 1440;
  const share = (value: number) => (value > 1 ? value / width : value);
  return { steps: share(stored.steps), inspector: share(stored.inspector) };
}

export function savePanelShares(shares: PanelShares | null) {
  try {
    if (shares) window.localStorage.setItem(KEY, JSON.stringify(shares));
    else window.localStorage.removeItem(KEY);
  } catch {
    // a convenience only
  }
}

/**
 * Keep both panels within their limits and leave the viewport its share.
 *
 * It takes no window width: that is the point of shares. Whatever comes
 * out of here fits every window the same way.
 */
export function clampPanelShares(shares: PanelShares): PanelShares {
  const within = (value: number, limits: { min: number; max: number }) =>
    Math.min(limits.max, Math.max(limits.min, Number.isFinite(value) ? value : limits.min));
  let steps = within(shares.steps, PANEL_LIMITS.steps);
  let inspector = within(shares.inspector, PANEL_LIMITS.inspector);

  const spare = 1 - PANEL_LIMITS.viewport - steps - inspector;
  if (spare >= 0) return { steps, inspector };
  // Too much between them: give back what is missing, the inspector first.
  const fromInspector = Math.min(inspector - PANEL_LIMITS.inspector.min, -spare);
  inspector -= fromInspector;
  steps -= Math.min(steps - PANEL_LIMITS.steps.min, -spare - fromInspector);
  return { steps, inspector };
}
