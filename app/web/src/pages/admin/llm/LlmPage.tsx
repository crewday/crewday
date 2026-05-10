import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import DeskPage from "@/components/DeskPage";
import { Loading } from "@/components/common";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useCloseOnEscape } from "@/lib/useCloseOnEscape";
import type {
  LLMCall,
  LlmAssignment,
  LlmGraphPayload,
  LlmPromptTemplate,
  LlmSyncPricingResult,
} from "@/types";
import AssignmentColumn from "./AssignmentColumn";
import LlmAlerts from "./LlmAlerts";
import LlmStats from "./LlmStats";
import ModelColumn from "./ModelColumn";
import PromptLibraryDrawer from "./PromptLibraryDrawer";
import ProviderColumn from "./ProviderColumn";
import ProviderModelPricing from "./ProviderModelPricing";
import RecentCalls from "./RecentCalls";
import { buildHighlighted, emptyHighlighted } from "./lib/highlight";
import { buildLlmIndexes } from "./lib/llmIndexes";
import { useLlmGraphEdges } from "./useLlmGraphEdges";
import type { Column, EdgeLayout, Selection } from "./types";

const sub =
  "Deployment-wide LLM graph/config: providers, models, provider-model pricing, capability assignment chains, and the prompt library. Shared by every workspace.";
const title = "LLM graph";

