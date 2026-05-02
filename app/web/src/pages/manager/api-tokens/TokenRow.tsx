import { RotateCw, ScrollText, Trash2 } from "lucide-react";
import { fmtDateTime } from "@/lib/dates";
import type { ApiToken } from "@/types/api";
import { STATUS_LABEL, statusOf } from "./lib/tokenStatus";

interface TokenRowProps {
  token: ApiToken;
  auditOpen: boolean;
  onToggleAudit: (id: string) => void;
  onRotate: (id: string) => void;
  onRevoke: (id: string) => void;
}

export default function TokenRow({
  token: t,
  auditOpen,
  onToggleAudit,
  onRotate,
  onRevoke,
}: TokenRowProps) {
  const st = statusOf(t);
  return (
    <tr className={`tokens-row tokens-row--${st}`}>
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
              onClick={() => onToggleAudit(t.id)}
              title="Request log"
            >
              <ScrollText size={13} strokeWidth={2} />{" "}
              {auditOpen ? "Hide" : "Log"}
            </button>
            {!t.revoked_at && (
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => onRotate(t.id)}
                title="Rotate secret"
              >
                <RotateCw size={13} strokeWidth={2} /> Rotate
              </button>
            )}
            {!t.revoked_at && (
              <button
                type="button"
                className="btn btn--sm btn--rust"
                onClick={() => onRevoke(t.id)}
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
}
