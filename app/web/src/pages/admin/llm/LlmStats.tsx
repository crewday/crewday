import { StatCard } from "@/components/common";
import { formatMoney } from "@/lib/money";
import type { LlmGraphPayload } from "@/types";

interface LlmStatsProps {
  graph: LlmGraphPayload;
}

export default function LlmStats({ graph }: LlmStatsProps) {
  const unassigned = graph.totals.unassigned_capabilities;
  return (
    <section className="grid grid--stats">
      <StatCard
        label="Spend (30d)"
        value={formatMoney(Math.round(graph.totals.spend_usd_30d * 100), "USD")}
        sub={graph.totals.calls_30d + " calls"}
      />
      <StatCard
        label="Providers"
        value={graph.providers.length}
        sub={graph.providers.filter((p) => p.is_enabled).length + " enabled"}
      />
      <StatCard
        label="Models"
        value={graph.models.length}
        sub={graph.models.filter((m) => m.is_active).length + " active"}
      />
      <StatCard
        label="Capabilities"
        value={graph.totals.capability_count}
        sub={unassigned.length ? unassigned.length + " unassigned" : "all assigned"}
      />
    </section>
  );
}
