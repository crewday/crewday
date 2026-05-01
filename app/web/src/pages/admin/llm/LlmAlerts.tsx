import type { LlmGraphPayload, LlmSyncPricingResult } from "@/types";

interface LlmAlertsProps {
  graph: LlmGraphPayload;
  syncResult: LlmSyncPricingResult | undefined;
}

export default function LlmAlerts({ graph, syncResult }: LlmAlertsProps) {
  const unassigned = graph.totals.unassigned_capabilities;
  return (
    <>
      {unassigned.length > 0 ? (
        <div className="llm-graph-alert llm-graph-alert--warn">
          <strong>Unassigned capabilities:</strong>{" "}
          {unassigned.map((k) => (
            <code key={k} className="inline-code">
              {k}
            </code>
          ))}
          <div className="llm-graph-alert__sub">
            Assign a provider-model from column 3, or add a capability-inheritance
            edge so this capability falls back to a parent's chain.
          </div>
        </div>
      ) : null}

      {graph.assignment_issues.length > 0 ? (
        <div className="llm-graph-alert llm-graph-alert--error">
          <strong>Missing required capabilities:</strong>{" "}
          {graph.assignment_issues.length} assignment
          {graph.assignment_issues.length === 1 ? "" : "s"} point at a model that
          lacks one of the tags the capability needs. Hover the red rows in the
          Assignments column for details.
        </div>
      ) : null}

      {syncResult ? (
        <div className="llm-graph-alert llm-graph-alert--info">
          <strong>Pricing sync:</strong> {syncResult.updated} updated,{" "}
          {syncResult.skipped} unchanged, {syncResult.errors} errors
          <span className="muted"> — started at {syncResult.started_at}</span>
        </div>
      ) : null}
    </>
  );
}
