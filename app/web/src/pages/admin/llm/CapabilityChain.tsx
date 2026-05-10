import type { LlmAssignment } from "@/types";
import LlmUsageTotals from "./LlmUsageTotals";
import type { LlmIndexes } from "./lib/llmIndexes";
import type { ElementRefSetter, Highlighted, Selection, SelectionSetter } from "./types";

interface CapabilityChainProps {
  chain: LlmAssignment[];
  indexes: LlmIndexes;
  active: Selection | null;
  hasActive: boolean;
  highlighted: Highlighted;
  setHover: SelectionSetter;
  setSelection: SelectionSetter;
  setRungRef: ElementRefSetter;
  onOpenAssignment: (assignmentId: string) => void;
}

export default function CapabilityChain(props: CapabilityChainProps) {
  const {
    chain,
    indexes,
    active,
    hasActive,
    highlighted,
    setHover,
    setSelection,
    setRungRef,
    onOpenAssignment,
  } = props;

  return (
    <ol className="llm-graph-chain">
      {chain.map((a) => {
        // code-health: ignore[ccn] Chain rung state is compact visual mapping over one assignment and should stay inline.
        const pm = indexes.pmById.get(a.provider_model_id);
        const model = pm ? indexes.modelsById.get(pm.model_id) : null;
        const provider = pm ? indexes.providersById.get(pm.provider_id) : null;
        const missing = indexes.issuesByAssignment.get(a.id) ?? [];
        const isActive = active?.column === "assignment" && active.id === a.id;
        const isLinked = highlighted.assignments.has(a.id);
        const rungClass = [
          "llm-graph-chain__rung",
          isActive ? "is-active" : "",
          isLinked && !isActive ? "is-linked" : "",
          hasActive && !isLinked ? "is-dim" : "",
          missing.length ? "is-error" : "",
          a.priority === 0 ? "is-primary" : "",
        ]
          .filter(Boolean)
          .join(" ");
        return (
          <li key={a.id} className="llm-graph-chain__item">
            <button
              type="button"
              ref={setRungRef(a.id)}
              className={rungClass}
              onMouseEnter={(e) => {
                e.stopPropagation();
                setHover({ column: "assignment", id: a.id });
              }}
              onMouseLeave={() => setHover(null)}
              onFocus={(e) => {
                e.stopPropagation();
                setHover({ column: "assignment", id: a.id });
              }}
              onBlur={() => setHover(null)}
              onClick={(e) => {
                e.stopPropagation();
                setSelection({ column: "assignment", id: a.id });
                onOpenAssignment(a.id);
              }}
              title={
                missing.length
                  ? `Missing required capability: ${missing.join(", ")}`
                  : undefined
              }
              aria-label={`${a.capability} assignment rung ${a.priority}, ${a.calls_30d.toLocaleString()} calls in 30 days`}
            >
              <span className="llm-graph-chain__prio">
                {a.priority === 0 ? "P" : a.priority}
              </span>
              <span className="llm-graph-chain__model mono">
                {model?.canonical_name ?? "(missing model)"}
              </span>
              <span className="llm-graph-chain__provider muted">
                via {provider?.name ?? "?"}
              </span>
              <span className="llm-graph-chain__usage">
                <LlmUsageTotals spendUsd={a.spend_usd_30d} calls={a.calls_30d} />
                <span className="llm-graph-chain__usage-breakout">
                  direct {a.direct_calls_30d.toLocaleString()} · inherited{" "}
                  {a.inherited_calls_30d.toLocaleString()}
                </span>
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}