export default function AdminLlmPage() {
  // code-health: ignore[nloc] LLM graph route already delegates columns, alerts, stats, pricing, calls, and drawers.
  const graphQ = useQuery({
    queryKey: qk.adminLlmGraph(),
    queryFn: () => fetchJson<LlmGraphPayload>("/admin/api/v1/llm/graph"),
  });
  const callsQ = useQuery({
    queryKey: qk.adminLlmCalls(),
    queryFn: () => fetchJson<LLMCall[]>("/admin/api/v1/llm/calls"),
  });
  const promptsQ = useQuery({
    queryKey: qk.adminLlmPrompts(),
    queryFn: () => fetchJson<LlmPromptTemplate[]>("/admin/api/v1/llm/prompts"),
  });

  const [selection, setSelection] = useState<Selection | null>(null);
  const [hover, setHover] = useState<Selection | null>(null);
  const [promptsOpen, setPromptsOpen] = useState(false);
  useCloseOnEscape(() => setPromptsOpen(false), promptsOpen);

  const qc = useQueryClient();
  const invalidateAdminLlm = () => {
    void qc.invalidateQueries({ queryKey: qk.adminLlmGraph() });
    void qc.invalidateQueries({ queryKey: qk.adminLlmCalls() });
    void qc.invalidateQueries({ queryKey: qk.adminLlmPrompts() });
  };
  const syncMut = useMutation({
    mutationFn: () =>
      fetchJson<LlmSyncPricingResult>("/admin/api/v1/llm/sync-pricing", {
        method: "POST",
      }),
    onSuccess: invalidateAdminLlm,
  });
  const assignmentMut = useMutation({
    mutationFn: ({ assignment, providerModelId }: {
      assignment: LlmAssignment;
      providerModelId: string;
    }) =>
      fetchJson<LlmAssignment>(`/admin/api/v1/llm/assignments/${assignment.id}`, {
        method: "PUT",
        body: { provider_model_id: providerModelId },
      }),
    onSuccess: invalidateAdminLlm,
  });
  const createInheritanceMut = useMutation({
    mutationFn: ({
      capability,
      inheritsFrom,
    }: {
      capability: string;
      inheritsFrom: string;
    }) =>
      fetchJson("/admin/api/v1/llm/inheritance", {
        method: "POST",
        body: { capability, inherits_from: inheritsFrom },
      }),
    onSuccess: invalidateAdminLlm,
  });
  const updateInheritanceMut = useMutation({
    mutationFn: ({
      capability,
      inheritsFrom,
    }: {
      capability: string;
      inheritsFrom: string;
    }) =>
      fetchJson(`/admin/api/v1/llm/inheritance/${encodeURIComponent(capability)}`, {
        method: "PUT",
        body: { inherits_from: inheritsFrom },
      }),
    onSuccess: invalidateAdminLlm,
  });
  const deleteInheritanceMut = useMutation({
    mutationFn: (capability: string) =>
      fetchJson(`/admin/api/v1/llm/inheritance/${encodeURIComponent(capability)}`, {
        method: "DELETE",
      }),
    onSuccess: invalidateAdminLlm,
  });

  const graph = graphQ.data;
  const indexes = useMemo(() => (graph ? buildLlmIndexes(graph) : null), [graph]);
  const active = hover ?? selection;

  const highlighted = useMemo(() => {
    if (!graph || !indexes || !active) return emptyHighlighted();
    return buildHighlighted(graph, indexes, active);
  }, [graph, indexes, active]);

  const hasActive = active !== null;
  const { graphRef, providerRefs, modelRefs, rungRefs, edges, canvas, setRef } =
    useLlmGraphEdges(graph, indexes, active);

  const edgeIsHighlighted = (e: EdgeLayout): boolean => {
    if (!active) return false;
    if (active.column === "provider") return e.providerId === active.id;
    if (active.column === "model") return e.modelId === active.id;
    if (active.column === "providerModel") return e.providerModelId === active.id;
    if (active.column === "assignment") {
      return (
        e.assignmentId === active.id ||
        e.providerModelId ===
          graph?.assignments.find((x) => x.id === active.id)?.provider_model_id
      );
    }
    if (active.column === "capability") {
      if (e.kind === "assign") return e.capability === active.id;
      const chain = indexes?.assignmentsByCapability.get(active.id) ?? [];
      return chain.some((a) => a.provider_model_id === e.providerModelId);
    }
    return false;
  };

  const nodeClass = (col: Column, id: string) => {
    const set = {
      provider: highlighted.providers,
      model: highlighted.models,
      providerModel: highlighted.providerModels,
      assignment: highlighted.assignments,
      capability: highlighted.capabilities,
    }[col];
    const isOn = set.has(id);
    const isActive = active?.column === col && active.id === id;
    const dim = hasActive && !isOn;
    return [
      "llm-graph-node",
      `llm-graph-node--${col}`,
      isActive ? "is-active" : "",
      isOn && !isActive ? "is-linked" : "",
      dim ? "is-dim" : "",
    ]
      .filter(Boolean)
      .join(" ");
  };

  const writeAssignmentToModel = (modelId: string): boolean => {
    // code-health: ignore[ccn] Assignment move guard deliberately lists each graph invariant before mutating.
    if (!graph || !indexes || selection?.column !== "assignment") return false;
    const assignment = graph.assignments.find((a) => a.id === selection.id);
    if (!assignment) return false;
    if (assignment.is_deployment_default) return false;
    const model = indexes.modelsById.get(modelId);
    if (!model) return false;
    const modelCapabilities = new Set(model.capabilities);
    if (!assignment.required_capabilities.every((cap) => modelCapabilities.has(cap))) {
      return false;
    }
    const currentPm = indexes.pmById.get(assignment.provider_model_id);
    const candidates = indexes.providerModelsByModelId.get(modelId) ?? [];
    const nextPm =
      candidates.find((pm) => pm.provider_id === currentPm?.provider_id) ??
      candidates.find((pm) => pm.is_enabled) ??
      candidates[0];
    if (!nextPm || nextPm.id === assignment.provider_model_id) return false;
    assignmentMut.mutate({ assignment, providerModelId: nextPm.id });
    setSelection({ column: "model", id: modelId });
    return true;
  };

  const actions = <button className="btn btn--moss">+ Provider</button>;
  const overflow = [
    {
      label: "Prompts",
      onSelect: () => setPromptsOpen(true),
    },
    {
      label: syncMut.isPending ? "Syncing…" : "Sync pricing",
      onSelect: () => {
        if (!syncMut.isPending) syncMut.mutate();
      },
    },
  ];

  if (graphQ.isPending || callsQ.isPending || promptsQ.isPending) {
    return (
      <DeskPage title={title} sub={sub} actions={actions} overflow={overflow}>
        <Loading />
      </DeskPage>
    );
  }
  if (!graph || !callsQ.data || !promptsQ.data || !indexes) {
    return (
      <DeskPage title={title} sub={sub} actions={actions} overflow={overflow}>
        Failed to load.
      </DeskPage>
    );
  }

  const calls = callsQ.data;
  const prompts = promptsQ.data;
  const syncResult = syncMut.data;

  return (
    <DeskPage title={title} sub={sub} actions={actions} overflow={overflow}>
      <LlmStats graph={graph} />
      <LlmAlerts graph={graph} syncResult={syncResult} />

      <div className="llm-graph" ref={graphRef}>
        <svg
          className="llm-graph__edges"
          width={canvas.w}
          height={canvas.h}
          aria-hidden="true"
        >
          {edges.map((e) => {
            const highlighted = edgeIsHighlighted(e);
            const dim = hasActive && !highlighted;
            const cls = [
              "llm-graph__edge",
              `llm-graph__edge--${e.kind}`,
              highlighted ? "is-linked" : "",
              dim ? "is-dim" : "",
              e.invalid ? "is-error" : "",
            ]
              .filter(Boolean)
              .join(" ");
            return <path key={e.id} className={cls} d={e.d} />;
          })}
        </svg>

        <div className="llm-graph__col-header llm-graph__col-header--providers">
          <span className="llm-graph__col-title">Providers</span>
          <span className="llm-graph__col-count">{graph.providers.length}</span>
        </div>
        <div className="llm-graph__col-header llm-graph__col-header--models">
          <span className="llm-graph__col-title">Models</span>
          <span className="llm-graph__col-count">{graph.models.length}</span>
        </div>
        <div className="llm-graph__col-header llm-graph__col-header--assignments">
          <span className="llm-graph__col-title">Assignments</span>
          <span className="llm-graph__col-count">{graph.totals.capability_count}</span>
        </div>

        <ProviderColumn
          providers={graph.providers}
          selection={selection}
          setHover={setHover}
          setSelection={setSelection}
          nodeClass={nodeClass}
          setProviderRef={setRef(providerRefs)}
        />
        <ModelColumn
          models={graph.models}
          selection={selection}
          setHover={setHover}
          setSelection={setSelection}
          nodeClass={nodeClass}
          setModelRef={setRef(modelRefs)}
          onModelClick={writeAssignmentToModel}
          indexes={indexes}
        />
        <AssignmentColumn
          capabilities={graph.capabilities}
          indexes={indexes}
          selection={selection}
          active={active}
          setHover={setHover}
          setSelection={setSelection}
          nodeClass={nodeClass}
          hasActive={hasActive}
          highlighted={highlighted}
          setRungRef={setRef(rungRefs)}
          onChangeInheritance={(capability, inheritsFrom, isExplicit) => {
            if (isExplicit) {
              updateInheritanceMut.mutate({ capability, inheritsFrom });
            } else {
              createInheritanceMut.mutate({ capability, inheritsFrom });
            }
          }}
          onRemoveInheritance={(capability) => deleteInheritanceMut.mutate(capability)}
        />
      </div>

      <ProviderModelPricing graph={graph} indexes={indexes} />
      <RecentCalls calls={calls} />

      {promptsOpen ? (
        <PromptLibraryDrawer prompts={prompts} onClose={() => setPromptsOpen(false)} />
      ) : null}
    </DeskPage>
  );
}
