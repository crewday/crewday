import { Chip } from "@/components/common";
import type { LlmModel } from "@/types";
import LlmUsageTotals, { formatUsageSummary } from "./LlmUsageTotals";
import { shouldOpenGraphEditor } from "./lib/clickTargets";
import type { LlmIndexes } from "./lib/llmIndexes";
import { thinkingStrategyLabel } from "./lib/llmThinking";
import type { ElementRefSetter, NodeClass, SelectionSetter } from "./types";

const CAPABILITY_TAG_LABEL: Record<string, string> = {
  chat: "chat",
  vision: "vision",
  audio_input: "audio",
  reasoning: "reasoning",
  function_calling: "tools",
  json_mode: "json",
  streaming: "stream",
};

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
  } = props;

  return (
    <div className="llm-graph__col llm-graph__col--models">
      {models.map((m) => {
        const providerModels = indexes.providerModelsByModelId.get(m.id) ?? [];
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
                  <Chip key={tag} tone="ghost" size="sm">
                    {CAPABILITY_TAG_LABEL[tag] ?? tag}
                  </Chip>
                ))}
              </div>
              {m.context_window ? (
                <footer className="llm-graph-node__foot">
                  <span className="muted">
                    {(m.context_window / 1000).toFixed(0)}k ctx
                  </span>
                  <span className="llm-graph-node__thinking">
                    Thinking {m.thinking_level} /{" "}
                    {thinkingStrategyLabel(m.thinking_strategy)}
                  </span>
                </footer>
              ) : (
                <div className="llm-graph-node__thinking">
                  Thinking {m.thinking_level} /{" "}
                  {thinkingStrategyLabel(m.thinking_strategy)}
                </div>
              )}
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
                      <span className="llm-provider-model-list__thinking">
                        Level{" "}
                        {pm.thinking_level_override
                          ? pm.thinking_level_override
                          : `inherits ${pm.effective_thinking_level}`}{" "}
                        / Strategy{" "}
                        {pm.thinking_strategy_override
                          ? thinkingStrategyLabel(pm.thinking_strategy_override)
                          : `inherits ${thinkingStrategyLabel(
                              pm.effective_thinking_strategy,
                            )}`}
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
