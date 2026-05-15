import { describe, expect, it } from "vitest";
import type {
  LlmAssignment,
  LlmCapabilityEntry,
  LlmGraphPayload,
  LlmModel,
  LlmProvider,
  LlmProviderModel,
} from "@/types";
import { buildLlmGraphLayout } from "./graphLayout";
import { buildLlmIndexes } from "./llmIndexes";

function provider(id: string, name: string): LlmProvider {
  return {
    id,
    name,
    provider_type: "openrouter",
    endpoint: "https://example.test",
    api_key_ref: null,
    api_key_status: "present",
    default_model: null,
    requests_per_minute: 60,
    timeout_s: 60,
    is_enabled: true,
    provider_model_count: 1,
    spend_usd_30d: 0,
    calls_30d: 0,
  };
}

function model(id: string, displayName: string): LlmModel {
  return {
    id,
    canonical_name: id,
    display_name: displayName,
    capabilities: ["chat"],
    context_window: null,
    max_output_tokens: null,
    thinking_level: "disabled",
    thinking_strategy: "none",
    price_source: "manual",
    price_source_model_id: null,
    is_active: true,
    notes: null,
    provider_model_count: 1,
    spend_usd_30d: 0,
    calls_30d: 0,
  };
}

function providerModel(
  id: string,
  providerId: string,
  modelId: string,
): LlmProviderModel {
  return {
    id,
    provider_id: providerId,
    model_id: modelId,
    api_model_id: id,
    input_cost_per_million: 0,
    output_cost_per_million: 0,
    fixed_cost_per_call_usd: null,
    max_tokens_override: null,
    temperature_override: null,
    supports_system_prompt: true,
    supports_temperature: true,
    thinking_strategy_override: null,
    effective_thinking_strategy: "none",
    extra_api_params: {},
    price_source_override: "",
    price_source_model_id_override: null,
    price_last_synced_at: null,
    is_enabled: true,
    spend_usd_30d: 0,
    calls_30d: 0,
  };
}

function capability(key: string): LlmCapabilityEntry {
  return {
    key,
    description: key,
    required_capabilities: ["chat"],
    spend_usd_30d: 0,
    calls_30d: 0,
    direct_spend_usd_30d: 0,
    direct_calls_30d: 0,
    inherited_spend_usd_30d: 0,
    inherited_calls_30d: 0,
  };
}

function assignment(
  id: string,
  capabilityKey: string,
  priority: number,
  providerModelId: string,
): LlmAssignment {
  return {
    id,
    capability: capabilityKey,
    description: capabilityKey,
    priority,
    provider_model_id: providerModelId,
    max_tokens: null,
    temperature: null,
    thinking_level_override: null,
    effective_thinking_level: "disabled",
    effective_thinking_strategy: "none",
    extra_api_params: {},
    required_capabilities: ["chat"],
    is_enabled: true,
    last_used_at: null,
    spend_usd_30d: 0,
    calls_30d: 0,
    direct_spend_usd_30d: 0,
    direct_calls_30d: 0,
    inherited_spend_usd_30d: 0,
    inherited_calls_30d: 0,
  };
}

function layoutFor(graph: LlmGraphPayload) {
  return buildLlmGraphLayout(graph, buildLlmIndexes(graph));
}

