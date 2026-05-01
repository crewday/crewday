import type { LlmAssignment, LlmGraphPayload } from "@/types";
import type { LlmIndexes } from "./llmIndexes";
import type { Highlighted, Selection } from "../types";

export function emptyHighlighted(): Highlighted {
  return {
    providers: new Set<string>(),
    models: new Set<string>(),
    providerModels: new Set<string>(),
    assignments: new Set<string>(),
    capabilities: new Set<string>(),
  };
}

export function buildHighlighted(
  graph: LlmGraphPayload,
  indexes: LlmIndexes,
  active: Selection,
): Highlighted {
  const providers = new Set<string>();
  const models = new Set<string>();
  const providerModels = new Set<string>();
  const assignments = new Set<string>();
  const capabilities = new Set<string>();
  const reachableAssignmentsByPm = new Map<string, LlmAssignment[]>();
  for (const a of graph.assignments) {
    const bucket = reachableAssignmentsByPm.get(a.provider_model_id) ?? [];
    bucket.push(a);
    reachableAssignmentsByPm.set(a.provider_model_id, bucket);
  }

  if (active.column === "provider") {
    providers.add(active.id);
    for (const pm of graph.provider_models) {
      if (pm.provider_id !== active.id) continue;
      providerModels.add(pm.id);
      models.add(pm.model_id);
      for (const a of reachableAssignmentsByPm.get(pm.id) ?? []) {
        assignments.add(a.id);
        capabilities.add(a.capability);
      }
    }
  } else if (active.column === "model") {
    models.add(active.id);
    for (const pm of graph.provider_models) {
      if (pm.model_id !== active.id) continue;
      providerModels.add(pm.id);
      providers.add(pm.provider_id);
      for (const a of reachableAssignmentsByPm.get(pm.id) ?? []) {
        assignments.add(a.id);
        capabilities.add(a.capability);
      }
    }
  } else if (active.column === "assignment") {
    assignments.add(active.id);
    const a = graph.assignments.find((x) => x.id === active.id);
    if (a) {
      capabilities.add(a.capability);
      const pm = indexes.pmById.get(a.provider_model_id);
      if (pm) {
        providerModels.add(pm.id);
        models.add(pm.model_id);
        providers.add(pm.provider_id);
      }
    }
  } else if (active.column === "capability") {
    capabilities.add(active.id);
    for (const a of indexes.assignmentsByCapability.get(active.id) ?? []) {
      assignments.add(a.id);
      const pm = indexes.pmById.get(a.provider_model_id);
      if (pm) {
        providerModels.add(pm.id);
        models.add(pm.model_id);
        providers.add(pm.provider_id);
      }
    }
  }

  return { providers, models, providerModels, assignments, capabilities };
}
