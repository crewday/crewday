import { formatMoney } from "@/lib/money";

interface LlmUsageTotalsProps {
  spendUsd: number;
  calls: number;
  label?: string;
}

export default function LlmUsageTotals(props: LlmUsageTotalsProps) {
  const { spendUsd, calls, label = "30d" } = props;
  return (
    <span className="llm-usage-total" aria-label={`${label} ${calls} calls`}>
      <span className="llm-usage-total__label">{label}</span>
      <span className="llm-usage-total__money mono">
        {formatMoney(Math.round(spendUsd * 100), "USD")}
      </span>
      <span className="llm-usage-total__calls mono">{calls.toLocaleString()} calls</span>
    </span>
  );
}
