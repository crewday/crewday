import { useState } from "react";
import type { FormEvent } from "react";
import { Check } from "lucide-react";
import type { ApiTokenKind } from "@/types/api";

export interface TokenScopeOption {
  key: string;
  hint?: string;
  verb?: string;
}

export interface TokenCreatePayload {
  label: string;
  scopes: Record<string, true>;
  expires_at_days?: number;
  never_expires?: true;
  delegate?: true;
}

interface TokenCreateFormProps {
  labelId: string;
  initialLabel: string;
  labelPlaceholder: string;
  labelMaxLength: number;
  scopes: TokenScopeOption[];
  initialScopes: string[];
  scopeTone?: "workspace" | "personal";
  allowDelegated?: boolean;
  isPending: boolean;
  error?: Error | null;
  submitLabel?: string;
  pendingLabel?: string;
  actionHint: string;
  onSubmit: (payload: TokenCreatePayload) => void;
  onCancel: () => void;
}

type ExpiryChoice = 7 | 30 | 90 | 365 | "never";
type KindChoice = Exclude<ApiTokenKind, "personal">;

const EXPIRY_CHOICES: ExpiryChoice[] = [7, 30, 90, 365, "never"];

function expiryLabel(choice: ExpiryChoice): string {
  if (choice === "never") return "Never expires";
  if (choice === 365) return "1 year";
  return `${choice} days`;
}

