// Global status/info toast bus. Mirrors `errorToastBus` but carries a
// plain message rather than a `DisplayError`: the error bus is
// `DisplayError`-shaped end to end (fingerprint, expandable
// `DisplayErrorDetails`, rust styling), so a non-error "status" signal
// like the §14 "Completed by <name>" concurrent-completion notice rides
// a separate, intentionally minimal bus instead of bending the error
// path into a second mode.
//
// The only producer today is the SSE supersession path in
// `@/lib/sse` (`task.completed` frames that show another user
// re-completed the current user's just-completed task — §06
// last-write-wins). `<StatusToastProvider>` subscribes and renders.

export type StatusToastTone = "info";

export interface StatusToastEvent {
  message: string;
  tone?: StatusToastTone;
}

type StatusToastHandler = (event: StatusToastEvent) => void;

let activeHandler: StatusToastHandler | null = null;

export function setGlobalStatusToastHandler(handler: StatusToastHandler): () => void {
  activeHandler = handler;
  return () => {
    if (activeHandler === handler) activeHandler = null;
  };
}

export function publishGlobalStatusToast(event: StatusToastEvent): void {
  activeHandler?.(event);
}
