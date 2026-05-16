import {
  createContext,
  type FocusEvent,
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useState,
} from "react";
import { Check, Copy, X } from "lucide-react";
import type { DisplayError, DisplayErrorDetail } from "@/lib/displayError";
import {
  type ErrorToastEvent,
  type ErrorToastSource,
  setGlobalErrorToastHandler,
} from "@/lib/errorToastBus";

const AUTO_DISMISS_MS = 9_000;
const MAX_TOASTS = 4;

interface ErrorToastItem {
  key: string;
  fingerprint: string;
  error: DisplayError;
  source: ErrorToastSource;
  expanded: boolean;
  hovered: boolean;
  focused: boolean;
}

interface ErrorToastContextValue {
  enqueueErrorToast: (error: DisplayError, source?: ErrorToastSource) => string;
  dismissErrorToast: (key: string) => void;
}

const ErrorToastContext = createContext<ErrorToastContextValue | null>(null);

export function ErrorToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ErrorToastItem[]>([]);

  const dismissErrorToast = useCallback((key: string) => {
    setToasts((current) => current.filter((toast) => toast.key !== key));
  }, []);

  const enqueueErrorToast = useCallback((error: DisplayError, source: ErrorToastSource = "manual") => {
    const fingerprint = errorFingerprint(error);
    const key = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    setToasts((current) => {
      const existingIndex = current.findIndex((toast) => toast.fingerprint === fingerprint);
      if (existingIndex >= 0) {
        const next = current.slice();
        const existing = next[existingIndex]!;
        next[existingIndex] = {
          ...existing,
          key,
          error,
          source,
          hovered: false,
          focused: false,
        };
        return moveToFront(next, existingIndex);
      }
      return [{
        key,
        fingerprint,
        error,
        source,
        expanded: false,
        hovered: false,
        focused: false,
      }, ...current].slice(0, MAX_TOASTS);
    });
    return key;
  }, []);

  useEffect(() => {
    return setGlobalErrorToastHandler((event: ErrorToastEvent) => {
      enqueueErrorToast(event.error, event.source);
    });
  }, [enqueueErrorToast]);

  const value = useMemo<ErrorToastContextValue>(() => ({
    dismissErrorToast,
    enqueueErrorToast,
  }), [dismissErrorToast, enqueueErrorToast]);

  const updateToast = useCallback((key: string, patch: Partial<ErrorToastItem>) => {
    setToasts((current) => current.map((toast) => (
      toast.key === key ? { ...toast, ...patch } : toast
    )));
  }, []);

  return (
    <ErrorToastContext.Provider value={value}>
      {children}
      <ErrorToastViewport
        toasts={toasts}
        onDismiss={dismissErrorToast}
        onUpdate={updateToast}
      />
    </ErrorToastContext.Provider>
  );
}

export function useErrorToasts(): ErrorToastContextValue {
  const context = useContext(ErrorToastContext);
  if (!context) {
    throw new Error("useErrorToasts must be used within ErrorToastProvider");
  }
  return context;
}

function ErrorToastViewport({
  toasts,
  onDismiss,
  onUpdate,
}: {
  toasts: ErrorToastItem[];
  onDismiss: (key: string) => void;
  onUpdate: (key: string, patch: Partial<ErrorToastItem>) => void;
}) {
  if (toasts.length === 0) return null;
  return (
    <div className="error-toast-viewport" aria-label="Error notifications">
      {toasts.map((toast) => (
        <ErrorToast
          key={toast.key}
          toast={toast}
          onDismiss={onDismiss}
          onUpdate={onUpdate}
        />
      ))}
    </div>
  );
}

