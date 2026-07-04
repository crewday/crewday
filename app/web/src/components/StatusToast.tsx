import {
  createContext,
  type ReactNode,
  useCallback,
  use,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { X } from "lucide-react";
import {
  type StatusToastEvent,
  type StatusToastTone,
  setGlobalStatusToastHandler,
} from "@/lib/statusToastBus";

// Non-error status/info toasts (§14 "Completed by <name>"). This is a
// deliberately lean sibling of `ErrorToast`: the error surface is
// `DisplayError`-shaped (expandable details, fingerprint, rust accent),
// whereas a status toast is a single line of neutral copy. It reuses the
// same accessibility posture — a `role="status"` `aria-live="polite"`
// region, a dismiss control, Escape-to-close, and an auto-dismiss timer
// that pauses on hover/focus — but carries none of the error chrome.

const AUTO_DISMISS_MS = 7_000;
const MAX_TOASTS = 4;
const STATUS_ROLE = "status";

interface StatusToastItem {
  key: string;
  message: string;
  tone: StatusToastTone;
  hovered: boolean;
  focused: boolean;
}

interface StatusToastContextValue {
  enqueueStatusToast: (message: string, tone?: StatusToastTone) => string;
  dismissStatusToast: (key: string) => void;
}

const StatusToastContext = createContext<StatusToastContextValue | null>(null);

export function StatusToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<StatusToastItem[]>([]);

  const dismissStatusToast = useCallback((key: string) => {
    setToasts((current) => current.filter((toast) => toast.key !== key));
  }, []);

  const enqueueStatusToast = useCallback((message: string, tone: StatusToastTone = "info") => {
    const key = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    setToasts((current) => {
      const existingIndex = current.findIndex((toast) => toast.message === message);
      if (existingIndex >= 0) {
        const next = current.slice();
        const existing = next[existingIndex]!;
        next[existingIndex] = { ...existing, key, tone, hovered: false, focused: false };
        return moveToFront(next, existingIndex);
      }
      return [{ key, message, tone, hovered: false, focused: false }, ...current].slice(0, MAX_TOASTS);
    });
    return key;
  }, []);

  useEffect(() => {
    return setGlobalStatusToastHandler((event: StatusToastEvent) => {
      enqueueStatusToast(event.message, event.tone ?? "info");
    });
  }, [enqueueStatusToast]);

  const value = useMemo<StatusToastContextValue>(() => ({
    dismissStatusToast,
    enqueueStatusToast,
  }), [dismissStatusToast, enqueueStatusToast]);

  const updateToast = useCallback((key: string, patch: Partial<StatusToastItem>) => {
    setToasts((current) => current.map((toast) => (
      toast.key === key ? { ...toast, ...patch } : toast
    )));
  }, []);

  return (
    <StatusToastContext.Provider value={value}>
      {children}
      <StatusToastViewport
        toasts={toasts}
        onDismiss={dismissStatusToast}
        onUpdate={updateToast}
      />
    </StatusToastContext.Provider>
  );
}

export function useStatusToasts(): StatusToastContextValue {
  const context = use(StatusToastContext);
  if (!context) {
    throw new Error("useStatusToasts must be used within StatusToastProvider");
  }
  return context;
}

function StatusToastViewport({
  toasts,
  onDismiss,
  onUpdate,
}: {
  toasts: StatusToastItem[];
  onDismiss: (key: string) => void;
  onUpdate: (key: string, patch: Partial<StatusToastItem>) => void;
}) {
  if (toasts.length === 0) return null;
  return (
    <div className="status-toast-viewport" aria-label="Status notifications">
      {toasts.map((toast) => (
        <StatusToast
          key={toast.key}
          toast={toast}
          onDismiss={onDismiss}
          onUpdate={onUpdate}
        />
      ))}
    </div>
  );
}

function StatusToast({
  toast,
  onDismiss,
  onUpdate,
}: {
  toast: StatusToastItem;
  onDismiss: (key: string) => void;
  onUpdate: (key: string, patch: Partial<StatusToastItem>) => void;
}) {
  const toastRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (toast.hovered || toast.focused) return undefined;
    const timeout = window.setTimeout(() => onDismiss(toast.key), AUTO_DISMISS_MS);
    return () => window.clearTimeout(timeout);
  }, [onDismiss, toast.focused, toast.hovered, toast.key]);

  useEffect(() => {
    const node = toastRef.current;
    if (!node) return undefined;
    const onMouseEnter = () => onUpdate(toast.key, { hovered: true });
    const onMouseLeave = () => onUpdate(toast.key, { hovered: false });
    const onFocusIn = () => onUpdate(toast.key, { focused: true });
    const onFocusOut = (event: FocusEvent) => {
      if (event.relatedTarget instanceof Node && node.contains(event.relatedTarget)) return;
      onUpdate(toast.key, { focused: false });
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      onDismiss(toast.key);
    };
    node.addEventListener("mouseenter", onMouseEnter);
    node.addEventListener("mouseleave", onMouseLeave);
    node.addEventListener("focusin", onFocusIn);
    node.addEventListener("focusout", onFocusOut);
    node.addEventListener("keydown", onKeyDown);
    return () => {
      node.removeEventListener("mouseenter", onMouseEnter);
      node.removeEventListener("mouseleave", onMouseLeave);
      node.removeEventListener("focusin", onFocusIn);
      node.removeEventListener("focusout", onFocusOut);
      node.removeEventListener("keydown", onKeyDown);
    };
  }, [onDismiss, onUpdate, toast.key]);

  return (
    <section
      ref={toastRef}
      className={`status-toast status-toast--${toast.tone}`}
      role={STATUS_ROLE}
      aria-live="polite"
      aria-atomic="true"
      data-tone={toast.tone}
    >
      <div className="status-toast__head">
        <span className="status-toast__message">{toast.message}</span>
        <button
          type="button"
          className="status-toast__close"
          aria-label="Dismiss status notification"
          onClick={() => onDismiss(toast.key)}
        >
          <X size={16} strokeWidth={2.2} aria-hidden="true" />
        </button>
      </div>
    </section>
  );
}

function moveToFront(items: StatusToastItem[], index: number): StatusToastItem[] {
  const [item] = items.splice(index, 1);
  if (!item) return items;
  return [item, ...items];
}
