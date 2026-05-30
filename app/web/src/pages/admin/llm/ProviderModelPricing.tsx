import DateTime from "@/components/DateTime";
import { Chip } from "@/components/common";
import { formatDecimal, formatInteger } from "@/lib/numberFormat";
import type { LlmGraphPayload, LlmSyncPricingResult } from "@/types";
import type { LlmIndexes } from "./lib/llmIndexes";

interface ProviderModelPricingProps {
  graph: LlmGraphPayload;
  indexes: LlmIndexes;
  syncResult?: LlmSyncPricingResult;
  isSyncing: boolean;
  onSync: () => void;
}

function formatUsdPerMillion(value: number | null): string {
  return `$${formatDecimal(value ?? 0, {
    minimumFractionDigits: 3,
    maximumFractionDigits: 3,
  })}`;
}

function formatUsdAmount(value: number | null): string {
  return `$${formatDecimal(value ?? 0, {
    minimumFractionDigits: 3,
    maximumFractionDigits: 6,
  })}`;
}

function hasOptionalPricingValue(before: number | null, after: number | null): boolean {
  return before !== null || after !== null;
}

export default function ProviderModelPricing({
  graph,
  indexes,
  syncResult,
  isSyncing,
  onSync,
}: ProviderModelPricingProps) {
  const pinnedCount = graph.provider_models.filter(
    (pm) => pm.price_source_override === "none",
  ).length;
  const lastSynced = graph.provider_models
    .map((pm) => pm.price_last_synced_at)
    .filter((value): value is string => value !== null)
    .sort()
    .at(-1);

  return (
    <div className="panel">
      <header className="panel__head llm-pricing-panel__head">
        <div>
          <h2>Provider-model pricing</h2>
          <span className="muted">From OpenRouter weekly; pinned rows skip the sync.</span>
        </div>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={onSync}
          disabled={isSyncing}
        >
          {isSyncing ? "Syncing…" : "Sync pricing"}
        </button>
      </header>
      <div className="llm-pricing-sync">
        <span>
          Last synced{" "}
          <DateTime value={lastSynced ?? null} showTime className="mono" empty="," />
        </span>
        <span>
          {formatInteger(pinnedCount)} manual-pinned row{pinnedCount === 1 ? "" : "s"}
        </span>
        {syncResult ? (
          <span>
            Last result: {formatInteger(syncResult.updated)} updated,{" "}
            {formatInteger(syncResult.skipped)} skipped,{" "}
            {formatInteger(syncResult.errors)} errors
          </span>
        ) : null}
      </div>
      {syncResult?.deltas.length ? (
        <div className="llm-pricing-deltas" aria-label="Pricing sync deltas">
          {syncResult.deltas.slice(0, 6).map((delta) => (
            <span key={`${delta.provider_model_id}-${delta.status}`} className="llm-pricing-delta">
              <code className="inline-code">{delta.api_model_id}</code>{" "}
              {delta.status}: {formatUsdPerMillion(delta.input_before)} -&gt;{" "}
              {formatUsdPerMillion(delta.input_after)} input,{" "}
              {formatUsdPerMillion(delta.output_before)} -&gt;{" "}
              {formatUsdPerMillion(delta.output_after)} output
              {hasOptionalPricingValue(delta.fixed_before, delta.fixed_after)
                ? `, ${formatUsdAmount(delta.fixed_before)} -> ${formatUsdAmount(delta.fixed_after)} fixed`
                : ""}
              {hasOptionalPricingValue(delta.audio_before, delta.audio_after)
                ? `, ${formatUsdAmount(delta.audio_before)} -> ${formatUsdAmount(delta.audio_after)} audio/hr`
                : ""}
            </span>
          ))}
        </div>
      ) : null}
      <table className="table">
        <thead>
          <tr>
            <th>Provider × Model</th>
            <th>API model id</th>
            <th>Input / 1M</th>
            <th>Output / 1M</th>
            <th>Fixed</th>
            <th>Audio / hr</th>
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
              (pm.input_cost_per_million ?? 0) === 0 &&
              (pm.output_cost_per_million ?? 0) === 0 &&
              (pm.audio_cost_per_hour_usd ?? 0) === 0;
            return (
              <tr key={pm.id}>
                <td>
                  {provider?.name ?? "?"}
                  <span className="muted"> × </span>
                  {model?.display_name ?? "?"}
                </td>
                <td className="mono">{pm.api_model_id}</td>
                <td className="mono">{formatUsdPerMillion(pm.input_cost_per_million)}</td>
                <td className="mono">{formatUsdPerMillion(pm.output_cost_per_million)}</td>
                <td className="mono">{formatUsdAmount(pm.fixed_cost_per_call_usd)}</td>
                <td className="mono">{formatUsdAmount(pm.audio_cost_per_hour_usd)}</td>
                <td>
                  <DateTime value={pm.price_last_synced_at} showTime className="mono muted" empty="," />
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
