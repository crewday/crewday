import { formatMoney } from "@/lib/money";
import type { LlmAssignment } from "@/types";
import type { LlmIndexes } from "./lib/llmIndexes";
import type { ElementRefSetter, Highlighted, SelectionSetter } from "./types";

interface CapabilityChainProps {
  chain: LlmAssignment[];
  indexes: LlmIndexes;
  hasActive: boolean;
  highlighted: Highlighted;
  setHover: SelectionSetter;
  setSelection: SelectionSetter;
  setRungRef: ElementRefSetter;
}

export default function CapabilityChain({
  chain,
  indexes,
  hasActive,
  highlighted,
  setHover,
  setSelection,
  setRungRef,
}: CapabilityChainProps) {
  return (
    <ol className="llm-graph-chain">
      {chain.map((a) => {
        const pm = indexes.pmById.get(a.provider_model_id);
        const model = pm ? indexes.modelsById.get(pm.model_id) : null;
        const provider = pm ? indexes.providersById.get(pm.provider_id) : null;
        const missing = indexes.issuesByAssignment.get(a.id) ?? [];
        const rungClass = [
          "llm-graph-chain__rung",
          hasActive && !highlighted.assignments.has(a.id) ? "is-dim" : "",
          missing.length ? "is-error" : "",
          a.priority === 0 ? "is-primary" : "",
        ]
          .filter(Boolean)
          .join(" ");
        return (
          <li
            key={a.id}
            ref={setRungRef(a.id)}
            className={rungClass}
            onMouseEnter={(e) => {
              e.stopPropagation();
              setHover({ column: "assignment", id: a.id });
            }}
            onClick={(e) => {
              e.stopPropagation();
              setSelection({ column: "assignment", id: a.id });
            }}
            title={
              missing.length
                ? `Missing required capability: ${missing.join(", ")}`
                : undefined
            }
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
            <span className="llm-graph-chain__spend mono">
              {formatMoney(Math.round(a.spend_usd_30d * 100), "USD")}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
