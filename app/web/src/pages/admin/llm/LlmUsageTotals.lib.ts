import { formatMoney } from "@/lib/money";
import { formatInteger } from "@/lib/numberFormat";

export function formatUsageSummary(calls: number, spendUsd: number): string {
  const spend = formatMoney(Math.round(spendUsd * 100), "USD");
  return `${formatInteger(calls)} calls, ${spend} spend`;
}
