import type {
  LlmAssignment,
  LlmCapabilityEntry,
  LlmGraphPayload,
  LlmModel,
  LlmProvider,
  LlmProviderModel,
} from "@/types";
import type { LlmIndexes } from "./llmIndexes";

export interface LlmAssignmentGroupLayout {
  capability: LlmCapabilityEntry;
  chain: LlmAssignment[];
  inheritedChildren: LlmCapabilityEntry[];
}

export interface LlmGraphLayout {
  providers: LlmProvider[];
  models: LlmModel[];
  providerModelsByModelId: Map<string, LlmProviderModel[]>;
  assignmentGroups: LlmAssignmentGroupLayout[];
}

const LAYOUT_SWEEPS = 4;

export function buildLlmGraphLayout(
  graph: LlmGraphPayload,
  indexes: LlmIndexes,
): LlmGraphLayout {
  const assignmentGroups = buildAssignmentGroups(graph, indexes);
  const fallback = buildFallbackRanks(graph, assignmentGroups);
  const assignedOrder = firstSeenAssignedOrder(assignmentGroups, indexes);
  const tieBreak = {
    provider: mergeRanks(fallback.provider, assignedOrder.provider),
    model: mergeRanks(fallback.model, assignedOrder.model),
    providerModel: mergeRanks(fallback.providerModel, assignedOrder.providerModel),
  };

  let providerRanks = new Map(tieBreak.provider);
  let providerModelRanks = new Map(tieBreak.providerModel);
  let assignmentGroupRanks = new Map(fallback.assignmentGroup);

  for (let i = 0; i < LAYOUT_SWEEPS; i += 1) {
    providerRanks = rankProviders(
      graph.providers,
      indexes,
      providerModelRanks,
      fallback,
      tieBreak.provider,
    );
    assignmentGroupRanks = rankAssignmentGroups(
      assignmentGroups,
      indexes,
      providerModelRanks,
      fallback,
    );
    providerModelRanks = rankProviderModels(
      graph.provider_models,
      indexes,
      providerRanks,
      assignmentGroupRanks,
      fallback,
      tieBreak.providerModel,
    );
  }

  const modelRanks = rankModels(
    graph.models,
    indexes,
    providerModelRanks,
    fallback,
    tieBreak.model,
  );
  const orderedProviders = stableByRank(
    graph.providers,
    providerRanks,
    tieBreak.provider,
  );
  const orderedModels = stableByRank(graph.models, modelRanks, tieBreak.model);
  const providerModelsByModelId = new Map<string, LlmProviderModel[]>();
  for (const model of orderedModels) {
    const rows = indexes.providerModelsByModelId.get(model.id) ?? [];
    providerModelsByModelId.set(
      model.id,
      stableByRank(rows, providerModelRanks, tieBreak.providerModel),
    );
  }

  return {
    providers: orderedProviders,
    models: orderedModels,
    providerModelsByModelId,
    assignmentGroups: stableByRank(
      assignmentGroups,
      assignmentGroupRanks,
      fallback.assignmentGroup,
      (group) => group.capability.key,
    ),
  };
}

function buildAssignmentGroups(
  graph: LlmGraphPayload,
  indexes: LlmIndexes,
): LlmAssignmentGroupLayout[] {
  return graph.capabilities
    .filter((capability) => {
      const hasChain =
        (indexes.assignmentsByCapability.get(capability.key) ?? []).length > 0;
      return hasChain || !indexes.inheritanceByChild.has(capability.key);
    })
    .map((capability) => ({
      capability,
      chain: indexes.assignmentsByCapability.get(capability.key) ?? [],
      inheritedChildren:
        indexes.childrenByParent
          .get(capability.key)
          ?.map((key) => indexes.capabilitiesByKey.get(key))
          .filter((child): child is LlmCapabilityEntry => Boolean(child)) ?? [],
    }));
}

interface FallbackRanks {
  provider: Map<string, number>;
  model: Map<string, number>;
  providerModel: Map<string, number>;
  assignmentGroup: Map<string, number>;
}

function buildFallbackRanks(
  graph: LlmGraphPayload,
  assignmentGroups: LlmAssignmentGroupLayout[],
): FallbackRanks {
  return {
    provider: indexRanks(graph.providers),
    model: indexRanks(graph.models),
    providerModel: indexRanks(graph.provider_models),
    assignmentGroup: indexRanks(assignmentGroups, (group) => group.capability.key),
  };
}

function indexRanks<T>(
  items: T[],
  idFor: (item: T) => string = itemId,
): Map<string, number> {
  return new Map(items.map((item, index) => [idFor(item), index]));
}

function itemId<T>(item: T): string {
  if (
    typeof item === "object" &&
    item !== null &&
    "id" in item &&
    typeof item.id === "string"
  ) {
    return item.id;
  }
  throw new Error("LLM graph layout item is missing an id");
}

interface AssignedOrder {
  provider: Map<string, number>;
  model: Map<string, number>;
  providerModel: Map<string, number>;
}

function firstSeenAssignedOrder(
  assignmentGroups: LlmAssignmentGroupLayout[],
  indexes: LlmIndexes,
): AssignedOrder {
  const provider = new Map<string, number>();
  const model = new Map<string, number>();
  const providerModel = new Map<string, number>();
  for (const group of assignmentGroups) {
    for (const assignment of group.chain) {
      const pm = indexes.pmById.get(assignment.provider_model_id);
      if (!pm) continue;
      if (!provider.has(pm.provider_id)) provider.set(pm.provider_id, provider.size);
      if (!model.has(pm.model_id)) model.set(pm.model_id, model.size);
      if (!providerModel.has(pm.id)) providerModel.set(pm.id, providerModel.size);
    }
  }
  return { provider, model, providerModel };
}

