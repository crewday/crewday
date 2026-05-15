import { formatMoney } from "@/lib/money";
import { formatInteger } from "@/lib/numberFormat";

interface LlmUsageTotalsProps {
  spendUsd: number;
  calls: number;
  variant?: "badge" | "muted";
}

export function formatUsageSummary(calls: number, spendUsd: number): string {
  const spend = formatMoney(Math.round(spendUsd * 100), "USD");
  return `${formatInteger(calls)} calls, ${spend} spend`;
}

export default function LlmUsageTotals(props: LlmUsageTotalsProps) {
  const { spendUsd, calls, variant = "badge" } = props;
  const summary = formatUsageSummary(calls, spendUsd);
  return (
    <span
      className={[
        "llm-usage-total",
        variant === "muted" ? "llm-usage-total--muted" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      aria-label={`Recent usage: ${summary}`}
    >
      <span className="llm-usage-total__money mono">
        {formatMoney(Math.round(spendUsd * 100), "USD")}
      </span>
      <span className="llm-usage-total__calls mono">{formatInteger(calls)} calls</span>
    </span>
  );
}
