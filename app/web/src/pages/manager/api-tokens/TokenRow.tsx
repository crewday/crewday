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
  // code-health: ignore[nloc] Token row keeps reveal/copy/revoke controls beside the token metadata they affect.
  const st = statusOf(t);
  // §03 scopes ride the wire as a flat `{action_key: true}` map; the
  // row renders one pill per truthy entry. `Object.keys` is stable
  // enough for this presentational use; no need to sort here.
  const scopeKeys = Object.keys(t.scopes ?? {});
  return (
    <tr className={`tokens-row tokens-row--${st}`}>
      <td>
        <div className="tokens-name">
          <span className="tokens-name__title">{t.label}</span>
          <span className="tokens-name__id">{t.prefix}…</span>
        </div>
      </td>
      <td>
        <span className={`tokens-kind tokens-kind--${t.kind}`}>{t.kind}</span>
      </td>
      <td>
        {t.kind === "delegated" ? (
          <span className="tokens-scopes__inherit">
            Inherits the delegator's grants
          </span>
        ) : (
          <span className="tokens-scopes">
            {scopeKeys.map((s) => (
              <span key={s} className="tokens-scopes__pill">{s}</span>
            ))}
          </span>
        )}
      </td>
      <td>
        <div className="tokens-time">
          <span>{fmtDateTime(t.created_at)}</span>
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
              onClick={() => onToggleAudit(t.key_id)}
              title="Audit timeline"
            >
              <ScrollText size={13} strokeWidth={2} />{" "}
              {auditOpen ? "Hide" : "Audit"}
            </button>
            {!t.revoked_at && (
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => onRotate(t.key_id)}
                title="Rotate secret"
              >
                <RotateCw size={13} strokeWidth={2} /> Rotate
              </button>
            )}
            {!t.revoked_at && (
              <button
                type="button"
                className="btn btn--sm btn--rust"
                onClick={() => onRevoke(t.key_id)}
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
