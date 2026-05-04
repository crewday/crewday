import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { fmtDateTime } from "@/lib/dates";
import { Loading } from "@/components/common";
import type { ApiTokenAuditEntry } from "@/types/api";

interface TokenAuditPanelProps {
  tokenId: string;
  onClose: () => void;
}

function statusClass(status: number): string {
  return status >= 400
    ? "tokens-audit__status tokens-audit__status--fail"
    : "tokens-audit__status tokens-audit__status--ok";
}

function renderAction(a: ApiTokenAuditEntry) {
  if (a.method && a.path) {
    return (
      <div className="tokens-audit__request">
        <div className="tokens-audit__request-main">
          <span className="tokens-audit__method">{a.method}</span>
          <span className="tokens-audit__request-path">{a.path}</span>
        </div>
        <div className="tokens-audit__request-meta">
          {a.status !== null ? (
            <span className={statusClass(a.status)}>{a.status}</span>
          ) : null}
          {a.ip_prefix ? <span className="tokens-audit__ip">{a.ip_prefix}</span> : null}
          {a.user_agent ? <span className="tokens-audit__ua">{a.user_agent}</span> : null}
        </div>
      </div>
    );
  }
  return <span className="tokens-audit__method">{a.action}</span>;
}

export default function TokenAuditPanel({ tokenId, onClose }: TokenAuditPanelProps) {
  const auditQ = useQuery({
    queryKey: qk.apiTokenAudit(tokenId),
    queryFn: () => fetchJson<ApiTokenAuditEntry[]>(`/api/v1/auth/tokens/${tokenId}/audit`),
  });

  return (
    <div className="tokens-audit">
      <header className="tokens-audit__head">
        <div>
          <span className="tokens-audit__title">Audit timeline</span>
          <span className="tokens-audit__title-tag">{tokenId}</span>
        </div>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={onClose}
        >
          Close
        </button>
      </header>
      {auditQ.isPending ? (
        <Loading />
      ) : (auditQ.data ?? []).length === 0 ? (
        <p className="tokens-audit__empty">No audit events recorded yet.</p>
      ) : (
        <table className="tokens-audit__table">
          <thead>
            <tr>
              <th>When</th>
              <th>Action</th>
              <th>Actor</th>
              <th>Correlation</th>
            </tr>
          </thead>
          <tbody>
            {(auditQ.data ?? []).map((a) => (
              <tr key={a.correlation_id + a.at + a.action + (a.path ?? "")}>
                <td className="tokens-audit__when">{fmtDateTime(a.at)}</td>
                <td>{renderAction(a)}</td>
                <td className="tokens-audit__actor">{a.actor_id}</td>
                <td className="tokens-audit__cid">{a.correlation_id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
