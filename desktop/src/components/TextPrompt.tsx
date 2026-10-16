import { X } from "lucide-react";
import { useCallback, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

/** What a prompt asks: a title, a line of explanation and a starting value. */
export interface PromptRequest {
  title: string;
  message?: string;
  label?: string;
  initial?: string;
  confirm?: string;
  /** Only whole numbers of at least this; absent for free text. */
  minimumInteger?: number;
}

/**
 * ``window.prompt`` inside the application. The webview the desktop shell
 * runs in on macOS has no prompt of its own: it answers every call at once
 * as if Cancel had been pressed, so whatever asked for a name -- forking a
 * branch, renaming one, a loop -- silently did nothing.
 *
 * Returns the dialog to render and ``ask``, which resolves to the text
 * entered, or null when the dialog is dismissed.
 */
export function useTextPrompt(): [ReactNode, (request: PromptRequest) => Promise<string | null>] {
  const [request, setRequest] = useState<PromptRequest | null>(null);
  const [value, setValue] = useState("");
  const resolver = useRef<((answer: string | null) => void) | null>(null);

  const ask = useCallback((next: PromptRequest) => {
    resolver.current?.(null);
    setValue(next.initial ?? "");
    setRequest(next);
    return new Promise<string | null>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  const finish = (answer: string | null) => {
    resolver.current?.(answer);
    resolver.current = null;
    setRequest(null);
  };

  const invalid =
    request?.minimumInteger !== undefined
      ? !(Number.isInteger(Number(value)) && Number(value) >= request.minimumInteger)
      : !value.trim();

  const dialog = request
    ? createPortal(
        <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label={request.title}>
          <div className="modal-card line-modal">
            <header className="modal-header">
              <div>
                <h2>{request.title}</h2>
              </div>
              <button type="button" className="icon-button" aria-label="Close" onClick={() => finish(null)}>
                <X size={16} />
              </button>
            </header>
            <form
              className="modal-body loop-form"
              onSubmit={(event) => {
                event.preventDefault();
                if (!invalid) finish(value.trim());
              }}
            >
              <label className="field-row">
                <span>{request.label ?? "Name"}</span>
                <input
                  autoFocus
                  value={value}
                  inputMode={request.minimumInteger !== undefined ? "numeric" : undefined}
                  onChange={(event) => setValue(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") finish(null);
                  }}
                />
                {request.message && <small>{request.message}</small>}
              </label>
            </form>
            <div className="modal-actions">
              <button type="button" className="secondary-button" onClick={() => finish(null)}>
                Cancel
              </button>
              <button
                type="button"
                className="modal-save primary-button"
                disabled={invalid}
                onClick={() => finish(value.trim())}
              >
                {request.confirm ?? "OK"}
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )
    : null;
  return [dialog, ask];
}
