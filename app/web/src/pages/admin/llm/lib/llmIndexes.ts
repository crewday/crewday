import type {
  LlmAssignment,
  LlmGraphPayload,
  LlmModel,
  LlmProvider,
  LlmProviderModel,
} from "@/types";

export interface LlmIndexes {
  providersById: Map<string, LlmProvider>;
  modelsById: Map<string, LlmModel>;
  pmById: Map<string, LlmProviderModel>;
  providerModelsByModelId: Map<string, LlmProviderModel[]>;
  capabilitiesByKey: Map<string, LlmGraphPayload["capabilities"][number]>;
  inheritanceByChild: Map<string, string>;
  assignmentsByCapability: Map<string, LlmAssignment[]>;
  issuesByAssignment: Map<string, string[]>;
}

export function buildLlmIndexes(graph: LlmGraphPayload): LlmIndexes {
  const providersById = new Map(graph.providers.map((p) => [p.id, p]));
  const modelsById = new Map(graph.models.map((m) => [m.id, m]));
  const pmById = new Map(graph.provider_models.map((pm) => [pm.id, pm]));
  const providerModelsByModelId = new Map<string, LlmProviderModel[]>();
  for (const pm of graph.provider_models) {
    const list = providerModelsByModelId.get(pm.model_id) ?? [];
    list.push(pm);
    providerModelsByModelId.set(pm.model_id, list);
  }
  const capabilitiesByKey = new Map(graph.capabilities.map((c) => [c.key, c]));
  const inheritanceByChild = new Map(
    graph.inheritance.map((edge) => [edge.capability, edge.inherits_from]),
  );
  const assignmentsByCapability = new Map<string, LlmAssignment[]>();
  for (const cap of graph.capabilities) {
    assignmentsByCapability.set(cap.key, []);
  }
  for (const a of graph.assignments) {
    const list = assignmentsByCapability.get(a.capability) ?? [];
    list.push(a);
    assignmentsByCapability.set(a.capability, list);
  }
  for (const list of assignmentsByCapability.values()) {
    list.sort((x, y) => x.priority - y.priority);
  }
  const issuesByAssignment = new Map(
    graph.assignment_issues.map((i) => [i.assignment_id, i.missing_capabilities]),
  );

  return {
    providersById,
    modelsById,
    pmById,
    providerModelsByModelId,
    capabilitiesByKey,
    inheritanceByChild,
    assignmentsByCapability,
    issuesByAssignment,
  };
}
