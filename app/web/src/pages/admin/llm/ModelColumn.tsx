import { formatContextWindow } from "@/lib/numberFormat";
import type { LlmModel, LlmProviderModel } from "@/types";
import CapabilityTagChip from "./CapabilityTagChip";
import LlmUsageTotals, { formatUsageSummary } from "./LlmUsageTotals";
import { shouldOpenGraphEditor } from "./lib/clickTargets";
import type { LlmIndexes } from "./lib/llmIndexes";
import { thinkingLevelLabel } from "./lib/llmThinking";
import type { ElementRefSetter, NodeClass, SelectionSetter } from "./types";

interface ModelColumnProps {
  models: LlmModel[];
  setHover: SelectionSetter;
  setSelection: SelectionSetter;
  nodeClass: NodeClass;
  setModelRef: ElementRefSetter;
  setProviderModelRef: ElementRefSetter;
  onEditModel: (modelId: string) => void;
  onEditProviderModel: (providerModelId: string) => void;
  indexes: LlmIndexes;
  providerModelsByModelId: Map<string, LlmProviderModel[]>;
}

export default function ModelColumn(props: ModelColumnProps) {
  const {
    models,
    setHover,
    setSelection,
    nodeClass,
    setModelRef,
    setProviderModelRef,
    onEditModel,
    onEditProviderModel,
    indexes,
    providerModelsByModelId,
  } = props;

  return (
    <div className="llm-graph__col llm-graph__col--models">
      {models.map((m) => {
        const providerModels = providerModelsByModelId.get(m.id) ?? [];
        return (
          <article
            key={m.id}
            className={[
              nodeClass("model", m.id),
              m.is_active ? "" : "is-disabled",
            ]
              .filter(Boolean)
              .join(" ")}
          >
            <button
              type="button"
              ref={setModelRef(m.id)}
              className="llm-graph-node__button"
              onMouseEnter={() => setHover({ column: "model", id: m.id })}
              onMouseLeave={() => setHover(null)}
              onFocus={() => setHover({ column: "model", id: m.id })}
              onBlur={() => setHover(null)}
              onClick={(event) => {
                setSelection({ column: "model", id: m.id });
                if (shouldOpenGraphEditor(event)) onEditModel(m.id);
              }}
              aria-label={`${m.display_name} model, ${formatUsageSummary(
                m.calls_30d,
                m.spend_usd_30d,
              )}`}
            >
              <header className="llm-graph-node__head">
                <span className="llm-graph-node__name" data-llm-edit-target="true">
                  {m.display_name}
                </span>
              </header>
              <div className="llm-graph-node__meta mono" data-llm-edit-target="true">
                {m.canonical_name}
              </div>
              <div className="llm-graph-node__tags">
                {m.capabilities.map((tag) => (
                  <CapabilityTagChip key={tag} tag={tag} />
                ))}
                <span className="chip chip--ghost chip--sm llm-graph-node__thinking-chip">
                  Thinking {thinkingLevelLabel(m.thinking_level)}
                </span>
                {m.context_window ? (
                  <span className="chip chip--ghost chip--sm">
                    {formatContextWindow(m.context_window)}
                  </span>
                ) : null}
              </div>
              <LlmUsageTotals spendUsd={m.spend_usd_30d} calls={m.calls_30d} />
            </button>
            {providerModels.length ? (
              <div
                className="llm-provider-model-list"
                aria-label={`${m.display_name} provider models`}
              >
                {providerModels.map((pm) => {
                  const provider = indexes.providersById.get(pm.provider_id);
                  return (
                    <button
                      key={pm.id}
                      type="button"
                      ref={setProviderModelRef(pm.id)}
                      className={[
                        nodeClass("providerModel", pm.id),
                        pm.is_enabled ? "" : "is-disabled",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      onMouseEnter={(event) => {
                        event.stopPropagation();
                        setHover({ column: "providerModel", id: pm.id });
                      }}
                      onMouseLeave={() => setHover(null)}
                      onFocus={(event) => {
                        event.stopPropagation();
                        setHover({ column: "providerModel", id: pm.id });
                      }}
                      onBlur={() => setHover(null)}
                      onClick={(event) => {
                        event.stopPropagation();
                        setSelection({ column: "providerModel", id: pm.id });
                        onEditProviderModel(pm.id);
                      }}
                      aria-label={`${provider?.name ?? "Unknown provider"} provider model for ${m.display_name}, ${formatUsageSummary(
                        pm.calls_30d,
                        pm.spend_usd_30d,
                      )}`}
                    >
                      <span className="llm-provider-model-list__name">
                        {provider?.name ?? "Unknown provider"}
                      </span>
                      <LlmUsageTotals
                        spendUsd={pm.spend_usd_30d}
                        calls={pm.calls_30d}
                        variant="muted"
                      />
                    </button>
                  );
                })}
              </div>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}
