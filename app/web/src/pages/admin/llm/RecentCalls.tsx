import { Chip } from "@/components/common";
import { formatMoney } from "@/lib/money";
import type { LLMCall } from "@/types";

const CALL_STATUS_TONE: Record<LLMCall["status"], "moss" | "rust" | "sand"> = {
  ok: "moss",
  error: "rust",
  redacted_block: "sand",
};

function hms(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

interface RecentCallsProps {
  calls: LLMCall[];
}

export default function RecentCalls({ calls }: RecentCallsProps) {
  return (
    <div className="panel">
      <header className="panel__head">
        <h2>Recent calls</h2>
      </header>
      <table className="table">
        <thead>
          <tr>
            <th>When</th>
            <th>Capability</th>
            <th>Model</th>
            <th>Tokens (in / out)</th>
            <th>Cost</th>
            <th>Latency</th>
            <th>Chain</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {calls.map((c, idx) => (
            <tr key={idx}>
              <td className="mono">{hms(c.at)}</td>
              <td>
                <code className="inline-code">{c.capability}</code>
              </td>
              <td className="mono muted">{c.model_id}</td>
              <td className="mono">
                {c.input_tokens} / {c.output_tokens}
              </td>
              <td className="mono">{formatMoney(c.cost_cents, "USD")}</td>
              <td className="mono">{c.latency_ms} ms</td>
              <td className="mono">
                {c.fallback_attempts && c.fallback_attempts > 0
                  ? `fallback #${c.fallback_attempts}`
                  : "primary"}
              </td>
              <td>
                <Chip tone={CALL_STATUS_TONE[c.status]} size="sm">
                  {c.status}
                </Chip>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
