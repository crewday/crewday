import { formatMoney } from "@/lib/money";

interface LlmUsageTotalsProps {
  spendUsd: number;
  calls: number;
}

export function formatUsageSummary(calls: number, spendUsd: number): string {
  const spend = formatMoney(Math.round(spendUsd * 100), "USD");
  return `${calls.toLocaleString()} calls, ${spend} spend`;
}

export default function LlmUsageTotals(props: LlmUsageTotalsProps) {
  const { spendUsd, calls } = props;
  const summary = formatUsageSummary(calls, spendUsd);
  return (
    <span className="llm-usage-total" aria-label={`Recent usage: ${summary}`}>
      <span className="llm-usage-total__money mono">
        {formatMoney(Math.round(spendUsd * 100), "USD")}
      </span>
      <span className="llm-usage-total__calls mono">{calls.toLocaleString()} calls</span>
    </span>
  );
}