function ErrorToast({
  toast,
  onDismiss,
  onUpdate,
}: {
  toast: ErrorToastItem;
  onDismiss: (key: string) => void;
  onUpdate: (key: string, patch: Partial<ErrorToastItem>) => void;
}) {
  const detailsId = useId();
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (toast.expanded || toast.hovered || toast.focused) return undefined;
    const timeout = window.setTimeout(() => onDismiss(toast.key), AUTO_DISMISS_MS);
    return () => window.clearTimeout(timeout);
  }, [onDismiss, toast.expanded, toast.focused, toast.hovered, toast.key]);

  useEffect(() => {
    if (!copied) return undefined;
    const timeout = window.setTimeout(() => setCopied(false), 1_800);
    return () => window.clearTimeout(timeout);
  }, [copied]);

  const toggleDetails = () => {
    onUpdate(toast.key, { expanded: !toast.expanded });
  };

  const onFocusCapture = () => {
    onUpdate(toast.key, { focused: true });
  };

  const onBlurCapture = (event: FocusEvent<HTMLElement>) => {
    if (event.currentTarget.contains(event.relatedTarget)) return;
    onUpdate(toast.key, { focused: false });
  };

  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      onDismiss(toast.key);
    }
  };

  const copyErrorId = () => {
    if (!toast.error.id || !navigator.clipboard) return;
    void navigator.clipboard.writeText(toast.error.id).then(() => setCopied(true), () => undefined);
  };

  return (
    <section
      className={`error-toast${toast.expanded ? " error-toast--expanded" : ""}`}
      role="status"
      aria-live="polite"
      aria-atomic="true"
      data-source={toast.source}
      onMouseEnter={() => onUpdate(toast.key, { hovered: true })}
      onMouseLeave={() => onUpdate(toast.key, { hovered: false })}
      onFocusCapture={onFocusCapture}
      onBlurCapture={onBlurCapture}
      onKeyDown={onKeyDown}
    >
      <div className="error-toast__head">
        <button
          type="button"
          className="error-toast__summary"
          aria-expanded={toast.expanded}
          aria-controls={detailsId}
          aria-label={`${toast.expanded ? "Hide" : "Show"} error details: ${toast.error.message}`}
          onClick={toggleDetails}
        >
          <span className="error-toast__message">{toast.error.message}</span>
        </button>
        <button
          type="button"
          className="error-toast__close"
          aria-label="Dismiss error notification"
          onClick={() => onDismiss(toast.key)}
        >
          <X size={16} strokeWidth={2.2} aria-hidden="true" />
        </button>
      </div>

      {toast.expanded ? (
        <div className="error-toast__details" id={detailsId}>
          <dl className="error-toast__properties">
            <ErrorProperty label="Error ID" value={toast.error.id}>
              {toast.error.id ? (
                <button
                  type="button"
                  className="error-toast__copy"
                  aria-label="Copy error ID"
                  onClick={copyErrorId}
                >
                  {copied ? (
                    <>
                      <Check size={13} strokeWidth={2.5} aria-hidden="true" />
                      Copied
                    </>
                  ) : (
                    <>
                      <Copy size={13} strokeWidth={2} aria-hidden="true" />
                      Copy
                    </>
                  )}
                </button>
              ) : null}
            </ErrorProperty>
            <ErrorProperty label="Status" value={toast.error.status?.toString() ?? null} />
            <ErrorProperty label="Type" value={toast.error.type} />
            <ErrorProperty label="Title" value={toast.error.title} />
            <ErrorProperty label="Machine code" value={toast.error.machineCode} />
            <ErrorProperty label="Instance" value={toast.error.instance} />
            <ErrorProperty label="Request ID" value={toast.error.requestId} />
          </dl>
          <ErrorFieldList fieldErrors={toast.error.fieldErrors} />
          <ErrorDetailList details={toast.error.details} />
        </div>
      ) : null}
    </section>
  );
}

function ErrorProperty({
  children,
  label,
  value,
}: {
  children?: ReactNode;
  label: string;
  value: string | null;
}) {
  return (
    <div className="error-toast__property">
      <dt>{label}</dt>
      <dd>
        <span>{value ?? "Not provided"}</span>
        {children}
      </dd>
    </div>
  );
}

function ErrorFieldList({
  fieldErrors,
}: {
  fieldErrors: DisplayError["fieldErrors"];
}) {
  return (
    <section className="error-toast__block" aria-label="Field errors">
      <h2 className="error-toast__block-title">Field errors</h2>
      {fieldErrors.length > 0 ? (
        <ul className="error-toast__list">
          {fieldErrors.map((fieldError, index) => (
            <li key={`${fieldError.loc?.join(".") ?? "field"}-${index}`}>
              <span>{fieldError.msg ?? "Invalid field"}</span>
              {fieldError.loc?.length ? (
                <code>{fieldError.loc.map(String).join(".")}</code>
              ) : null}
              {fieldError.type ? <small>{fieldError.type}</small> : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="error-toast__empty">None</p>
      )}
    </section>
  );
}

function ErrorDetailList({ details }: { details: ReadonlyArray<DisplayErrorDetail> }) {
  return (
    <section className="error-toast__block" aria-label="Details">
      <h2 className="error-toast__block-title">Details</h2>
      {details.length > 0 ? (
        <ul className="error-toast__list">
          {details.map((detail, index) => (
            <li key={`${detail.label}-${detail.path ?? "detail"}-${index}`}>
              <span>{detail.label}: {detail.message}</span>
              {detail.path ? <code>{detail.path}</code> : null}
              {detail.type ? <small>{detail.type}</small> : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="error-toast__empty">None</p>
      )}
    </section>
  );
}

function moveToFront(items: ErrorToastItem[], index: number): ErrorToastItem[] {
  const [item] = items.splice(index, 1);
  if (!item) return items;
  return [item, ...items];
}

function errorFingerprint(error: DisplayError): string {
  return [
    error.id,
    error.requestId,
    error.status?.toString() ?? null,
    error.type,
    error.machineCode,
    error.message,
  ].filter((part): part is string => Boolean(part)).join("|");
}
