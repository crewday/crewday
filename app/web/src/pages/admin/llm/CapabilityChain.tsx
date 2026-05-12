import type { LlmAssignment } from "@/types";
import LlmUsageTotals, { formatUsageSummary } from "./LlmUsageTotals";
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
  onOpenProviderModel: (providerModelId: string) => void;
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
    onOpenProviderModel,
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
            <div
              ref={setRungRef(a.id)}
              className={rungClass}
              onMouseEnter={(e) => {
                e.stopPropagation();
                if (e.target === e.currentTarget) {
                  setHover({ column: "assignment", id: a.id });
                }
              }}
              onMouseLeave={() => setHover(null)}
              onFocus={(e) => {
                e.stopPropagation();
                if (e.target === e.currentTarget) {
                  setHover({ column: "assignment", id: a.id });
                }
              }}
              onBlur={() => setHover(null)}
              title={
                missing.length
                  ? `Missing required capability: ${missing.join(", ")}`
                  : undefined
              }
            >
              <button
                type="button"
                className="llm-graph-chain__assignment"
                onMouseOver={(e) => {
                  e.stopPropagation();
                  setHover({ column: "assignment", id: a.id });
                }}
                onMouseEnter={(e) => {
                  e.stopPropagation();
                  setHover({ column: "assignment", id: a.id });
                }}
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
                aria-label={`${a.capability} assignment rung ${a.priority}, ${formatUsageSummary(
                  a.calls_30d,
                  a.spend_usd_30d,
                )}`}
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
                </span>
              </button>
              {pm ? (
                <button
                  type="button"
                  className={[
                    "llm-graph-chain__provider-model",
                    active?.column === "providerModel" && active.id === pm.id
                      ? "is-active"
                      : "",
                    highlighted.providerModels.has(pm.id) &&
                    !(active?.column === "providerModel" && active.id === pm.id)
                      ? "is-linked"
                      : "",
                    hasActive && !highlighted.providerModels.has(pm.id) ? "is-dim" : "",
                    pm.is_enabled ? "" : "is-disabled",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  onMouseEnter={(e) => {
                    e.stopPropagation();
                    setHover({ column: "providerModel", id: pm.id });
                  }}
                  onMouseOver={(e) => {
                    e.stopPropagation();
                    setHover({ column: "providerModel", id: pm.id });
                  }}
                  onMouseLeave={(e) => {
                    e.stopPropagation();
                    const nextTarget = e.relatedTarget;
                    const staysInsideRung =
                      nextTarget instanceof Node &&
                      e.currentTarget.parentElement?.contains(nextTarget);
                    setHover(staysInsideRung ? { column: "assignment", id: a.id } : null);
                  }}
                  onFocus={(e) => {
                    e.stopPropagation();
                    setHover({ column: "providerModel", id: pm.id });
                  }}
                  onBlur={() => setHover(null)}
                  onClick={(e) => {
                    e.stopPropagation();
                    setSelection({ column: "providerModel", id: pm.id });
                    onOpenProviderModel(pm.id);
                  }}
                  aria-label={`Open provider-model ${pm.api_model_id} for ${a.capability} assignment, ${provider?.name ?? "Unknown provider"}, ${model?.display_name ?? "unknown model"}, ${formatUsageSummary(
                    pm.calls_30d,
                    pm.spend_usd_30d,
                  )}`}
                >
                  <span className="llm-graph-chain__pm-name">
                    {provider?.name ?? "Unknown provider"}
                  </span>
                  <span className="llm-graph-chain__pm-model mono">
                    {pm.api_model_id}
                  </span>
                </button>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
