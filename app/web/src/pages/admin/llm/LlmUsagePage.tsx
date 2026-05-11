import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import DeskPage from "@/components/DeskPage";
import { Loading } from "@/components/common";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { LLMCall, LlmGraphPayload, LlmSyncPricingResult } from "@/types";
import LlmRouteTabs from "./LlmRouteTabs";
import LlmStats from "./LlmStats";
import ProviderModelPricing from "./ProviderModelPricing";
import RecentCalls from "./RecentCalls";
import { buildLlmIndexes } from "./lib/llmIndexes";
import { useAdminLlmPromptDrawer } from "./useAdminLlmPromptDrawer";

const title = "LLM usage";
const sub =
  "Deployment-wide LLM spend, pricing sync state, and recent call telemetry across workspaces.";

export default function AdminLlmUsagePage() {
  const graphQ = useQuery({
    queryKey: qk.adminLlmGraph(),
    queryFn: () => fetchJson<LlmGraphPayload>("/admin/api/v1/llm/graph"),
  });
  const callsQ = useQuery({
    queryKey: qk.adminLlmCalls(),
    queryFn: () => fetchJson<LLMCall[]>("/admin/api/v1/llm/calls"),
  });
  const { promptsQ, promptOverflow, promptDrawer } = useAdminLlmPromptDrawer();
  const qc = useQueryClient();
  const syncMut = useMutation({
    mutationFn: () =>
      fetchJson<LlmSyncPricingResult>("/admin/api/v1/llm/sync-pricing", {
        method: "POST",
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.adminLlmGraph() });
      void qc.invalidateQueries({ queryKey: qk.adminLlmCalls() });
    },
  });

  const graph = graphQ.data;
  const indexes = useMemo(() => (graph ? buildLlmIndexes(graph) : null), [graph]);

  if (graphQ.isPending || callsQ.isPending || promptsQ.isPending) {
    return (
      <DeskPage title={title} sub={sub} overflow={[promptOverflow]}>
        <Loading />
      </DeskPage>
    );
  }
  if (!graph || !callsQ.data || !promptsQ.data || !indexes) {
    return (
      <DeskPage title={title} sub={sub} overflow={[promptOverflow]}>
        Failed to load.
      </DeskPage>
    );
  }

  return (
    <DeskPage title={title} sub={sub} overflow={[promptOverflow]}>
      <LlmRouteTabs activeKey="usage" />
      <LlmStats graph={graph} />
      <ProviderModelPricing
        graph={graph}
        indexes={indexes}
        syncResult={syncMut.data}
        isSyncing={syncMut.isPending}
        onSync={() => {
          if (!syncMut.isPending) syncMut.mutate();
        }}
      />
      <RecentCalls calls={callsQ.data} />
      {promptDrawer}
    </DeskPage>
  );
}
