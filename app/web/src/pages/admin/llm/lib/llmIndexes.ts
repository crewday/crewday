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
  explicitInheritanceByChild: Map<string, string>;
  childrenByParent: Map<string, string[]>;
  assignmentsByCapability: Map<string, LlmAssignment[]>;
  issuesByAssignment: Map<string, string[]>;
  issuesByCapability: Map<string, string[]>;
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
  const explicitInheritanceByChild = new Map(
    graph.inheritance
      .filter((edge) => edge.source === "explicit")
      .map((edge) => [edge.capability, edge.inherits_from]),
  );
  const childrenByParent = new Map<string, string[]>();
  for (const edge of graph.inheritance) {
    const list = childrenByParent.get(edge.inherits_from) ?? [];
    list.push(edge.capability);
    childrenByParent.set(edge.inherits_from, list);
  }
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
  for (const list of childrenByParent.values()) {
    list.sort();
  }
  const issuesByAssignment = new Map<string, string[]>();
  const issuesByCapability = new Map<string, string[]>();
  for (const issue of graph.assignment_issues) {
    const existing = issuesByAssignment.get(issue.assignment_id) ?? [];
    issuesByAssignment.set(issue.assignment_id, [
      ...new Set([...existing, ...issue.missing_capabilities]),
    ]);
    const capExisting = issuesByCapability.get(issue.capability) ?? [];
    issuesByCapability.set(issue.capability, [
      ...new Set([...capExisting, ...issue.missing_capabilities]),
    ]);
  }

  return {
    providersById,
    modelsById,
    pmById,
    providerModelsByModelId,
    capabilitiesByKey,
    inheritanceByChild,
    explicitInheritanceByChild,
    childrenByParent,
    assignmentsByCapability,
    issuesByAssignment,
    issuesByCapability,
  };
}