export default function TokenCreateForm(props: TokenCreateFormProps) {
  const {
    labelId,
    initialLabel,
    labelPlaceholder,
    labelMaxLength,
    scopes,
    initialScopes,
    scopeTone = "workspace",
    allowDelegated = false,
    isPending,
    error = null,
    submitLabel = "Create token",
    pendingLabel = "Creating...",
    actionHint,
    onSubmit,
    onCancel,
  } = props;
  // code-health: ignore[nloc] Token creation keeps kind, scope, expiry, and submit controls together so audit-sensitive payload mapping remains local.
  const [label, setLabel] = useState(initialLabel);
  const [kind, setKind] = useState<KindChoice>("scoped");
  const [picked, setPicked] = useState<Set<string>>(new Set(initialScopes));
  const [expiry, setExpiry] = useState<ExpiryChoice>(90);

  const selectedCount = kind === "delegated" ? 0 : picked.size;
  const allSelected = scopes.length > 0 && picked.size === scopes.length;
  const personal = scopeTone === "personal";

  function togglePick(key: string) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function selectAllScopes() {
    setPicked(new Set(scopes.map((scope) => scope.key)));
  }

  function submitCreate(e: FormEvent) {
    e.preventDefault();
    const body: TokenCreatePayload = {
      label,
      scopes:
        kind === "delegated"
          ? {}
          : Object.fromEntries(Array.from(picked).map((key) => [key, true])),
      ...(kind === "delegated" ? { delegate: true } : {}),
      ...(expiry === "never" ? { never_expires: true } : { expires_at_days: expiry }),
    };
    onSubmit(body);
  }

  return (
    <form className="tokens-form" onSubmit={submitCreate}>
      <div className="tokens-form__section">
        <label className="tokens-form__legend" htmlFor={labelId}>
          Name
          <span className="tokens-form__legend-hint">
            a human label that shows up in the audit log
          </span>
        </label>
        <input
          id={labelId}
          className="tokens-name-input"
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder={labelPlaceholder}
          maxLength={labelMaxLength}
          required
        />
      </div>

      {allowDelegated && (
        <div className="tokens-form__section">
          <div className="tokens-form__legend">Kind</div>
          <div className="tokens-kind-picker">
            <label
              className={
                "tokens-kind-picker__opt" +
                (kind === "scoped" ? " tokens-kind-picker__opt--active" : "")
              }
            >
              <input
                type="radio"
                name={`${labelId}-kind`}
                checked={kind === "scoped"}
                onChange={() => setKind("scoped")}
              />
              <span className="tokens-kind-picker__title">Scoped</span>
              <span className="tokens-kind-picker__sub">
                Pick the exact verbs your script needs. Bypasses your role grants and
                stays valid even if your access changes later.
              </span>
            </label>
            <label
              className={
                "tokens-kind-picker__opt" +
                (kind === "delegated" ? " tokens-kind-picker__opt--active" : "")
              }
            >
              <input
                type="radio"
                name={`${labelId}-kind`}
                checked={kind === "delegated"}
                onChange={() => setKind("delegated")}
              />
              <span className="tokens-kind-picker__title">Delegated</span>
              <span className="tokens-kind-picker__sub">
                Inherits your grants at request time. Dies the moment your account is
                archived or your role changes. Used by embedded chat agents.
              </span>
            </label>
          </div>
        </div>
      )}

      {kind === "scoped" && (
        <div className="tokens-form__section">
          <div className="tokens-form__legend tokens-form__legend--with-action">
            <span>
              Scopes
              <span className="tokens-form__legend-hint">
                {selectedCount} selected
                {personal
                  ? " - each one only reads/writes your own data"
                  : " - narrow is safer"}
              </span>
            </span>
            <button
              type="button"
              className="btn btn--ghost btn--sm tokens-form__legend-action"
              disabled={allSelected}
              onClick={selectAllScopes}
            >
              Add all scopes
            </button>
          </div>

          {personal ? (
            <ul className="tokens-scope-list">
              {scopes.map((scope) => {
                const on = picked.has(scope.key);
                return (
                  <li key={scope.key}>
                    <label
                      className={
                        "tokens-scope-list__item" +
                        (on ? " tokens-scope-list__item--on" : "")
                      }
                    >
                      <input
                        type="checkbox"
                        checked={on}
                        onChange={() => togglePick(scope.key)}
                      />
                      <span className="tokens-scope-list__check" aria-hidden="true">
                        <Check size={12} strokeWidth={2.5} />
                      </span>
                      <span className="tokens-scope-list__key">{scope.key}</span>
                      {scope.verb && (
                        <span className="tokens-scope-list__badge">{scope.verb}</span>
                      )}
                      {scope.hint && (
                        <span className="tokens-scope-list__hint">{scope.hint}</span>
                      )}
                    </label>
                  </li>
                );
              })}
            </ul>
          ) : (
            <div className="tokens-scope-picker">
              {scopes.map((scope) => {
                const on = picked.has(scope.key);
                return (
                  <label
                    key={scope.key}
                    className={
                      "tokens-scope-picker__pill" +
                      (on ? " tokens-scope-picker__pill--on" : "")
                    }
                  >
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() => togglePick(scope.key)}
                    />
                    {scope.key}
                  </label>
                );
              })}
            </div>
          )}
        </div>
      )}

      <div className="tokens-form__section">
        <div className="tokens-form__legend">Expires in</div>
        <div className="tokens-expiry">
          {EXPIRY_CHOICES.map((choice) => (
            <button
              key={choice}
              type="button"
              className={
                "tokens-expiry__preset" +
                (expiry === choice ? " tokens-expiry__preset--on" : "") +
                (choice === "never" ? " tokens-expiry__preset--never" : "")
              }
              onClick={() => setExpiry(choice)}
            >
              {expiryLabel(choice)}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <p className="tokens-form__error">{error.message || "Create failed"}</p>
      )}

      <div className="tokens-form__actions">
        <div className="tokens-form__actions-hint">{actionHint}</div>
        <div className="tokens-form__actions-buttons">
          <button type="button" className="btn btn--ghost" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="submit"
            className="btn btn--moss"
            disabled={isPending || (kind === "scoped" && picked.size === 0)}
          >
            {isPending ? pendingLabel : submitLabel}
          </button>
        </div>
      </div>
    </form>
  );
}
