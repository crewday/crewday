import { Chip } from "@/components/common";
import type { LlmProvider } from "@/types";
import LlmUsageTotals from "./LlmUsageTotals";
import { formatUsageSummary } from "./LlmUsageTotals.lib";
import { shouldOpenGraphEditor } from "./lib/clickTargets";
import type { ElementRefSetter, NodeClass, SelectionSetter } from "./types";

interface ProviderColumnProps {
  providers: LlmProvider[];
  setHover: SelectionSetter;
  setSelection: SelectionSetter;
  onEditProvider: (providerId: string) => void;
  nodeClass: NodeClass;
  setProviderRef: ElementRefSetter;
}

function providerTypeLabel(provider: LlmProvider): string {
  if (provider.provider_type === "openrouter") return "OpenRouter provider";
  if (provider.provider_type === "openai_compatible") return "OpenAI compatible";
  if (provider.provider_type === "ollama") return "Ollama";
  if (provider.provider_type === "local_embedding") return "Local embedding";
  return "Fake";
}

export default function ProviderColumn(props: ProviderColumnProps) {
  const {
    providers,
    setHover,
    setSelection,
    onEditProvider,
    nodeClass,
    setProviderRef,
  } = props;

  return (
    <div className="llm-graph__col llm-graph__col--providers">
      {providers.map((p) => (
        <button
          key={p.id}
          type="button"
          ref={setProviderRef(p.id)}
          className={[
            nodeClass("provider", p.id),
            p.is_enabled ? "" : "is-disabled",
          ]
            .filter(Boolean)
            .join(" ")}
          onMouseEnter={() => setHover({ column: "provider", id: p.id })}
          onMouseLeave={() => setHover(null)}
          onFocus={() => setHover({ column: "provider", id: p.id })}
          onBlur={() => setHover(null)}
          onClick={(event) => {
            setSelection({ column: "provider", id: p.id });
            if (shouldOpenGraphEditor(event)) onEditProvider(p.id);
          }}
          aria-label={`${p.name} provider, ${formatUsageSummary(
            p.calls_30d,
            p.spend_usd_30d,
          )}`}
        >
          <header className="llm-graph-node__head">
            <span className="llm-graph-node__name" data-llm-edit-target="true">
              {p.name}
            </span>
          </header>
          <div className="llm-graph-node__meta" data-llm-edit-target="true">
            <span className="llm-graph-node__type">{providerTypeLabel(p)}</span>
            <span className="llm-graph-node__endpoint mono">
              {p.endpoint || "(unset)"}
            </span>
          </div>
          <footer className="llm-graph-node__foot">
            {p.provider_type === "local_embedding" ? (
              <Chip tone="sky" size="sm">
                local
              </Chip>
            ) : p.api_key_status === "missing" ? (
              <Chip tone="rust" size="sm">
                no key
              </Chip>
            ) : p.api_key_status === "rotating" ? (
              <Chip tone="sand" size="sm">
                rotating
              </Chip>
            ) : (
              <Chip tone="sky" size="sm">
                key set
              </Chip>
            )}
          </footer>
          <LlmUsageTotals spendUsd={p.spend_usd_30d} calls={p.calls_30d} />
        </button>
      ))}
    </div>
  );
}
