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
          <span className="tokens-audit__title">Request log</span>
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
  );
}