describe("buildLlmGraphLayout", () => {
  it("orders connected providers and models by first-seen assignment traversal", () => {
    const graph: LlmGraphPayload = {
      providers: [
        provider("provider_c", "Provider C"),
        provider("provider_a", "Provider A"),
        provider("provider_b", "Provider B"),
      ],
      models: [
        model("model_alpha", "Alpha"),
        model("model_beta", "Beta"),
        model("model_gamma", "Gamma"),
      ],
      provider_models: [
        providerModel("pm_c_alpha", "provider_c", "model_alpha"),
        providerModel("pm_a_beta", "provider_a", "model_beta"),
        providerModel("pm_b_gamma", "provider_b", "model_gamma"),
      ],
      capabilities: [
        capability("cap_b"),
        capability("cap_a"),
        capability("cap_c"),
      ],
      inheritance: [],
      assignments: [
        assignment("assign_b", "cap_b", 0, "pm_b_gamma"),
        assignment("assign_a", "cap_a", 0, "pm_a_beta"),
        assignment("assign_c", "cap_c", 0, "pm_c_alpha"),
      ],
      assignment_issues: [],
      totals: {
        spend_usd_30d: 0,
        calls_30d: 0,
        provider_count: 3,
        model_count: 3,
        capability_count: 3,
        unassigned_capabilities: [],
      },
    };

    const layout = layoutFor(graph);

    expect(layout.providers.map((item) => item.id)).toEqual([
      "provider_b",
      "provider_a",
      "provider_c",
    ]);
    expect(layout.models.map((item) => item.id)).toEqual([
      "model_gamma",
      "model_beta",
      "model_alpha",
    ]);
    expect(layout.assignmentGroups.map((group) => group.capability.key)).toEqual([
      "cap_b",
      "cap_a",
      "cap_c",
    ]);
  });

  it("preserves priority order inside a capability chain", () => {
    const graph: LlmGraphPayload = {
      providers: [provider("provider_a", "Provider A")],
      models: [model("model_alpha", "Alpha"), model("model_beta", "Beta")],
      provider_models: [
        providerModel("pm_alpha", "provider_a", "model_alpha"),
        providerModel("pm_beta", "provider_a", "model_beta"),
      ],
      capabilities: [capability("default")],
      inheritance: [],
      assignments: [
        assignment("fallback", "default", 1, "pm_beta"),
        assignment("primary", "default", 0, "pm_alpha"),
      ],
      assignment_issues: [],
      totals: {
        spend_usd_30d: 0,
        calls_30d: 0,
        provider_count: 1,
        model_count: 2,
        capability_count: 1,
        unassigned_capabilities: [],
      },
    };

    const [group] = layoutFor(graph).assignmentGroups;

    expect(group?.chain.map((item) => item.id)).toEqual(["primary", "fallback"]);
  });

  it("keeps unconnected nodes stable after connected graph nodes", () => {
    const graph: LlmGraphPayload = {
      providers: [
        provider("provider_idle", "Provider Idle"),
        provider("provider_live", "Provider Live"),
      ],
      models: [model("model_idle", "Idle"), model("model_live", "Live")],
      provider_models: [providerModel("pm_live", "provider_live", "model_live")],
      capabilities: [capability("unassigned"), capability("default")],
      inheritance: [],
      assignments: [assignment("default_primary", "default", 0, "pm_live")],
      assignment_issues: [],
      totals: {
        spend_usd_30d: 0,
        calls_30d: 0,
        provider_count: 2,
        model_count: 2,
        capability_count: 2,
        unassigned_capabilities: ["unassigned"],
      },
    };

    const layout = layoutFor(graph);

    expect(layout.providers.map((item) => item.id)).toEqual([
      "provider_live",
      "provider_idle",
    ]);
    expect(layout.models.map((item) => item.id)).toEqual([
      "model_live",
      "model_idle",
    ]);
    expect(layout.assignmentGroups.map((group) => group.capability.key)).toEqual([
      "default",
      "unassigned",
    ]);
  });

  it("keeps inherited child capabilities nested under their parent", () => {
    const graph: LlmGraphPayload = {
      providers: [provider("provider_a", "Provider A")],
      models: [model("model_alpha", "Alpha")],
      provider_models: [providerModel("pm_alpha", "provider_a", "model_alpha")],
      capabilities: [capability("default"), capability("voice.transcribe")],
      inheritance: [
        {
          capability: "voice.transcribe",
          inherits_from: "default",
          source: "implicit_default",
        },
      ],
      assignments: [assignment("default_primary", "default", 0, "pm_alpha")],
      assignment_issues: [],
      totals: {
        spend_usd_30d: 0,
        calls_30d: 0,
        provider_count: 1,
        model_count: 1,
        capability_count: 2,
        unassigned_capabilities: [],
      },
    };

    const [group] = layoutFor(graph).assignmentGroups;

    expect(group?.capability.key).toBe("default");
    expect(group?.inheritedChildren.map((child) => child.key)).toEqual([
      "voice.transcribe",
    ]);
  });
});
