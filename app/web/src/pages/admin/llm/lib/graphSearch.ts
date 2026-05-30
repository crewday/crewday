import { formatContextWindow } from "@/lib/numberFormat";
import type { LlmGraphPayload } from "@/types";
import { capabilityTagLabel } from "../CapabilityTagChip.lib";
import { formatUsageSummary } from "../LlmUsageTotals.lib";
import { thinkingLevelLabel } from "./llmThinking";
import type { LlmIndexes } from "./llmIndexes";
import type { Highlighted, Selection } from "../types";

export interface LlmGraphSearchResult {
  graph: LlmGraphPayload;
  directMatches: Highlighted;
  hasQuery: boolean;
  hasMatches: boolean;
}

export function filterLlmGraphBySearch(
  graph: LlmGraphPayload,
  indexes: LlmIndexes,
  query: string,
): LlmGraphSearchResult {
  const normalizedQuery = normalizeSearch(query);
  const directMatches = emptySearchMatches();
  if (!normalizedQuery) {
    return { graph, directMatches, hasQuery: false, hasMatches: true };
  }

  markDirectMatches(graph, indexes, normalizedQuery, directMatches);
  const visible = expandConnectedSearchMatches(graph, indexes, directMatches);
  const hasMatches =
    visible.providers.size > 0 ||
    visible.models.size > 0 ||
    visible.providerModels.size > 0 ||
    visible.assignments.size > 0 ||
    visible.capabilities.size > 0;

  return {
    graph: filterGraph(graph, visible),
    directMatches,
    hasQuery: true,
    hasMatches,
  };
}

export function selectionIsVisible(
  selection: Selection | null,
  visible: Highlighted,
): boolean {
  if (!selection) return false;
  if (selection.column === "provider") return visible.providers.has(selection.id);
  if (selection.column === "model") return visible.models.has(selection.id);
  if (selection.column === "providerModel") {
    return visible.providerModels.has(selection.id);
  }
  if (selection.column === "assignment") return visible.assignments.has(selection.id);
  if (selection.column === "capability") return visible.capabilities.has(selection.id);
  return false;
}

function emptySearchMatches(): Highlighted {
  return {
    providers: new Set<string>(),
    models: new Set<string>(),
    providerModels: new Set<string>(),
    assignments: new Set<string>(),
    capabilities: new Set<string>(),
  };
}

function markDirectMatches(
  graph: LlmGraphPayload,
  indexes: LlmIndexes,
  normalizedQuery: string,
  directMatches: Highlighted,
): void {
  for (const provider of graph.providers) {
    const host = endpointHost(provider.endpoint);
    if (
      textMatches(
        normalizedQuery,
        provider.id,
        provider.name,
        provider.provider_type,
        provider.endpoint,
        host,
        provider.api_key_status === "missing"
          ? "no key"
          : provider.api_key_status === "rotating"
            ? "rotating"
            : "key set",
        formatUsageSummary(provider.calls_30d, provider.spend_usd_30d),
      )
    ) {
      directMatches.providers.add(provider.id);
    }
  }

  for (const model of graph.models) {
    if (
      textMatches(
        normalizedQuery,
        model.id,
        model.display_name,
        model.canonical_name,
        ...model.capabilities,
        ...model.capabilities.map(capabilityTagLabel),
        "Thinking " + thinkingLevelLabel(model.thinking_level),
        model.context_window ? formatContextWindow(model.context_window) : "",
        formatUsageSummary(model.calls_30d, model.spend_usd_30d),
      )
    ) {
      directMatches.models.add(model.id);
    }
  }

  for (const providerModel of graph.provider_models) {
    const provider = indexes.providersById.get(providerModel.provider_id);
    const model = indexes.modelsById.get(providerModel.model_id);
    if (
      textMatches(
        normalizedQuery,
        providerModel.id,
        providerModel.api_model_id,
        provider?.name,
        model?.display_name,
        model?.canonical_name,
        providerModel.price_source_override === "none" ? "manual" : "",
        providerModel.price_source_override,
        providerModel.price_source_model_id_override,
        providerModel.supports_system_prompt ? "system prompt" : "",
        providerModel.supports_temperature ? "temperature" : "",
        thinkingLevelText(providerModel.effective_thinking_strategy),
        formatUsageSummary(providerModel.calls_30d, providerModel.spend_usd_30d),
      )
    ) {
      directMatches.providerModels.add(providerModel.id);
    }
  }

  for (const capability of graph.capabilities) {
    const inheritsFrom = indexes.inheritanceByChild.get(capability.key);
    const hasExplicitInheritance = indexes.explicitInheritanceByChild.has(
      capability.key,
    );
    if (
      textMatches(
        normalizedQuery,
        capability.key,
        capability.description,
        ...capability.required_capabilities,
        inheritsFrom
          ? `${hasExplicitInheritance ? "explicitly inherits" : "defaults"} to ${inheritsFrom}`
          : "",
        formatUsageSummary(capability.calls_30d, capability.spend_usd_30d),
        formatUsageSummary(
          capability.direct_calls_30d,
          capability.direct_spend_usd_30d,
        ),
        formatUsageSummary(
          capability.inherited_calls_30d,
          capability.inherited_spend_usd_30d,
        ),
      )
    ) {
      directMatches.capabilities.add(capability.key);
    }
  }

  for (const assignment of graph.assignments) {
    if (
      textMatches(
        normalizedQuery,
        assignment.id,
        assignment.capability,
        assignment.description,
        `rung ${assignment.priority}`,
        `priority ${assignment.priority}`,
        ...assignment.required_capabilities,
        "Thinking " + thinkingLevelLabel(assignment.effective_thinking_level),
        assignment.thinking_level_override
          ? "Thinking " + thinkingLevelLabel(assignment.thinking_level_override)
          : "",
        thinkingLevelText(assignment.effective_thinking_strategy),
        formatUsageSummary(assignment.calls_30d, assignment.spend_usd_30d),
        formatUsageSummary(
          assignment.direct_calls_30d,
          assignment.direct_spend_usd_30d,
        ),
        formatUsageSummary(
          assignment.inherited_calls_30d,
          assignment.inherited_spend_usd_30d,
        ),
      )
    ) {
      directMatches.assignments.add(assignment.id);
    }
  }
}

