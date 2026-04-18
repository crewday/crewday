import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading, ProgressBar, StatCard } from "@/components/common";
import type {
  AdminLlmPricingRow,
  AdminLlmProvider,
  LLMCall,
  ModelAssignment,
} from "@/types/api";

interface AssignmentsPayload {
  assignments: ModelAssignment[];
  total_spent: number;
  total_budget: number;
  total_calls: number;
}

const STATUS_TONE: Record<LLMCall["status"], "moss" | "rust" | "sand"> = {
  ok: "moss",
  error: "rust",
  redacted_block: "sand",
};

function hms(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

export default function AdminLlmPage() {
  const assignQ = useQuery({
    queryKey: qk.adminLlmAssignments(),
    queryFn: () => fetchJson<AssignmentsPayload>("/admin/api/v1/llm/assignments"),
  });
  const callsQ = useQuery({
    queryKey: qk.adminLlmCalls(),
    queryFn: () => fetchJson<LLMCall[]>("/admin/api/v1/llm/calls"),
  });
  const providersQ = useQuery({
    queryKey: qk.adminLlmProviders(),
    queryFn: () => fetchJson<AdminLlmProvider[]>("/admin/api/v1/llm/providers"),
  });
  const pricingQ = useQuery({
    queryKey: qk.adminLlmPricing(),
    queryFn: () => fetchJson<AdminLlmPricingRow[]>("/admin/api/v1/llm/pricing"),
  });

  const sub =
    "Deployment-wide LLM config: providers, capability → model assignments, pricing, and spend. Shared by every workspace.";
  const actions = (
    <>
      <button className="btn btn--ghost">Prompt library</button>
      <button className="btn btn--moss">+ Provider</button>
    </>
  );

  if (assignQ.isPending || callsQ.isPending || providersQ.isPending || pricingQ.isPending) {
    return <DeskPage title="LLM & agents" sub={sub} actions={actions}><Loading /></DeskPage>;
  }
  if (!assignQ.data || !callsQ.data || !providersQ.data || !pricingQ.data) {
    return <DeskPage title="LLM & agents" sub={sub} actions={actions}>Failed to load.</DeskPage>;
  }

  const { assignments, total_spent, total_budget, total_calls } = assignQ.data;
  const calls = callsQ.data;
  const providers = providersQ.data;
  const pricing = pricingQ.data;

  return (
    <DeskPage title="LLM & agents" sub={sub} actions={actions}>
      <section className="grid grid--stats">
        <StatCard
          label="24h spend"
          value={formatMoney(Math.round(total_spent * 100), "USD")}
          sub={"of " + formatMoney(Math.round(total_budget * 100), "USD") + " budget"}
        />
        <StatCard
          label="Calls (24h)"
          value={total_calls}
          sub={"across " + assignments.length + " capabilities"}
        />
        <StatCard label="Default model" value="gemma-4-31b-it" sub="via OpenRouter" />
        <StatCard label="Providers" value={providers.length} sub={providers.filter((p) => p.status === "connected").length + " connected"} />
      </section>

      <div className="panel">
        <header className="panel__head"><h2>Providers</h2></header>
        <table className="table">
          <thead>
            <tr>
              <th>Provider</th><th>URL</th><th>API key env</th><th>Status</th><th>Last check</th>
            </tr>
          </thead>
          <tbody>
            {providers.map((p) => (
              <tr key={p.key}>
                <td>
                  {p.label}
                  {p.fallback ? <span className="muted"> · fallback</span> : null}
                </td>
                <td className="mono">{p.url}</td>
                <td className="mono">{p.api_key_env}</td>
                <td>
                  <Chip tone={p.status === "connected" ? "moss" : p.status === "error" ? "rust" : "ghost"} size="sm">
                    {p.status}
                  </Chip>
                </td>
                <td className="mono muted">{p.last_check_at ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head"><h2>Capabilities</h2></header>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Capability</th><th>Model</th><th>24h calls</th><th>Budget</th><th>Spent</th><th></th>
            </tr>
          </thead>
          <tbody>
            {assignments.map((a) => {
              const pct = a.daily_budget_usd > 0
                ? (a.spent_24h_usd / a.daily_budget_usd) * 100
                : 0;
              return (
                <tr key={a.capability}>
                  <td>
                    <code className="inline-code">{a.capability}</code>
                    <div className="table__sub">{a.description}</div>
                  </td>
                  <td className="mono">
                    {a.model_id}
                    <div className="table__sub">{a.provider}</div>
                  </td>
                  <td className="mono">{a.calls_24h}</td>
                  <td className="mono">{formatMoney(Math.round(a.daily_budget_usd * 100), "USD")}</td>
                  <td className="mono">
                    <ProgressBar value={pct} slim />{" "}
                    <span>{formatMoney(Math.round(a.spent_24h_usd * 100), "USD")}</span>
                  </td>
                  <td>
                    {a.enabled ? (
                      <Chip tone="moss" size="sm">on</Chip>
                    ) : (
                      <Chip tone="ghost" size="sm">off</Chip>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head">
          <h2>Pricing table</h2>
          <button className="btn btn--ghost">Reload from llm_pricing.yml</button>
        </header>
        <table className="table">
          <thead>
            <tr>
              <th>Model</th><th>Input / 1k</th><th>Output / 1k</th><th></th>
            </tr>
          </thead>
          <tbody>
            {pricing.map((p) => (
              <tr key={p.model_id}>
                <td className="mono">{p.model_id}</td>
                <td className="mono">${p.input_per_1k_usd.toFixed(3)}</td>
                <td className="mono">${p.output_per_1k_usd.toFixed(3)}</td>
                <td>{p.is_free_tier ? <Chip tone="sky" size="sm">free-tier</Chip> : null}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head"><h2>Recent calls</h2></header>
        <table className="table">
          <thead>
            <tr>
              <th>When</th><th>Capability</th><th>Model</th><th>Tokens (in / out)</th>
              <th>Cost</th><th>Latency</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {calls.map((c, idx) => (
              <tr key={idx}>
                <td className="mono">{hms(c.at)}</td>
                <td><code className="inline-code">{c.capability}</code></td>
                <td className="mono muted">{c.model_id}</td>
                <td className="mono">{c.input_tokens} / {c.output_tokens}</td>
                <td className="mono">{formatMoney(c.cost_cents, "USD")}</td>
                <td className="mono">{c.latency_ms} ms</td>
                <td><Chip tone={STATUS_TONE[c.status]} size="sm">{c.status}</Chip></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
