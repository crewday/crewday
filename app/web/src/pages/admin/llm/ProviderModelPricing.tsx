import { Chip } from "@/components/common";
import type { LlmGraphPayload } from "@/types";
import type { LlmIndexes } from "./lib/llmIndexes";

function hms(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

interface ProviderModelPricingProps {
  graph: LlmGraphPayload;
  indexes: LlmIndexes;
}

export default function ProviderModelPricing({
  graph,
  indexes,
}: ProviderModelPricingProps) {
  return (
    <div className="panel">
      <header className="panel__head">
        <h2>Provider-model pricing</h2>
        <span className="muted">From OpenRouter weekly; pinned rows skip the sync.</span>
      </header>
      <table className="table">
        <thead>
          <tr>
            <th>Provider × Model</th>
            <th>API model id</th>
            <th>Input / 1M</th>
            <th>Output / 1M</th>
            <th>Last synced</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {graph.provider_models.map((pm) => {
            const provider = indexes.providersById.get(pm.provider_id);
            const model = indexes.modelsById.get(pm.model_id);
            const pinned = pm.price_source_override === "none";
            const free =
              pm.input_cost_per_million === 0 && pm.output_cost_per_million === 0;
            return (
              <tr key={pm.id}>
                <td>
                  {provider?.name ?? "?"}
                  <span className="muted"> × </span>
                  {model?.display_name ?? "?"}
                </td>
                <td className="mono">{pm.api_model_id}</td>
                <td className="mono">${pm.input_cost_per_million.toFixed(3)}</td>
                <td className="mono">${pm.output_cost_per_million.toFixed(3)}</td>
                <td className="mono muted">
                  {pm.price_last_synced_at ? hms(pm.price_last_synced_at) : "—"}
                </td>
                <td>
                  {pinned ? (
                    <Chip tone="sand" size="sm">
                      manual
                    </Chip>
                  ) : free ? (
                    <Chip tone="sky" size="sm">
                      free-tier
                    </Chip>
                  ) : (
                    <Chip tone="ghost" size="sm">
                      auto
                    </Chip>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