function expandConnectedSearchMatches(
  graph: LlmGraphPayload,
  indexes: LlmIndexes,
  directMatches: Highlighted,
): Highlighted {
  const visible = emptySearchMatches();
  const queue: { kind: keyof Highlighted; id: string }[] = [];
  enqueueMatches(queue, directMatches);

  for (let cursor = 0; cursor < queue.length; cursor += 1) {
    const current = queue[cursor];
    if (!current || alreadyVisible(visible, current.kind, current.id)) continue;
    addVisible(visible, current.kind, current.id);
    for (const next of neighbors(graph, indexes, current.kind, current.id)) {
      if (!alreadyVisible(visible, next.kind, next.id)) queue.push(next);
    }
  }

  return visible;
}

function enqueueMatches(
  queue: { kind: keyof Highlighted; id: string }[],
  matches: Highlighted,
): void {
  for (const id of matches.providers) queue.push({ kind: "providers", id });
  for (const id of matches.models) queue.push({ kind: "models", id });
  for (const id of matches.providerModels) {
    queue.push({ kind: "providerModels", id });
  }
  for (const id of matches.assignments) queue.push({ kind: "assignments", id });
  for (const id of matches.capabilities) queue.push({ kind: "capabilities", id });
}

function neighbors(
  graph: LlmGraphPayload,
  indexes: LlmIndexes,
  kind: keyof Highlighted,
  id: string,
): { kind: keyof Highlighted; id: string }[] {
  if (kind === "providers") {
    return (indexes.providerModelsByProviderId.get(id) ?? []).map((pm) => ({
      kind: "providerModels",
      id: pm.id,
    }));
  }
  if (kind === "models") {
    return (indexes.providerModelsByModelId.get(id) ?? []).map((pm) => ({
      kind: "providerModels",
      id: pm.id,
    }));
  }
  if (kind === "providerModels") {
    const pm = indexes.pmById.get(id);
    const result: { kind: keyof Highlighted; id: string }[] = [];
    if (pm) {
      result.push({ kind: "providers", id: pm.provider_id });
      result.push({ kind: "models", id: pm.model_id });
    }
    for (const assignment of graph.assignments) {
      if (assignment.provider_model_id === id) {
        result.push({ kind: "assignments", id: assignment.id });
      }
    }
    return result;
  }
  if (kind === "assignments") {
    const assignment = graph.assignments.find((item) => item.id === id);
    return assignment
      ? [
          { kind: "providerModels", id: assignment.provider_model_id },
          { kind: "capabilities", id: assignment.capability },
        ]
      : [];
  }
  const result: { kind: keyof Highlighted; id: string }[] = [];
  for (const assignment of indexes.assignmentsByCapability.get(id) ?? []) {
    result.push({ kind: "assignments", id: assignment.id });
  }
  const parent = indexes.inheritanceByChild.get(id);
  if (parent) result.push({ kind: "capabilities", id: parent });
  for (const child of indexes.childrenByParent.get(id) ?? []) {
    result.push({ kind: "capabilities", id: child });
  }
  return result;
}

function filterGraph(graph: LlmGraphPayload, visible: Highlighted): LlmGraphPayload {
  const providers = graph.providers.filter((item) => visible.providers.has(item.id));
  const models = graph.models.filter((item) => visible.models.has(item.id));
  const providerModels = graph.provider_models.filter((item) =>
    visible.providerModels.has(item.id),
  );
  const capabilities = graph.capabilities.filter((item) =>
    visible.capabilities.has(item.key),
  );
  const assignments = graph.assignments.filter((item) =>
    visible.assignments.has(item.id),
  );

  return {
    providers,
    models,
    provider_models: providerModels,
    capabilities,
    inheritance: graph.inheritance.filter(
      (item) =>
        visible.capabilities.has(item.capability) &&
        visible.capabilities.has(item.inherits_from),
    ),
    assignments,
    assignment_issues: graph.assignment_issues.filter(
      (item) =>
        visible.assignments.has(item.assignment_id) &&
        visible.capabilities.has(item.capability),
    ),
    totals: {
      ...graph.totals,
      provider_count: providers.length,
      model_count: models.length,
      capability_count: capabilities.length,
      unassigned_capabilities: graph.totals.unassigned_capabilities.filter((key) =>
        visible.capabilities.has(key),
      ),
    },
  };
}

function alreadyVisible(visible: Highlighted, kind: keyof Highlighted, id: string) {
  return visible[kind].has(id);
}

function addVisible(visible: Highlighted, kind: keyof Highlighted, id: string): void {
  visible[kind].add(id);
}

function textMatches(normalizedQuery: string, ...values: unknown[]): boolean {
  return values.some((value) => normalizeSearch(String(value ?? "")).includes(normalizedQuery));
}

function normalizeSearch(value: string): string {
  return value.trim().toLowerCase();
}

function endpointHost(endpoint: string): string {
  try {
    return new URL(endpoint).host;
  } catch {
    return endpoint;
  }
}

function thinkingLevelText(strategy: string | null): string {
  if (!strategy || strategy === "none") return "";
  return strategy.replaceAll("_", " ");
}
