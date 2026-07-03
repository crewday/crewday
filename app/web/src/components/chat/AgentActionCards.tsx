import type { AgentAction } from "@/types/api";

// §11 "Inline approval UX" — the pending-approval card block shared by
// every chat surface (owner/worker rail on desktop, full-screen /chat on
// phone, admin rail). Purely presentational: the parent owns the approvals
// query and the decide mutation, and passes a stable-id `onDecide`. Wired
// to the `/approvals/{id}/{decision}` contract by every caller.
interface AgentActionCardsProps {
  actions: AgentAction[];
  onDecide: (id: string, decision: "approve" | "deny") => void;
}

export default function AgentActionCards({ actions, onDecide }: AgentActionCardsProps) {
  if (actions.length === 0) return null;
  return (
    <div className="agent-actions" aria-label="Pending agent actions">
      <div className="agent-actions__title">
        <span>Pending approvals</span>
        <span className="agent-actions__count">{actions.length}</span>
      </div>
      <div className="agent-actions__list">
        {actions.map((a) => (
          <div key={a.id} className={"agent-action agent-action--" + a.risk}>
            <div className="agent-action__title">{a.card_summary || a.title}</div>
            {a.card_fields.length > 0 && (
              <dl className="agent-action__fields">
                {a.card_fields.map(([k, v]) => (
                  <div key={k} className="agent-action__field">
                    <dt>{k}</dt>
                    <dd>{v}</dd>
                  </div>
                ))}
              </dl>
            )}
            <div className="agent-action__detail">{a.detail}</div>
            <div className="agent-action__ctas">
              <button
                type="button"
                className="btn btn--approve"
                onClick={() => onDecide(a.id, "approve")}
              >
                Confirm
              </button>
              <button
                type="button"
                className="btn btn--deny"
                onClick={() => onDecide(a.id, "deny")}
              >
                Reject
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
