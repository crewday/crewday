import type { DisplayError } from "@/lib/displayError";

export type ErrorToastSource = "query" | "mutation" | "manual";

export interface ErrorToastEvent {
  error: DisplayError;
  source: ErrorToastSource;
}

type ErrorToastHandler = (event: ErrorToastEvent) => void;

let activeHandler: ErrorToastHandler | null = null;

export function setGlobalErrorToastHandler(handler: ErrorToastHandler): () => void {
  activeHandler = handler;
  return () => {
    if (activeHandler === handler) activeHandler = null;
  };
}

export function publishGlobalErrorToast(event: ErrorToastEvent): void {
  activeHandler?.(event);
}
