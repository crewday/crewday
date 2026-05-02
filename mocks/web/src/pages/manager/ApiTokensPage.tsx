import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, RotateCw, ScrollText, Trash2 } from "lucide-react";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { fmtDateTime } from "@/lib/dates";
import DeskPage from "@/components/DeskPage";
import { Loading } from "@/components/common";
import TokenRevealPanel from "@/components/TokenRevealPanel";
import type { ApiToken, ApiTokenAuditEntry, ApiTokenCreated } from "@/types/api";

// §03 API tokens — manager surface. Scoped + delegated workspace
// tokens live here. Personal access tokens (kind === "personal")
// are deliberately hidden — they live on /me, revocable only by
// the subject.

const WORKSPACE_SCOPES: string[] = [
  "tasks:read", "tasks:write", "tasks:complete",
  "users:read", "properties:read", "stays:read",
  "inventory:read", "inventory:adjust", "time:read",
  "expenses:read", "expenses:approve",
  "payroll:read", "payroll:run",
  "instructions:read", "messaging:read", "llm:call",
];

type Kind = "scoped" | "delegated";

type Status = "active" | "expiring" | "expired" | "revoked";

function statusOf(tok: ApiToken): Status {
  if (tok.revoked_at) return "revoked";
  const exp = tok.expires_at ? new Date(tok.expires_at).getTime() : null;
  if (exp !== null && exp < Date.now()) return "expired";
  if (exp !== null && exp - Date.now() < 14 * 864e5) return "expiring";
  return "active";
}

const STATUS_LABEL: Record<Status, string> = {
  active: "Active",
  expiring: "Expires soon",
  expired: "Expired",
  revoked: "Revoked",
};

