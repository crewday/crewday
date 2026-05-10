import { Chip } from "@/components/common";
import type { LlmProvider } from "@/types";
import LlmUsageTotals from "./LlmUsageTotals";
import type { ElementRefSetter, NodeClass, Selection, SelectionSetter } from "./types";

interface ProviderColumnProps {
  providers: LlmProvider[];
  selection: Selection | null;
  setHover: SelectionSetter;
  setSelection: SelectionSetter;
  nodeClass: NodeClass;
  setProviderRef: ElementRefSetter;
}

export default function ProviderColumn(props: ProviderColumnProps) {
  const { providers, selection, setHover, setSelection, nodeClass, setProviderRef } =
    props;

  return (
    <div className="llm-graph__col llm-graph__col--providers">
      {providers.map((p) => (
        <button
          key={p.id}
          type="button"
          ref={setProviderRef(p.id)}
          className={nodeClass("provider", p.id)}
          onMouseEnter={() => setHover({ column: "provider", id: p.id })}
          onMouseLeave={() => setHover(null)}
          onFocus={() => setHover({ column: "provider", id: p.id })}
          onBlur={() => setHover(null)}
          onClick={() =>
            setSelection(
              selection?.column === "provider" && selection.id === p.id
                ? null
                : { column: "provider", id: p.id },
            )
          }
          aria-label={`${p.name} provider, ${p.calls_30d.toLocaleString()} calls in 30 days`}
        >
          <header className="llm-graph-node__head">
            <span className="llm-graph-node__name">{p.name}</span>
            <Chip tone={p.is_enabled ? "moss" : "ghost"} size="sm">
              {p.is_enabled ? "on" : "off"}
            </Chip>
          </header>
          <div className="llm-graph-node__meta">
            <span className="llm-graph-node__type">{p.provider_type}</span>
            <span className="llm-graph-node__endpoint mono">
              {p.endpoint || "(unset)"}
            </span>
          </div>
          <footer className="llm-graph-node__foot">
            <span>
              {p.provider_model_count} model
              {p.provider_model_count === 1 ? "" : "s"}
            </span>
            {p.api_key_status === "missing" ? (
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
