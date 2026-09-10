import type { DesktopBridge } from "./bridge";
import { BrowserBridge } from "./browserBridge";
import { TauriBridge } from "./tauriBridge";

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

export const bridge: DesktopBridge = window.__TAURI_INTERNALS__
  ? new TauriBridge()
  : new BrowserBridge();