export default function ApiTokensPage() {
  const qc = useQueryClient();
  const listQ = useQuery({
    queryKey: qk.apiTokens(),
    queryFn: () => fetchJson<ApiToken[]>("/api/v1/auth/tokens"),
  });

  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("my-script");
  const [kind, setKind] = useState<Kind>("scoped");
  const [picked, setPicked] = useState<Set<string>>(new Set(["tasks:read"]));
  const [expiryDays, setExpiryDays] = useState(90);
  const [note, setNote] = useState("");
  const [justCreated, setJustCreated] = useState<ApiTokenCreated | null>(null);
  const [openAudit, setOpenAudit] = useState<string | null>(null);

  const auditQ = useQuery({
    queryKey: qk.apiTokenAudit(openAudit ?? ""),
    enabled: Boolean(openAudit),
    queryFn: () => fetchJson<ApiTokenAuditEntry[]>(`/api/v1/auth/tokens/${openAudit}/audit`),
  });

  const createM = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      fetchJson<ApiTokenCreated>("/api/v1/auth/tokens", { method: "POST", body }),
    onSuccess: (created) => {
      setJustCreated(created);
      setShowCreate(false);
      qc.invalidateQueries({ queryKey: qk.apiTokens() });
    },
  });

  const revokeM = useMutation({
    mutationFn: (id: string) =>
      fetchJson<ApiToken>(`/api/v1/auth/tokens/${id}/revoke`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.apiTokens() }),
  });

  const rotateM = useMutation({
    mutationFn: (id: string) =>
      fetchJson<ApiTokenCreated>(`/api/v1/auth/tokens/${id}/rotate`, { method: "POST" }),
    onSuccess: (created) => {
      setJustCreated(created);
      qc.invalidateQueries({ queryKey: qk.apiTokens() });
    },
  });

  const liveCount = useMemo(
    () => (listQ.data ?? []).filter((t) => !t.revoked_at).length,
    [listQ.data],
  );
  const delegatedCount = useMemo(
    () => (listQ.data ?? []).filter((t) => t.kind === "delegated" && !t.revoked_at).length,
    [listQ.data],
  );

  const sub =
    "Bearer tokens for scripts, agents, and integrations. " +
    "Scoped tokens carry explicit scopes; delegated tokens inherit your permissions.";
  const actions = (
    <button
      type="button"
      className="btn btn--moss"
      onClick={() => {
        setJustCreated(null);
        setShowCreate((v) => !v);
      }}
    >
      + New token
    </button>
  );

  if (listQ.isPending) {
    return <DeskPage title="API tokens" sub={sub} actions={actions}><Loading /></DeskPage>;
  }
  if (!listQ.data) {
    return <DeskPage title="API tokens" sub={sub} actions={actions}>Failed to load.</DeskPage>;
  }

  const rows = listQ.data;

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
    <DeskPage title="API tokens" sub={sub} actions={actions}>
      {justCreated && (
        <TokenRevealPanel created={justCreated} onDismiss={() => setJustCreated(null)} />
      )}

      {showCreate && (
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
                  onClick={() => setShowCreate(false)}
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
      )}

      <section className="panel">
        <header className="panel__head">
          <h2>Workspace tokens</h2>
          <div className="tokens-meta">
            <span className="tokens-meta__stat">
              <span className="tokens-meta__stat-value">{liveCount}</span>
              <span className="tokens-meta__stat-label">live</span>
            </span>
            <span className="tokens-meta__divider" aria-hidden="true" />
            <span className="tokens-meta__stat">
              <span className="tokens-meta__stat-value">{delegatedCount}</span>
              <span className="tokens-meta__stat-label">delegated</span>
            </span>
            <span className="tokens-meta__divider" aria-hidden="true" />
            <span className="tokens-meta__stat">
              <span className="tokens-meta__stat-value">50</span>
              <span className="tokens-meta__stat-label">cap</span>
            </span>
            <span className="tokens-meta__divider" aria-hidden="true" />
            <span className="tokens-meta__hint">
              personal tokens live on <Link to="/me">/me</Link>
            </span>
          </div>
        </header>

        {rows.length === 0 ? (
          <div className="tokens-empty">
            <span className="tokens-empty__glyph" aria-hidden="true">
              <KeyRound size={20} strokeWidth={1.75} />
            </span>
            <p className="tokens-empty__title">No workspace tokens yet</p>
            <p className="tokens-empty__sub">
              Create one to let a script or agent hit the crew.day API on your behalf.
            </p>
          </div>
        ) : (
          <table className="tokens-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Kind</th>
                <th>Scopes</th>
                <th>Created</th>
                <th>Expires</th>
                <th>Last used</th>
                <th>Status</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => {
                const st = statusOf(t);
                return (
                  <tr key={t.id} className={`tokens-row tokens-row--${st}`}>
                    <td>
                      <div className="tokens-name">
                        <span className="tokens-name__title">{t.name}</span>
                        <span className="tokens-name__id">{t.prefix}…</span>
                        {t.note ? <span className="tokens-name__note">{t.note}</span> : null}
                      </div>
                    </td>
                    <td>
                      <span className={`tokens-kind tokens-kind--${t.kind}`}>{t.kind}</span>
                    </td>
                    <td>
                      {t.kind === "delegated" ? (
                        <span className="tokens-scopes__inherit">
                          Inherits {t.created_by_display}'s grants
                        </span>
                      ) : (
                        <span className="tokens-scopes">
                          {t.scopes.map((s) => (
                            <span key={s} className="tokens-scopes__pill">{s}</span>
                          ))}
                        </span>
                      )}
                    </td>
                    <td>
                      <div className="tokens-time">
                        <span>{fmtDateTime(t.created_at)}</span>
                        <span className="tokens-time__sub">by {t.created_by_display}</span>
                      </div>
                    </td>
                    <td>
                      {t.expires_at ? (
                        <div className="tokens-time">
                          <span>{fmtDateTime(t.expires_at)}</span>
                        </div>
                      ) : (
                        <span className="tokens-time--absent">never</span>
                      )}
                    </td>
                    <td>
                      {t.last_used_at ? (
                        <div className="tokens-time">
                          <span>{fmtDateTime(t.last_used_at)}</span>
                          <span className="tokens-time__ip">{t.last_used_ip}</span>
                        </div>
                      ) : (
                        <span className="tokens-time--absent">never</span>
                      )}
                    </td>
                    <td>
                      <span className={`tokens-status tokens-status--${st}`}>
                        {STATUS_LABEL[st]}
                      </span>
                    </td>
                    <td>
                      <div className="tokens-row-actions">
                        <div className="tokens-row-actions__primary">
                          <button
                            type="button"
                            className="btn btn--sm btn--ghost"
                            onClick={() => setOpenAudit((v) => (v === t.id ? null : t.id))}
                            title="Request log"
                          >
                            <ScrollText size={13} strokeWidth={2} />{" "}
                            {openAudit === t.id ? "Hide" : "Log"}
                          </button>
                          {!t.revoked_at && (
                            <button
                              type="button"
                              className="btn btn--sm btn--ghost"
                              onClick={() => rotateM.mutate(t.id)}
                              title="Rotate secret"
                            >
                              <RotateCw size={13} strokeWidth={2} /> Rotate
                            </button>
                          )}
                          {!t.revoked_at && (
                            <button
                              type="button"
                              className="btn btn--sm btn--rust"
                              onClick={() => revokeM.mutate(t.id)}
                              title="Revoke"
                            >
                              <Trash2 size={13} strokeWidth={2} /> Revoke
                            </button>
                          )}
                        </div>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}

        {openAudit && (
          <div className="tokens-audit">
            <header className="tokens-audit__head">
              <div>
                <span className="tokens-audit__title">Request log</span>
                <span className="tokens-audit__title-tag">{openAudit}</span>
              </div>
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                onClick={() => setOpenAudit(null)}
              >
                Close
              </button>
            </header>
            {auditQ.isPending ? (
              <Loading />
            ) : (auditQ.data ?? []).length === 0 ? (
              <p className="tokens-audit__empty">No requests recorded yet.</p>
            ) : (
              <table className="tokens-audit__table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Method</th>
                    <th>Path</th>
                    <th>Status</th>
                    <th>IP</th>
                    <th>Correlation</th>
                  </tr>
                </thead>
                <tbody>
                  {(auditQ.data ?? []).map((a) => (
                    <tr key={a.correlation_id + a.at}>
                      <td className="tokens-audit__when">{fmtDateTime(a.at)}</td>
                      <td>
                        <span className="tokens-audit__method">{a.method}</span>
                      </td>
                      <td className="tokens-audit__path">{a.path}</td>
                      <td>
                        <span
                          className={
                            "tokens-audit__status " +
                            (a.status < 400
                              ? "tokens-audit__status--ok"
                              : "tokens-audit__status--fail")
                          }
                        >
                          {a.status}
                        </span>
                      </td>
                      <td className="tokens-audit__ip">{a.ip}</td>
                      <td className="tokens-audit__cid">{a.correlation_id}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </section>
    </DeskPage>
  );
}
