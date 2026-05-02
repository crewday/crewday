import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Loading } from "@/components/common";
import TokenRevealPanel from "@/components/TokenRevealPanel";
import type {
  ApiToken,
  ApiTokenCreated,
  ApiTokenListResponse,
} from "@/types/api";
import MintTokenModal from "./api-tokens/MintTokenModal";
import TokenRow from "./api-tokens/TokenRow";
import TokenAuditPanel from "./api-tokens/TokenAuditPanel";

// §03 API tokens — manager surface. Scoped + delegated workspace
// tokens live here. Personal access tokens (kind === "personal")
// are deliberately hidden — they live on /me, revocable only by
// the subject.

export default function ApiTokensPage() {
  const qc = useQueryClient();
  // §12 cursor envelope (cd-msu2). The page renders a single page —
  // pagination UI lands as a follow-up; today's per-user/per-workspace
  // caps keep the corpus well below the default limit.
  const listQ = useQuery({
    queryKey: qk.apiTokens(),
    queryFn: () => fetchJson<ApiTokenListResponse>("/api/v1/auth/tokens"),
  });

  const [showCreate, setShowCreate] = useState(false);
  const [justCreated, setJustCreated] = useState<ApiTokenCreated | null>(null);
  const [openAudit, setOpenAudit] = useState<string | null>(null);

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
    () => (listQ.data?.data ?? []).filter((t) => !t.revoked_at).length,
    [listQ.data],
  );
  const delegatedCount = useMemo(
    () =>
      (listQ.data?.data ?? []).filter((t) => t.kind === "delegated" && !t.revoked_at).length,
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

  const rows = listQ.data.data;

  return (
    <DeskPage title="API tokens" sub={sub} actions={actions}>
      {justCreated && (
        <TokenRevealPanel created={justCreated} onDismiss={() => setJustCreated(null)} />
      )}

      {showCreate && (
        <MintTokenModal
          onCreated={(created) => {
            setJustCreated(created);
            setShowCreate(false);
          }}
          onCancel={() => setShowCreate(false)}
        />
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
              {rows.map((t) => (
                <TokenRow
                  key={t.id}
                  token={t}
                  auditOpen={openAudit === t.id}
                  onToggleAudit={(id) =>
                    setOpenAudit((v) => (v === id ? null : id))
                  }
                  onRotate={(id) => rotateM.mutate(id)}
                  onRevoke={(id) => revokeM.mutate(id)}
                />
              ))}
            </tbody>
          </table>
        )}

        {openAudit && (
          <TokenAuditPanel
            tokenId={openAudit}
            onClose={() => setOpenAudit(null)}
          />
        )}
      </section>
    </DeskPage>
  );
}
