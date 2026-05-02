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
          {/* §03 v1 surface: lifecycle events (mint / rotate / revoke /
              revoked_noop) only. A sibling per-request log lands later
              once the api_token_request_log table ships. */}
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
              <tr key={a.correlation_id + a.at}>
                <td className="tokens-audit__when">{fmtDateTime(a.at)}</td>
                <td>
                  <span className="tokens-audit__method">{a.action}</span>
                </td>
                <td className="tokens-audit__path">{a.actor_id}</td>
                <td className="tokens-audit__cid">{a.correlation_id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
