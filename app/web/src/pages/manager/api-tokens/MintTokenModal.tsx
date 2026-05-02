import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { ApiTokenCreated } from "@/types/api";
import { type TokenKind, WORKSPACE_SCOPES } from "./lib/tokenStatus";

interface MintTokenModalProps {
  onCreated: (created: ApiTokenCreated) => void;
  onCancel: () => void;
}

export default function MintTokenModal({ onCreated, onCancel }: MintTokenModalProps) {
  const qc = useQueryClient();
  const [name, setName] = useState("my-script");
  const [kind, setKind] = useState<TokenKind>("scoped");
  const [picked, setPicked] = useState<Set<string>>(new Set(["tasks:read"]));
  const [expiryDays, setExpiryDays] = useState(90);
  const [note, setNote] = useState("");

  const createM = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      fetchJson<ApiTokenCreated>("/api/v1/auth/tokens", { method: "POST", body }),
    onSuccess: (created) => {
      onCreated(created);
      qc.invalidateQueries({ queryKey: qk.apiTokens() });
    },
  });

  function togglePick(key: string) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function submitCreate(e: React.FormEvent) {
    e.preventDefault();
    const expires = new Date(Date.now() + expiryDays * 864e5).toISOString();
    createM.mutate({
      name,
      delegate: kind === "delegated",
      scopes: kind === "delegated" ? [] : Array.from(picked),
      expires_at: expires,
      note: note || null,
    });
  }

  return (
    <section className="panel">
      <header className="panel__head">
        <h2>New workspace token</h2>
      </header>

      <form className="tokens-form" onSubmit={submitCreate}>
        <div className="tokens-form__section">
          <label className="tokens-form__legend" htmlFor="tok-name">
            Name
            <span className="tokens-form__legend-hint">
              a human label that shows up in the audit log
            </span>
          </label>
          <input
            id="tok-name"
            className="tokens-name-input"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-script"
            maxLength={80}
            required
          />
        </div>

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
                name="kind"
                checked={kind === "scoped"}
                onChange={() => setKind("scoped")}
              />
              <span className="tokens-kind-picker__title">Scoped</span>
              <span className="tokens-kind-picker__sub">
                Pick the exact verbs your script needs. Bypasses your role grants — stays
                valid even if you lose access later.
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
                name="kind"
                checked={kind === "delegated"}
                onChange={() => setKind("delegated")}
              />
              <span className="tokens-kind-picker__title">Delegated</span>
              <span className="tokens-kind-picker__sub">
                Inherits your grants at request time. Dies the moment your account is archived
                or your role changes. Used by embedded chat agents.
              </span>
            </label>
          </div>
        </div>

        {kind === "scoped" && (
          <div className="tokens-form__section">
            <div className="tokens-form__legend">
              Scopes
              <span className="tokens-form__legend-hint">
                {picked.size} selected — narrow is safer
              </span>
            </div>
            <div className="tokens-scope-picker">
              {WORKSPACE_SCOPES.map((s) => {
                const on = picked.has(s);
                return (
                  <label
                    key={s}
                    className={
                      "tokens-scope-picker__pill" +
                      (on ? " tokens-scope-picker__pill--on" : "")
                    }
                  >
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() => togglePick(s)}
                    />
                    {s}
                  </label>
                );
              })}
            </div>
          </div>
        )}

        <div className="tokens-form__row">
          <div className="tokens-form__section">
            <div className="tokens-form__legend">Expires in</div>
            <div className="tokens-expiry">
              {[7, 30, 90, 365].map((d) => (
                <button
                  key={d}
                  type="button"
                  className={
                    "tokens-expiry__preset" +
                    (expiryDays === d ? " tokens-expiry__preset--on" : "")
                  }
                  onClick={() => setExpiryDays(d)}
                >
                  {d === 365 ? "1 year" : `${d} days`}
                </button>
              ))}
            </div>
          </div>
          <div className="tokens-form__section">
            <label className="tokens-form__legend" htmlFor="tok-note">
              Note
              <span className="tokens-form__legend-hint">optional · private to the workspace</span>
            </label>
            <input
              id="tok-note"
              type="text"
              className="tokens-note-input"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="e.g. Hermes scheduler on the dev box"
            />
          </div>
        </div>

        {createM.isError && (
          <p className="tokens-form__error">
            {(createM.error as Error)?.message ?? "Create failed"}
          </p>
        )}

        <div className="tokens-form__actions">
          <div className="tokens-form__actions-hint">
            The plaintext secret is shown exactly once on the next screen. We store only an
            argon2id hash — if you lose it, rotate.
          </div>
          <div className="tokens-form__actions-buttons">
            <button
              type="button"
              className="btn btn--ghost"
              onClick={onCancel}
            >
              Cancel
            </button>
            <button type="submit" className="btn btn--moss" disabled={createM.isPending}>
              {createM.isPending ? "Creating…" : "Create token"}
            </button>
          </div>
        </div>
      </form>
    </section>
  );
}