function mergeRanks(
  fallback: Map<string, number>,
  preferred: Map<string, number>,
): Map<string, number> {
  const ranks = new Map<string, number>();
  const connectedCount = preferred.size;
  for (const [id, rank] of fallback) {
    ranks.set(id, preferred.get(id) ?? connectedCount + rank);
  }
  return ranks;
}

function rankProviders(
  providers: LlmProvider[],
  indexes: LlmIndexes,
  providerModelRanks: Map<string, number>,
  fallback: FallbackRanks,
  tieBreak: Map<string, number>,
): Map<string, number> {
  const ranks = new Map<string, number>();
  for (const provider of providers) {
    const neighborRanks = indexes.providerModelsByProviderId
      .get(provider.id)
      ?.map((pm) => providerModelRanks.get(pm.id))
      .filter(isNumber) ?? [];
    ranks.set(
      provider.id,
      neighborRanks.length
        ? median(neighborRanks)
        : disconnectedRank(fallback.provider, provider.id),
    );
  }
  return normalizeRanks(providers, ranks, tieBreak);
}

function rankModels(
  models: LlmModel[],
  indexes: LlmIndexes,
  providerModelRanks: Map<string, number>,
  fallback: FallbackRanks,
  tieBreak: Map<string, number>,
): Map<string, number> {
  const ranks = new Map<string, number>();
  for (const model of models) {
    const neighborRanks = indexes.providerModelsByModelId
      .get(model.id)
      ?.map((pm) => providerModelRanks.get(pm.id))
      .filter(isNumber) ?? [];
    ranks.set(
      model.id,
      neighborRanks.length
        ? median(neighborRanks)
        : disconnectedRank(fallback.model, model.id),
    );
  }
  return normalizeRanks(models, ranks, tieBreak);
}

function rankAssignmentGroups(
  groups: LlmAssignmentGroupLayout[],
  indexes: LlmIndexes,
  providerModelRanks: Map<string, number>,
  fallback: FallbackRanks,
): Map<string, number> {
  const ranks = new Map<string, number>();
  for (const group of groups) {
    const neighborRanks = group.chain
      .map((assignment) => indexes.pmById.get(assignment.provider_model_id)?.id)
      .map((providerModelId) =>
        providerModelId ? providerModelRanks.get(providerModelId) : undefined,
      )
      .filter(isNumber);
    ranks.set(
      group.capability.key,
      neighborRanks.length
        ? median(neighborRanks)
        : disconnectedRank(fallback.assignmentGroup, group.capability.key),
    );
  }
  return normalizeRanks(
    groups,
    ranks,
    fallback.assignmentGroup,
    (group) => group.capability.key,
  );
}

function rankProviderModels(
  providerModels: LlmProviderModel[],
  indexes: LlmIndexes,
  providerRanks: Map<string, number>,
  assignmentGroupRanks: Map<string, number>,
  fallback: FallbackRanks,
  tieBreak: Map<string, number>,
): Map<string, number> {
  const assignmentGroupsByProviderModel = new Map<string, number[]>();
  for (const assignments of indexes.assignmentsByCapability.values()) {
    for (const assignment of assignments) {
      const groupRank = assignmentGroupRanks.get(assignment.capability);
      if (groupRank === undefined) continue;
      const ranks =
        assignmentGroupsByProviderModel.get(assignment.provider_model_id) ?? [];
      ranks.push(groupRank);
      assignmentGroupsByProviderModel.set(assignment.provider_model_id, ranks);
    }
  }

  const ranks = new Map<string, number>();
  for (const providerModel of providerModels) {
    const neighborRanks = [
      providerRanks.get(providerModel.provider_id),
      ...(assignmentGroupsByProviderModel.get(providerModel.id) ?? []),
    ].filter(isNumber);
    ranks.set(
      providerModel.id,
      neighborRanks.length
        ? median(neighborRanks)
        : disconnectedRank(fallback.providerModel, providerModel.id),
    );
  }
  return normalizeRanks(providerModels, ranks, tieBreak);
}

function stableByRank<T>(
  items: T[],
  ranks: Map<string, number>,
  fallback: Map<string, number>,
  idFor: (item: T) => string = itemId,
): T[] {
  return [...items].sort((a, b) => {
    const aId = idFor(a);
    const bId = idFor(b);
    const rankDiff = (ranks.get(aId) ?? 0) - (ranks.get(bId) ?? 0);
    if (rankDiff !== 0) return rankDiff;
    return (fallback.get(aId) ?? 0) - (fallback.get(bId) ?? 0);
  });
}

function normalizeRanks<T>(
  items: T[],
  ranks: Map<string, number>,
  fallback: Map<string, number>,
  idFor: (item: T) => string = itemId,
): Map<string, number> {
  return new Map(
    stableByRank(items, ranks, fallback, idFor).map((item, index) => [
      idFor(item),
      index,
    ]),
  );
}

function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const midpoint = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 1) return sorted[midpoint] ?? 0;
  return ((sorted[midpoint - 1] ?? 0) + (sorted[midpoint] ?? 0)) / 2;
}

function disconnectedRank(fallback: Map<string, number>, id: string): number {
  return fallback.size + (fallback.get(id) ?? 0);
}

function isNumber(value: number | undefined): value is number {
  return typeof value === "number";
}
