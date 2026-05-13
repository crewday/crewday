import { useId, useMemo, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import FormModal, {
  FormModalField,
  FormModalGrid,
} from "@/components/FormModal";
import SearchableSelect, { type SearchableSelectOption } from "@/components/SearchableSelect";
import { ApiError, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type {
  LlmAssignment,
  LlmModel,
  LlmPriceSource,
  LlmPriceSourceOverride,
  LlmProvider,
  LlmProviderModelPlaygroundMode,
  LlmProviderModelPlaygroundRequest,
  LlmProviderModelPlaygroundResponse,
  LlmProviderModel,
  LlmProviderType,
  LlmReasoningEffort,
  LlmThinkingLevel,
  LlmThinkingStrategy,
} from "@/types";
import type { LlmIndexes } from "./lib/llmIndexes";
import {
  THINKING_LEVEL_OPTIONS,
  THINKING_STRATEGY_OPTIONS,
  isThinkingLevel,
  isThinkingStrategy,
  thinkingLevelLabel,
  thinkingStrategyLabel,
} from "./lib/llmThinking";

export type RegistryDialogState =
  | { kind: "provider"; mode: "create" }
  | { kind: "provider"; mode: "edit"; id: string }
  | { kind: "model"; mode: "create" }
  | { kind: "model"; mode: "edit"; id: string }
  | { kind: "providerModel"; mode: "create" }
  | { kind: "providerModel"; mode: "edit"; id: string };

interface RegistryModalsProps {
  dialog: RegistryDialogState | null;
  providers: LlmProvider[];
  models: LlmModel[];
  providerModels: LlmProviderModel[];
  indexes: LlmIndexes;
  onClose: () => void;
}

interface ProviderFormProps {
  mode: "create" | "edit";
  provider?: LlmProvider;
  providerModels: LlmProviderModel[];
  models: LlmModel[];
  titleId?: string;
  onClose: () => void;
}

interface ProviderModelFormProps {
  mode: "create" | "edit";
  providerModel?: LlmProviderModel;
  providers: LlmProvider[];
  models: LlmModel[];
  assignments: LlmAssignment[];
  titleId?: string;
  onClose: () => void;
}

interface ProviderPayload {
  name: string;
  provider_type: LlmProviderType;
  api_endpoint: string | null;
  api_key_envelope_ref: string | null;
  default_model: string | null;
  timeout_s: number;
  requests_per_minute: number;
  is_enabled: boolean;
}

interface ModelPayload {
  canonical_name: string;
  display_name: string;
  vendor: string;
  capabilities: string[];
  context_window: number | null;
  max_output_tokens: number | null;
  thinking_level: LlmThinkingLevel;
  thinking_strategy: LlmThinkingStrategy;
  price_source: LlmPriceSource;
  price_source_model_id: string | null;
  is_active: boolean;
  notes: string | null;
}

interface OpenRouterProviderModelPreview {
  provider_id: string;
  provider_name: string;
  existing_provider_model_id: string | null;
  payload: ProviderModelPayload;
}

interface OpenRouterModelPreviewResponse {
  openrouter_model_id: string;
  existing_model_id: string | null;
  model_payload: ModelPayload;
  provider_model_previews: OpenRouterProviderModelPreview[];
}

interface OpenRouterPricingPreview {
  providerName: string;
  providerCount: number;
  inputCostPerMillion: number;
  outputCostPerMillion: number;
  fixedCostPerCallUsd: number | null;
}

interface ProviderModelPayload {
  provider_id: string;
  model_id: string;
  api_model_id: string;
  input_cost_per_million: number;
  output_cost_per_million: number;
  fixed_cost_per_call_usd: number | null;
  max_tokens_override: number | null;
  temperature_override: number | null;
  supports_system_prompt: boolean;
  supports_temperature: boolean;
  thinking_level_override: LlmThinkingLevel | null;
  thinking_strategy_override: LlmThinkingStrategy | null;
  reasoning_effort: LlmReasoningEffort;
  extra_api_params: Record<string, unknown>;
  price_source_override: LlmPriceSourceOverride;
  price_source_model_id_override: string | null;
  is_enabled: boolean;
}

const CAPABILITY_TAGS = [
  "chat",
  "vision",
  "audio_input",
  "reasoning",
  "function_calling",
  "json_mode",
  "streaming",
  "embeddings",
] as const;

function emptyToNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function numberOrNull(value: string): number | null {
  if (value.trim() === "") return null;
  return Number(value);
}

function optionalNonNegativeNumber(
  value: string,
  label: string,
): { ok: true; value: number | null } | { ok: false; error: string } {
  const parsed = numberOrNull(value);
  if (parsed !== null && (!Number.isFinite(parsed) || parsed < 0)) {
    return { ok: false, error: `${label} must be zero or more.` };
  }
  return { ok: true, value: parsed };
}

function optionalPositiveInteger(
  value: string,
  label: string,
): { ok: true; value: number | null } | { ok: false; error: string } {
  const parsed = numberOrNull(value);
  if (parsed !== null && (!Number.isInteger(parsed) || parsed < 1)) {
    return { ok: false, error: `${label} must be a positive whole number.` };
  }
  return { ok: true, value: parsed };
}

function optionalTemperature(
  value: string,
): { ok: true; value: number | null } | { ok: false; error: string } {
  const parsed = numberOrNull(value);
  if (parsed !== null && (!Number.isFinite(parsed) || parsed < 0 || parsed > 2)) {
    return { ok: false, error: "Temperature must be between 0 and 2." };
  }
  return { ok: true, value: parsed };
}

function errorCode(error: unknown): string | null {
  if (error instanceof ApiError && typeof error.problem?.error === "string") {
    return error.problem.error;
  }
  return null;
}

function apiErrorCopy(error: unknown, fallback: string): string {
  const code = errorCode(error);
  if (code === "provider_in_use") {
    return "This provider is still attached to provider-model rows. Delete or move those joins first.";
  }
  if (code === "model_in_use") {
    return "This model is still attached to provider-model rows. Delete or move those joins first.";
  }
  if (code === "provider_model_in_use") {
    return "This provider-model is assigned to one or more capabilities. Move those assignments first.";
  }
  if (error instanceof ApiError) {
    return error.detail ?? error.title ?? error.message ?? fallback;
  }
  return error instanceof Error ? error.message : fallback;
}

function playgroundErrorCopy(error: unknown): string {
  const code = errorCode(error);
  if (code === "assignment_not_found" || code === "assignment_provider_model_mismatch") {
    return "That assignment no longer points at this provider-model.";
  }
  if (code === "assignment_disabled") {
    return "That assignment is disabled.";
  }
  if (code === "provider_model_disabled") {
    return "This provider-model is disabled.";
  }
  if (code === "provider_disabled") {
    return "This provider is disabled.";
  }
  if (code === "model_inactive") {
    return "This model is inactive.";
  }
  if (code === "system_prompt_not_supported") {
    return "This provider-model does not support system prompts.";
  }
  if (code === "temperature_not_supported") {
    return "This provider-model does not support temperature overrides.";
  }
  if (code === "image_requires_vision_model") {
    return "Images require a vision-capable model.";
  }
  if (code === "image_playground_not_supported") {
    return "Image playground runs are not supported yet.";
  }
  if (code === "max_tokens_exceeds_model_limit") {
    return "Max tokens exceeds this model's output limit.";
  }
  if (code === "max_tokens_exceeds_playground_limit") {
    return "Max tokens exceeds the playground limit.";
  }
  if (code === "provider_type_not_supported") {
    return "This provider type is not supported by the playground.";
  }
  if (code === "provider_client_unavailable" || code === "provider_api_key_missing") {
    return "The provider client is not configured for playground runs.";
  }
  if (error instanceof ApiError) {
    return error.title ?? "Playground run failed.";
  }
  return "Playground run failed.";
}

function openRouterMetadataErrorCopy(error: unknown): string {
  const code = errorCode(error);
  if (code === "invalid_openrouter_model_id") {
    return "Enter an OpenRouter model id or URL.";
  }
  if (code === "openrouter_model_not_found") {
    return "OpenRouter did not find that model.";
  }
  if (code === "openrouter_unavailable") {
    return "OpenRouter metadata is temporarily unavailable.";
  }
  if (error instanceof ApiError) {
    return error.detail ?? error.title ?? "OpenRouter metadata load failed.";
  }
  return error instanceof Error ? error.message : "OpenRouter metadata load failed.";
}

function describedBy(...ids: (string | undefined)[]): string | undefined {
  const present = ids.filter((id): id is string => id !== undefined);
  return present.length ? present.join(" ") : undefined;
}

function formatNullable(value: string | number | null | undefined, suffix = ""): string {
  if (value === null || value === undefined || value === "") return "n/a";
  return `${value}${suffix}`;
}

function formatPlaygroundCost(value: string | null): string {
  if (value === null) return "n/a";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return value;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: numeric === 0 ? 2 : 6,
    maximumFractionDigits: numeric < 0.01 ? 6 : 2,
  }).format(numeric);
}

function formatCostPerMillion(value: number): string {
  return `${new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: value === 0 ? 0 : 2,
    maximumFractionDigits: value < 0.01 && value > 0 ? 6 : 2,
  }).format(value)}/M`;
}

function providerOption(provider: LlmProvider): SearchableSelectOption {
  return {
    value: provider.id,
    label: provider.name,
    secondaryText: provider.provider_type,
    searchText: [
      provider.name,
      provider.provider_type,
      provider.endpoint ?? "",
      provider.id,
    ].join(" "),
  };
}

function modelOption(model: LlmModel): SearchableSelectOption {
  const capabilities = model.capabilities.join(", ");
  return {
    value: model.id,
    label: model.display_name,
    secondaryText: [model.vendor, capabilities].filter(Boolean).join(" - "),
    searchText: [
      model.display_name,
      model.canonical_name,
      model.vendor,
      capabilities,
      model.id,
    ].join(" "),
  };
}

function providerModelOption(
  pm: LlmProviderModel,
  models: readonly LlmModel[],
  provider?: LlmProvider,
): SearchableSelectOption {
  const model = models.find((item) => item.id === pm.model_id);
  const capabilities = model?.capabilities.join(", ");
  return {
    value: pm.id,
    label: `${model?.display_name ?? pm.api_model_id} (${pm.api_model_id})`,
    secondaryText: [
      provider?.name,
      model?.canonical_name,
      capabilities,
    ].filter(Boolean).join(" - "),
    searchText: [
      provider?.name,
      model?.display_name,
      model?.canonical_name,
      pm.api_model_id,
      capabilities,
      pm.id,
    ].filter(Boolean).join(" "),
  };
}

export default function LlmRegistryModals(props: RegistryModalsProps) {
  // code-health: ignore[ccn nloc] Lizard misattributes the registry form bodies to this modal dispatcher; each form is implemented below.
  const { dialog, providers, models, providerModels, indexes, onClose } = props;

  const titleId = dialog ? `llm-${dialog.kind}-${dialog.mode}-title` : undefined;
  const assignments = useMemo(
    () => Array.from(indexes.assignmentsByCapability.values()).flat(),
    [indexes],
  );

  return (
    <>
      {dialog?.kind === "provider" ? (
        <ProviderForm
          mode={dialog.mode}
          provider={
            dialog.mode === "edit" ? indexes.providersById.get(dialog.id) : undefined
          }
          providerModels={providerModels}
          models={models}
          titleId={titleId}
          onClose={onClose}
        />
      ) : null}
      {dialog?.kind === "model" ? (
        <ModelForm
          mode={dialog.mode}
          model={dialog.mode === "edit" ? indexes.modelsById.get(dialog.id) : undefined}
          titleId={titleId}
          onClose={onClose}
        />
      ) : null}
      {dialog?.kind === "providerModel" ? (
        <ProviderModelForm
          mode={dialog.mode}
          providerModel={
            dialog.mode === "edit" ? indexes.pmById.get(dialog.id) : undefined
          }
          providers={providers}
          models={models}
          assignments={assignments}
          titleId={titleId}
          onClose={onClose}
        />
      ) : null}
    </>
  );
}

function ProviderForm(props: ProviderFormProps) {
  // code-health: ignore[ccn] Provider form keeps validation beside the provider payload and API mutation it drives.
  const { mode, provider, providerModels, models, titleId, onClose } = props;
  const qc = useQueryClient();
  const [name, setName] = useState(provider?.name ?? "");
  const [providerType, setProviderType] = useState<LlmProviderType>(
    provider?.provider_type ?? "openrouter",
  );
  const [apiEndpoint, setApiEndpoint] = useState(provider?.endpoint ?? "");
  const [apiKeyRef, setApiKeyRef] = useState(provider?.api_key_ref ?? "");
  const [defaultModel, setDefaultModel] = useState(provider?.default_model ?? "");
  const [timeout, setTimeoutValue] = useState(String(provider?.timeout_s ?? 60));
  const [rpm, setRpm] = useState(String(provider?.requests_per_minute ?? 60));
  const [enabled, setEnabled] = useState(provider?.is_enabled ?? true);
  const [clientErr, setClientErr] = useState<string | null>(null);
  const [serverErr, setServerErr] = useState<string | null>(null);

  const defaultModelOptions = useMemo(() => {
    if (!provider) return [];
    return providerModels
      .filter((pm) => pm.provider_id === provider.id)
      .map((pm) => providerModelOption(pm, models, provider));
  }, [models, provider, providerModels]);

  const invalidate = async () => {
    await qc.invalidateQueries({ queryKey: qk.adminLlmGraph() });
  };
  const save = useMutation({
    mutationFn: (body: ProviderPayload) =>
      fetchJson<LlmProvider>(
        mode === "create"
          ? "/admin/api/v1/llm/providers"
          : `/admin/api/v1/llm/providers/${provider?.id}`,
        { method: mode === "create" ? "POST" : "PUT", body },
      ),
    onSuccess: async () => {
      await invalidate();
      onClose();
    },
    onError: (error: Error) => setServerErr(apiErrorCopy(error, "Provider save failed.")),
  });
  const remove = useMutation({
    mutationFn: () =>
      fetchJson(`/admin/api/v1/llm/providers/${provider?.id}`, {
        method: "DELETE",
      }),
    onSuccess: async () => {
      await invalidate();
      onClose();
    },
    onError: (error: Error) =>
      setServerErr(apiErrorCopy(error, "Provider delete failed.")),
  });

  const err = clientErr ?? serverErr;
  const errId = err ? "llm-provider-error" : undefined;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const timeoutValue = Number(timeout);
    const rpmValue = Number(rpm);
    if (!name.trim()) return setClientErr("Name is required.");
    if (providerType === "openai_compatible" && !apiEndpoint.trim()) {
      return setClientErr("OpenAI-compatible providers need an API endpoint.");
    }
    if (!Number.isInteger(timeoutValue) || timeoutValue < 1) {
      return setClientErr("Timeout must be at least 1 second.");
    }
    if (!Number.isInteger(rpmValue) || rpmValue < 1) {
      return setClientErr("Requests per minute must be at least 1.");
    }
    setClientErr(null);
    setServerErr(null);
    save.mutate({
      name: name.trim(),
      provider_type: providerType,
      api_endpoint: emptyToNull(apiEndpoint),
      api_key_envelope_ref: emptyToNull(apiKeyRef),
      default_model: emptyToNull(defaultModel),
      timeout_s: timeoutValue,
      requests_per_minute: rpmValue,
      is_enabled: enabled,
    });
  }

  return (
    <FormModal
      open
      title={mode === "create" ? "Create provider" : provider?.name}
      titleId={titleId}
      eyebrow={mode === "create" ? "New provider" : "Edit provider"}
      onClose={onClose}
      onSubmit={submit}
      noValidate
      actions={
        <>
          {mode === "edit" ? (
            <button
              type="button"
              className="btn btn--rust llm-registry-form__delete"
              onClick={() => remove.mutate()}
              disabled={remove.isPending || save.isPending}
            >
              {remove.isPending ? "Deleting…" : "Delete provider"}
            </button>
          ) : null}
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="btn btn--moss"
            disabled={save.isPending || remove.isPending}
          >
            {save.isPending ? "Saving…" : mode === "create" ? "Create provider" : "Save provider"}
          </button>
        </>
      }
    >
        <FormModalField label="Name" requirement="required">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            aria-invalid={clientErr === "Name is required."}
            aria-describedby={errId}
          />
        </FormModalField>
        <FormModalGrid>
          <FormModalField label="Type" requirement="required">
            <select
              value={providerType}
              onChange={(e) => setProviderType(e.target.value as LlmProviderType)}
              required
            >
              <option value="openrouter">OpenRouter</option>
              <option value="openai_compatible">OpenAI compatible</option>
              <option value="fake">Fake</option>
            </select>
          </FormModalField>
          <FormModalField label="Enabled" requirement="required">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
          </FormModalField>
        </FormModalGrid>
        <FormModalField label="API endpoint" requirement="optional">
          <input
            value={apiEndpoint}
            onChange={(e) => setApiEndpoint(e.target.value)}
            aria-invalid={
              clientErr === "OpenAI-compatible providers need an API endpoint."
            }
            aria-describedby={errId}
          />
        </FormModalField>
        <FormModalField label="API key envelope ref" requirement="optional">
          <input value={apiKeyRef} onChange={(e) => setApiKeyRef(e.target.value)} />
        </FormModalField>
        <SearchableSelect
          label="Default provider-model"
          requirement="optional"
          className="form-modal__field"
          value={defaultModel}
          options={defaultModelOptions}
          blankOption={{ label: "None" }}
          onChange={setDefaultModel}
          disabled={defaultModelOptions.length === 0}
        />
        <FormModalGrid>
          <FormModalField label="Timeout seconds" requirement="required">
            <input
              type="number"
              min="1"
              value={timeout}
              onChange={(e) => setTimeoutValue(e.target.value)}
              required
              aria-invalid={clientErr === "Timeout must be at least 1 second."}
              aria-describedby={errId}
            />
          </FormModalField>
          <FormModalField label="Requests per minute" requirement="required">
            <input
              type="number"
              min="1"
              value={rpm}
              onChange={(e) => setRpm(e.target.value)}
              required
              aria-invalid={clientErr === "Requests per minute must be at least 1."}
              aria-describedby={errId}
            />
          </FormModalField>
        </FormModalGrid>
        {err ? (
          <p id="llm-provider-error" className="form-error" role="alert">
            {err}
          </p>
        ) : null}
    </FormModal>
  );
}

function ModelForm({
  mode,
  model,
  titleId,
  onClose,
}: {
  mode: "create" | "edit";
  model?: LlmModel;
  titleId?: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const openRouterInputId = useId();
  const [canonicalName, setCanonicalName] = useState(model?.canonical_name ?? "");
  const [displayName, setDisplayName] = useState(model?.display_name ?? "");
  const [vendor, setVendor] = useState(model?.vendor ?? "");
  const [capabilities, setCapabilities] = useState<string[]>(model?.capabilities ?? []);
  const [contextWindow, setContextWindow] = useState(
    model?.context_window === null || model?.context_window === undefined
      ? ""
      : String(model.context_window),
  );
  const [maxOutput, setMaxOutput] = useState(
    model?.max_output_tokens === null || model?.max_output_tokens === undefined
      ? ""
      : String(model.max_output_tokens),
  );
  const [priceSource, setPriceSource] = useState<LlmPriceSource>(
    model?.price_source ?? "",
  );
  const [thinkingLevel, setThinkingLevel] = useState<LlmThinkingLevel>(
    model?.thinking_level ?? "disabled",
  );
  const [thinkingStrategy, setThinkingStrategy] = useState<LlmThinkingStrategy>(
    model?.thinking_strategy ?? "none",
  );
  const [priceSourceModel, setPriceSourceModel] = useState(
    model?.price_source_model_id ?? "",
  );
  const [active, setActive] = useState(model?.is_active ?? true);
  const [notes, setNotes] = useState(model?.notes ?? "");
  const [openRouterModel, setOpenRouterModel] = useState("");
  const [openRouterErr, setOpenRouterErr] = useState<string | null>(null);
  const [openRouterStatus, setOpenRouterStatus] = useState<string | null>(null);
  const [openRouterPricing, setOpenRouterPricing] =
    useState<OpenRouterPricingPreview | null>(null);
  const [clientErr, setClientErr] = useState<string | null>(null);
  const [serverErr, setServerErr] = useState<string | null>(null);

  const invalidate = async () => {
    await qc.invalidateQueries({ queryKey: qk.adminLlmGraph() });
  };
  const save = useMutation({
    mutationFn: (body: ModelPayload) =>
      fetchJson<LlmModel>(
        mode === "create"
          ? "/admin/api/v1/llm/models"
          : `/admin/api/v1/llm/models/${model?.id}`,
        { method: mode === "create" ? "POST" : "PUT", body },
      ),
    onSuccess: async () => {
      await invalidate();
      onClose();
    },
    onError: (error: Error) => setServerErr(apiErrorCopy(error, "Model save failed.")),
  });
  const remove = useMutation({
    mutationFn: () =>
      fetchJson(`/admin/api/v1/llm/models/${model?.id}`, { method: "DELETE" }),
    onSuccess: async () => {
      await invalidate();
      onClose();
    },
    onError: (error: Error) => setServerErr(apiErrorCopy(error, "Model delete failed.")),
  });
  const openRouterPreview = useMutation({
    mutationFn: (modelIdOrUrl: string) =>
      fetchJson<OpenRouterModelPreviewResponse>(
        "/admin/api/v1/llm/models/openrouter-preview",
        { method: "POST", body: { model_id_or_url: modelIdOrUrl } },
      ),
    onSuccess: (preview) => {
      applyModelPayload(preview.model_payload);
      setOpenRouterErr(null);
      setClientErr(null);
      setServerErr(null);
      setOpenRouterStatus(
        `Loaded OpenRouter metadata for ${preview.openrouter_model_id}.`,
      );
      const firstPreview = preview.provider_model_previews[0];
      setOpenRouterPricing(
        firstPreview
          ? {
              providerName: firstPreview.provider_name,
              providerCount: preview.provider_model_previews.length,
              inputCostPerMillion: firstPreview.payload.input_cost_per_million,
              outputCostPerMillion: firstPreview.payload.output_cost_per_million,
              fixedCostPerCallUsd: firstPreview.payload.fixed_cost_per_call_usd,
            }
          : null,
      );
    },
    onError: (error: Error) => {
      setOpenRouterErr(openRouterMetadataErrorCopy(error));
      setOpenRouterStatus(null);
      setOpenRouterPricing(null);
    },
  });
  const err = clientErr ?? serverErr;
  const errId = err ? "llm-model-error" : undefined;
  const openRouterErrId = openRouterErr ? "llm-openrouter-error" : undefined;
  const openRouterStatusId =
    openRouterPreview.isPending || openRouterStatus
      ? "llm-openrouter-status"
      : undefined;

  function toggleCapability(tag: string) {
    setCapabilities((current) =>
      current.includes(tag)
        ? current.filter((value) => value !== tag)
        : [...current, tag],
    );
  }

  function applyModelPayload(payload: ModelPayload) {
    setCanonicalName(payload.canonical_name);
    setDisplayName(payload.display_name);
    setVendor(payload.vendor);
    setCapabilities(payload.capabilities);
    setContextWindow(
      payload.context_window === null || payload.context_window === undefined
        ? ""
        : String(payload.context_window),
    );
    setMaxOutput(
      payload.max_output_tokens === null || payload.max_output_tokens === undefined
        ? ""
        : String(payload.max_output_tokens),
    );
    setThinkingLevel(payload.thinking_level);
    setThinkingStrategy(payload.thinking_strategy);
    setPriceSource(payload.price_source);
    setPriceSourceModel(payload.price_source_model_id ?? "");
    setActive(payload.is_active);
    setNotes(payload.notes ?? "");
  }

  function loadOpenRouterMetadata() {
    const modelIdOrUrl = openRouterModel.trim();
    if (!modelIdOrUrl) {
      setOpenRouterErr("Enter an OpenRouter model id or URL.");
      setOpenRouterStatus(null);
      setOpenRouterPricing(null);
      return;
    }
    setOpenRouterErr(null);
    setOpenRouterStatus(null);
    setOpenRouterPricing(null);
    openRouterPreview.mutate(modelIdOrUrl);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const contextValue = numberOrNull(contextWindow);
    const outputValue = numberOrNull(maxOutput);
    if (!canonicalName.trim()) return setClientErr("Canonical name is required.");
    if (!displayName.trim()) return setClientErr("Display name is required.");
    if (!vendor.trim()) return setClientErr("Vendor is required.");
    if (contextValue !== null && (!Number.isInteger(contextValue) || contextValue < 1)) {
      return setClientErr("Context window must be a positive whole number.");
    }
    if (outputValue !== null && (!Number.isInteger(outputValue) || outputValue < 1)) {
      return setClientErr("Max output tokens must be a positive whole number.");
    }
    setClientErr(null);
    setServerErr(null);
    save.mutate({
      canonical_name: canonicalName.trim(),
      display_name: displayName.trim(),
      vendor: vendor.trim(),
      capabilities,
      context_window: contextValue,
      max_output_tokens: outputValue,
      thinking_level: thinkingLevel,
      thinking_strategy: thinkingStrategy,
      price_source: priceSource,
      price_source_model_id: emptyToNull(priceSourceModel),
      is_active: active,
      notes: emptyToNull(notes),
    });
  }

  return (
    <FormModal
      open
      title={mode === "create" ? "Create model" : model?.display_name}
      titleId={titleId}
      eyebrow={mode === "create" ? "New model" : "Edit model"}
      onClose={onClose}
      onSubmit={submit}
      noValidate
      actions={
        <>
          {mode === "edit" ? (
            <button
              type="button"
              className="btn btn--rust llm-registry-form__delete"
              onClick={() => remove.mutate()}
              disabled={remove.isPending || save.isPending}
            >
              {remove.isPending ? "Deleting…" : "Delete model"}
            </button>
          ) : null}
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="btn btn--moss"
            disabled={save.isPending || remove.isPending || openRouterPreview.isPending}
          >
            {save.isPending ? "Saving…" : mode === "create" ? "Create model" : "Save model"}
          </button>
        </>
      }
    >
        {mode === "create" ? (
          <div className="field form-field form-field--optional form-modal__field llm-openrouter-loader">
            <label htmlFor={openRouterInputId} className="llm-openrouter-loader__label">
              <span className="form-field__label">
                OpenRouter model{" "}
                <span className="form-field__requirement form-field__requirement--optional">
                  Optional
                </span>
              </span>
            </label>
            <div className="llm-openrouter-loader__control">
              <input
                id={openRouterInputId}
                value={openRouterModel}
                onChange={(e) => setOpenRouterModel(e.target.value)}
                placeholder="google/gemma-4-31b-it"
                aria-invalid={openRouterErr ? true : undefined}
                aria-describedby={describedBy(openRouterErrId, openRouterStatusId)}
              />
              <button
                type="button"
                className="btn btn--ghost llm-openrouter-loader__button"
                onClick={loadOpenRouterMetadata}
                disabled={openRouterPreview.isPending || save.isPending}
              >
                {openRouterPreview.isPending ? "Loading…" : "Load metadata"}
              </button>
            </div>
            {openRouterPreview.isPending || openRouterStatus ? (
              <p
                id="llm-openrouter-status"
                className="llm-openrouter-loader__status"
                role="status"
              >
                {openRouterPreview.isPending
                  ? "Loading OpenRouter metadata..."
                  : openRouterStatus}
              </p>
            ) : null}
            {openRouterPricing ? (
              <p className="llm-openrouter-loader__pricing">
                Provider price preview: {openRouterPricing.providerName}{" "}
                {formatCostPerMillion(openRouterPricing.inputCostPerMillion)} in,{" "}
                {formatCostPerMillion(openRouterPricing.outputCostPerMillion)} out
                {openRouterPricing.fixedCostPerCallUsd !== null
                  ? `, ${new Intl.NumberFormat("en-US", {
                      style: "currency",
                      currency: "USD",
                      maximumFractionDigits: 6,
                    }).format(openRouterPricing.fixedCostPerCallUsd)} fixed`
                  : ""}
                {openRouterPricing.providerCount > 1
                  ? ` across ${openRouterPricing.providerCount} OpenRouter providers`
                  : ""}
                .
              </p>
            ) : null}
            {openRouterErr ? (
              <p id="llm-openrouter-error" className="form-error" role="alert">
                {openRouterErr}
              </p>
            ) : null}
          </div>
        ) : null}
        <FormModalField label="Canonical name" requirement="required">
          <input
            value={canonicalName}
            onChange={(e) => setCanonicalName(e.target.value)}
            required
            aria-invalid={clientErr === "Canonical name is required."}
            aria-describedby={errId}
          />
        </FormModalField>
        <FormModalGrid>
          <FormModalField label="Display name" requirement="required">
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              required
              aria-invalid={clientErr === "Display name is required."}
              aria-describedby={errId}
            />
          </FormModalField>
          <FormModalField label="Vendor" requirement="required">
            <input
              value={vendor}
              onChange={(e) => setVendor(e.target.value)}
              required
              aria-invalid={clientErr === "Vendor is required."}
              aria-describedby={errId}
            />
          </FormModalField>
        </FormModalGrid>
        <fieldset className="llm-registry-form__fieldset">
          <legend>Capabilities</legend>
          <div className="llm-registry-form__checks">
            {CAPABILITY_TAGS.map((tag) => (
              <label key={tag} className="llm-registry-form__check">
                <input
                  type="checkbox"
                  checked={capabilities.includes(tag)}
                  onChange={() => toggleCapability(tag)}
                />
                <span>{tag}</span>
              </label>
            ))}
          </div>
        </fieldset>
        <FormModalGrid>
          <FormModalField label="Context window" requirement="optional">
            <input
              type="number"
              min="1"
              value={contextWindow}
              onChange={(e) => setContextWindow(e.target.value)}
              aria-invalid={clientErr === "Context window must be a positive whole number."}
              aria-describedby={errId}
            />
          </FormModalField>
          <FormModalField label="Max output tokens" requirement="optional">
            <input
              type="number"
              min="1"
              value={maxOutput}
              onChange={(e) => setMaxOutput(e.target.value)}
              aria-invalid={clientErr === "Max output tokens must be a positive whole number."}
              aria-describedby={errId}
            />
          </FormModalField>
        </FormModalGrid>
        <FormModalGrid>
          <FormModalField label="Thinking level" requirement="required">
            <select
              value={thinkingLevel}
              onChange={(e) => {
                if (isThinkingLevel(e.target.value)) {
                  setThinkingLevel(e.target.value);
                }
              }}
            >
              {THINKING_LEVEL_OPTIONS.map((level) => (
                <option key={level} value={level}>
                  {thinkingLevelLabel(level)}
                </option>
              ))}
            </select>
          </FormModalField>
          <FormModalField label="Thinking strategy" requirement="required">
            <select
              value={thinkingStrategy}
              onChange={(e) => {
                if (isThinkingStrategy(e.target.value)) {
                  setThinkingStrategy(e.target.value);
                }
              }}
            >
              {THINKING_STRATEGY_OPTIONS.map((strategy) => (
                <option key={strategy} value={strategy}>
                  {thinkingStrategyLabel(strategy)}
                </option>
              ))}
            </select>
          </FormModalField>
        </FormModalGrid>
        <FormModalGrid>
          <FormModalField label="Price source" requirement="required">
            <select
              value={priceSource}
              onChange={(e) => setPriceSource(e.target.value as LlmPriceSource)}
            >
              <option value="">None</option>
              <option value="openrouter">OpenRouter</option>
              <option value="manual">Manual</option>
            </select>
          </FormModalField>
          <FormModalField label="Price source model id" requirement="optional">
            <input
              value={priceSourceModel}
              onChange={(e) => setPriceSourceModel(e.target.value)}
            />
          </FormModalField>
        </FormModalGrid>
        <FormModalField label="Active" requirement="required">
          <input
            type="checkbox"
            checked={active}
            onChange={(e) => setActive(e.target.checked)}
          />
        </FormModalField>
        <FormModalField label="Notes" requirement="optional">
          <AutoGrowTextarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={3} />
        </FormModalField>
        {err ? (
          <p id="llm-model-error" className="form-error" role="alert">
            {err}
          </p>
        ) : null}
    </FormModal>
  );
}

function ProviderModelForm(props: ProviderModelFormProps) {
  // code-health: ignore[ccn] Provider-model form keeps its pricing, override, capability, and JSON validation next to the save payload.
  const { mode, providerModel, providers, models, assignments, titleId, onClose } = props;
  const qc = useQueryClient();
  const [providerId, setProviderId] = useState(
    providerModel?.provider_id ?? providers[0]?.id ?? "",
  );
  const [modelId, setModelId] = useState(providerModel?.model_id ?? models[0]?.id ?? "");
  const [apiModelId, setApiModelId] = useState(providerModel?.api_model_id ?? "");
  const [inputCost, setInputCost] = useState(
    String(providerModel?.input_cost_per_million ?? 0),
  );
  const [outputCost, setOutputCost] = useState(
    String(providerModel?.output_cost_per_million ?? 0),
  );
  const [fixedCost, setFixedCost] = useState(
    providerModel?.fixed_cost_per_call_usd == null
      ? ""
      : String(providerModel.fixed_cost_per_call_usd),
  );
  const [maxTokens, setMaxTokens] = useState(
    providerModel?.max_tokens_override == null
      ? ""
      : String(providerModel.max_tokens_override),
  );
  const [temperature, setTemperature] = useState(
    providerModel?.temperature_override == null
      ? ""
      : String(providerModel.temperature_override),
  );
  const [supportsSystemPrompt, setSupportsSystemPrompt] = useState(
    providerModel?.supports_system_prompt ?? true,
  );
  const [supportsTemperature, setSupportsTemperature] = useState(
    providerModel?.supports_temperature ?? true,
  );
  const [reasoningEffort] = useState<LlmReasoningEffort>(
    providerModel?.reasoning_effort ?? "",
  );
  const [thinkingOverride, setThinkingOverride] = useState<
    LlmThinkingLevel | "inherit"
  >(providerModel?.thinking_level_override ?? "inherit");
  const [thinkingStrategyOverride, setThinkingStrategyOverride] = useState<
    LlmThinkingStrategy | "inherit"
  >(providerModel?.thinking_strategy_override ?? "inherit");
  const [priceSourceOverride, setPriceSourceOverride] =
    useState<LlmPriceSourceOverride>(providerModel?.price_source_override ?? "");
  const [priceSourceModelOverride, setPriceSourceModelOverride] = useState(
    providerModel?.price_source_model_id_override ?? "",
  );
  const [extraApiParams, setExtraApiParams] = useState(
    JSON.stringify(providerModel?.extra_api_params ?? {}, null, 2),
  );
  const [enabled, setEnabled] = useState(providerModel?.is_enabled ?? true);
  const [clientErr, setClientErr] = useState<string | null>(null);
  const [serverErr, setServerErr] = useState<string | null>(null);
  const providerOptions = useMemo(() => providers.map(providerOption), [providers]);
  const modelOptions = useMemo(() => models.map(modelOption), [models]);
  const selectedModel = useMemo(
    () => models.find((item) => item.id === modelId),
    [modelId, models],
  );
  const persistedModel = useMemo(
    () =>
      providerModel
        ? models.find((item) => item.id === providerModel.model_id)
        : undefined,
    [models, providerModel],
  );
  const inheritedThinkingLevel =
    selectedModel?.thinking_level ?? providerModel?.effective_thinking_level ?? "disabled";
  const effectiveThinkingLevel =
    thinkingOverride === "inherit" ? inheritedThinkingLevel : thinkingOverride;
  const inheritedThinkingStrategy =
    selectedModel?.thinking_strategy ??
    providerModel?.effective_thinking_strategy ??
    "none";
  const effectiveThinkingStrategy =
    thinkingStrategyOverride === "inherit"
      ? inheritedThinkingStrategy
      : thinkingStrategyOverride;

  const invalidate = async () => {
    await qc.invalidateQueries({ queryKey: qk.adminLlmGraph() });
  };
  const save = useMutation({
    mutationFn: (body: ProviderModelPayload) =>
      fetchJson<LlmProviderModel>(
        mode === "create"
          ? "/admin/api/v1/llm/provider-models"
          : `/admin/api/v1/llm/provider-models/${providerModel?.id}`,
        { method: mode === "create" ? "POST" : "PUT", body },
      ),
    onSuccess: async () => {
      await invalidate();
      onClose();
    },
    onError: (error: Error) =>
      setServerErr(apiErrorCopy(error, "Provider-model save failed.")),
  });
  const remove = useMutation({
    mutationFn: () =>
      fetchJson(`/admin/api/v1/llm/provider-models/${providerModel?.id}`, {
        method: "DELETE",
      }),
    onSuccess: async () => {
      await invalidate();
      onClose();
    },
    onError: (error: Error) =>
      setServerErr(apiErrorCopy(error, "Provider-model delete failed.")),
  });
  const err = clientErr ?? serverErr;
  const errId = err ? "llm-provider-model-error" : undefined;
  const extraHelpId = "llm-provider-model-extra-help";
  const thinkingHelpId =
    thinkingOverride === "inherit" ? "llm-provider-model-thinking-help" : undefined;
  const thinkingStrategyHelpId = "llm-provider-model-thinking-strategy-help";

  function submit(event: FormEvent<HTMLFormElement>) {
    // code-health: ignore[ccn] Provider-model submit intentionally keeps all field validation next to the payload it sends.
    event.preventDefault();
    if (!providerId) return setClientErr("Provider is required.");
    if (!modelId) return setClientErr("Model is required.");
    if (!apiModelId.trim()) return setClientErr("API model id is required.");
    if (inputCost.trim() === "") return setClientErr("Input cost is required.");
    if (outputCost.trim() === "") return setClientErr("Output cost is required.");
    const inputParsed = optionalNonNegativeNumber(inputCost, "Input cost");
    if (!inputParsed.ok) return setClientErr(inputParsed.error);
    if (inputParsed.value === null) return setClientErr("Input cost is required.");
    const outputParsed = optionalNonNegativeNumber(outputCost, "Output cost");
    if (!outputParsed.ok) return setClientErr(outputParsed.error);
    if (outputParsed.value === null) return setClientErr("Output cost is required.");
    const fixedParsed = optionalNonNegativeNumber(fixedCost, "Fixed cost");
    if (!fixedParsed.ok) return setClientErr(fixedParsed.error);
    const maxTokensValue = numberOrNull(maxTokens);
    const temperatureValue = numberOrNull(temperature);
    if (maxTokensValue !== null && (!Number.isInteger(maxTokensValue) || maxTokensValue < 1)) {
      return setClientErr("Max tokens override must be a positive whole number.");
    }
    if (
      temperatureValue !== null &&
      (!Number.isFinite(temperatureValue) || temperatureValue < 0 || temperatureValue > 2)
    ) {
      return setClientErr("Temperature override must be between 0 and 2.");
    }

    let parsedExtra: Record<string, unknown>;
    try {
      const parsed: unknown = extraApiParams.trim() ? JSON.parse(extraApiParams) : {};
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        return setClientErr("Extra API params must be a JSON object.");
      }
      parsedExtra = parsed as Record<string, unknown>;
    } catch {
      return setClientErr("Extra API params must be valid JSON.");
    }

    setClientErr(null);
    setServerErr(null);
    save.mutate({
      provider_id: providerId,
      model_id: modelId,
      api_model_id: apiModelId.trim(),
      input_cost_per_million: inputParsed.value,
      output_cost_per_million: outputParsed.value,
      fixed_cost_per_call_usd: fixedParsed.value,
      max_tokens_override: maxTokensValue,
      temperature_override: temperatureValue,
      supports_system_prompt: supportsSystemPrompt,
      supports_temperature: supportsTemperature,
      thinking_level_override:
        thinkingOverride === "inherit" ? null : thinkingOverride,
      thinking_strategy_override:
        thinkingStrategyOverride === "inherit" ? null : thinkingStrategyOverride,
      reasoning_effort: reasoningEffort,
      extra_api_params: parsedExtra,
      price_source_override: priceSourceOverride,
      price_source_model_id_override: emptyToNull(priceSourceModelOverride),
      is_enabled: enabled,
    });
  }

  return (
    <FormModal
      open
      title={mode === "create" ? "Create provider-model" : providerModel?.api_model_id}
      titleId={titleId}
      eyebrow={mode === "create" ? "New provider-model" : "Edit provider-model"}
      onClose={onClose}
      onSubmit={submit}
      noValidate
      actions={
        <>
          {mode === "edit" ? (
            <button
              type="button"
              className="btn btn--rust llm-registry-form__delete"
              onClick={() => remove.mutate()}
              disabled={remove.isPending || save.isPending}
            >
              {remove.isPending ? "Deleting…" : "Delete provider-model"}
            </button>
          ) : null}
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="btn btn--moss"
            disabled={save.isPending || remove.isPending}
          >
            {save.isPending
              ? "Saving…"
              : mode === "create"
                ? "Create provider-model"
                : "Save provider-model"}
          </button>
        </>
      }
    >
        <FormModalGrid>
          <SearchableSelect
            label="Provider"
            requirement="required"
            className="form-modal__field"
            value={providerId}
            options={providerOptions}
            onChange={setProviderId}
            required
            aria-invalid={clientErr === "Provider is required."}
            aria-describedby={errId}
          />
          <SearchableSelect
            label="Model"
            requirement="required"
            className="form-modal__field"
            value={modelId}
            options={modelOptions}
            onChange={setModelId}
            required
            aria-invalid={clientErr === "Model is required."}
            aria-describedby={errId}
          />
        </FormModalGrid>
        <FormModalField label="API model id" requirement="required">
          <input
            value={apiModelId}
            onChange={(e) => setApiModelId(e.target.value)}
            required
            aria-invalid={clientErr === "API model id is required."}
            aria-describedby={errId}
          />
        </FormModalField>
        <FormModalGrid>
          <FormModalField label="Input cost per 1M" requirement="required">
            <input
              type="number"
              min="0"
              step="0.0001"
              value={inputCost}
              onChange={(e) => setInputCost(e.target.value)}
              required
              aria-invalid={
                clientErr === "Input cost is required." ||
                clientErr === "Input cost must be zero or more."
              }
              aria-describedby={errId}
            />
          </FormModalField>
          <FormModalField label="Output cost per 1M" requirement="required">
            <input
              type="number"
              min="0"
              step="0.0001"
              value={outputCost}
              onChange={(e) => setOutputCost(e.target.value)}
              required
              aria-invalid={
                clientErr === "Output cost is required." ||
                clientErr === "Output cost must be zero or more."
              }
              aria-describedby={errId}
            />
          </FormModalField>
        </FormModalGrid>
        <FormModalGrid>
          <FormModalField label="Fixed cost per call" requirement="optional">
            <input
              type="number"
              min="0"
              step="0.0001"
              value={fixedCost}
              onChange={(e) => setFixedCost(e.target.value)}
              aria-invalid={clientErr === "Fixed cost must be zero or more."}
              aria-describedby={errId}
            />
          </FormModalField>
          <FormModalField
            label="Thinking level"
            requirement="optional"
            helpId={thinkingHelpId}
            helpText={
              thinkingOverride === "inherit"
                ? `Inherited model default: ${thinkingLevelLabel(
                    inheritedThinkingLevel,
                  )}. Effective: ${thinkingLevelLabel(effectiveThinkingLevel)}.`
                : undefined
            }
          >
            <select
              value={thinkingOverride}
              aria-describedby={describedBy(thinkingHelpId, errId)}
              onChange={(e) => {
                const next = e.target.value;
                if (next === "inherit" || isThinkingLevel(next)) {
                  setThinkingOverride(next);
                }
              }}
            >
              <option value="inherit">Model default</option>
              {THINKING_LEVEL_OPTIONS.map((level) => (
                <option key={level} value={level}>
                  {thinkingLevelLabel(level)}
                </option>
              ))}
            </select>
          </FormModalField>
        </FormModalGrid>
        <FormModalField
          label="Thinking strategy"
          requirement="optional"
          helpId={thinkingStrategyHelpId}
          helpText={
            thinkingStrategyOverride === "inherit"
              ? `Inherited model default: ${thinkingStrategyLabel(
                  inheritedThinkingStrategy,
                )}. Effective: ${thinkingStrategyLabel(effectiveThinkingStrategy)}.`
              : `Model default: ${thinkingStrategyLabel(
                  inheritedThinkingStrategy,
                )}. Effective: ${thinkingStrategyLabel(effectiveThinkingStrategy)}.`
          }
        >
          <select
            value={thinkingStrategyOverride}
            aria-describedby={describedBy(thinkingStrategyHelpId, errId)}
            onChange={(e) => {
              const next = e.target.value;
              if (next === "inherit" || isThinkingStrategy(next)) {
                setThinkingStrategyOverride(next);
              }
            }}
          >
            <option value="inherit">Model default</option>
            {THINKING_STRATEGY_OPTIONS.map((strategy) => (
              <option key={strategy} value={strategy}>
                {thinkingStrategyLabel(strategy)}
              </option>
            ))}
          </select>
        </FormModalField>
        <FormModalGrid>
          <FormModalField label="Max tokens override" requirement="optional">
            <input
              type="number"
              min="1"
              value={maxTokens}
              onChange={(e) => setMaxTokens(e.target.value)}
              aria-invalid={clientErr === "Max tokens override must be a positive whole number."}
              aria-describedby={errId}
            />
          </FormModalField>
          <FormModalField label="Temperature override" requirement="optional">
            <input
              type="number"
              min="0"
              max="2"
              step="0.1"
              value={temperature}
              onChange={(e) => setTemperature(e.target.value)}
              aria-invalid={clientErr === "Temperature override must be between 0 and 2."}
              aria-describedby={errId}
            />
          </FormModalField>
        </FormModalGrid>
        <FormModalGrid>
          <FormModalField label="Price source override" requirement="optional">
            <select
              value={priceSourceOverride}
              onChange={(e) =>
                setPriceSourceOverride(e.target.value as LlmPriceSourceOverride)
              }
            >
              <option value="">Use model default</option>
              <option value="openrouter">OpenRouter</option>
              <option value="none">Manual / pinned</option>
            </select>
          </FormModalField>
          <FormModalField label="Price source model override" requirement="optional">
            <input
              value={priceSourceModelOverride}
              onChange={(e) => setPriceSourceModelOverride(e.target.value)}
            />
          </FormModalField>
        </FormModalGrid>
        <fieldset className="llm-registry-form__fieldset">
          <legend>Provider support overrides</legend>
          <div className="llm-registry-form__checks">
            <label className="llm-registry-form__check">
              <input
                type="checkbox"
                checked={supportsSystemPrompt}
                onChange={(e) => setSupportsSystemPrompt(e.target.checked)}
              />
              <span>System prompt</span>
            </label>
            <label className="llm-registry-form__check">
              <input
                type="checkbox"
                checked={supportsTemperature}
                onChange={(e) => setSupportsTemperature(e.target.checked)}
              />
              <span>Temperature</span>
            </label>
            <label className="llm-registry-form__check">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
              />
              <span>Enabled</span>
            </label>
          </div>
        </fieldset>
        <FormModalField
          label="Extra API params"
          requirement="optional"
          helpId={extraHelpId}
          helpText="JSON object merged into provider requests for this row."
        >
          <AutoGrowTextarea
            className="mono"
            value={extraApiParams}
            onChange={(e) => setExtraApiParams(e.target.value)}
            rows={4}
            aria-invalid={
              clientErr === "Extra API params must be valid JSON." ||
              clientErr === "Extra API params must be a JSON object."
            }
            aria-describedby={describedBy(extraHelpId, errId)}
          />
        </FormModalField>
        {err ? (
          <p id="llm-provider-model-error" className="form-error" role="alert">
            {err}
          </p>
        ) : null}
        {mode === "edit" && providerModel ? (
          <ProviderModelPlayground
            providerModel={providerModel}
            model={persistedModel}
            assignments={assignments.filter(
              (assignment) =>
                assignment.provider_model_id === providerModel.id &&
                assignment.is_enabled,
            )}
          />
        ) : null}
    </FormModal>
  );
}

function ProviderModelPlayground({
  providerModel,
  model,
  assignments,
}: {
  providerModel: LlmProviderModel;
  model: LlmModel | undefined;
  assignments: LlmAssignment[];
}) {
  const [mode, setMode] = useState<LlmProviderModelPlaygroundMode>("direct");
  const [assignmentId, setAssignmentId] = useState(assignments[0]?.id ?? "");
  const [prompt, setPrompt] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [maxTokens, setMaxTokens] = useState(
    providerModel.max_tokens_override == null
      ? ""
      : String(providerModel.max_tokens_override),
  );
  const [temperature, setTemperature] = useState(
    providerModel.temperature_override == null
      ? ""
      : String(providerModel.temperature_override),
  );
  const [imageUrl, setImageUrl] = useState("");
  const [clientErr, setClientErr] = useState<string | null>(null);
  const [result, setResult] =
    useState<LlmProviderModelPlaygroundResponse | null>(null);

  const hasAssignments = assignments.length > 0;
  const supportsVision = model?.capabilities.includes("vision") ?? false;
  const supportsSystemPrompt = providerModel.supports_system_prompt;
  const supportsTemperature = providerModel.supports_temperature;
  const selectedAssignment = assignments.find((item) => item.id === assignmentId);
  const resultId = "llm-provider-model-playground-result";
  const errId = clientErr ? "llm-provider-model-playground-error" : undefined;

  const playground = useMutation({
    mutationFn: (body: LlmProviderModelPlaygroundRequest) =>
      fetchJson<LlmProviderModelPlaygroundResponse>(
        `/admin/api/v1/llm/provider-models/${providerModel.id}/playground`,
        { method: "POST", body },
      ),
    onSuccess: (response) => {
      setClientErr(null);
      setResult(response);
    },
    onError: (error: Error) => {
      setResult(null);
      setClientErr(playgroundErrorCopy(error));
    },
  });

  function runPlayground(): void {
    if (!prompt.trim()) {
      setResult(null);
      setClientErr("Prompt is required.");
      return;
    }
    const parsedMaxTokens = optionalPositiveInteger(maxTokens, "Max tokens");
    if (!parsedMaxTokens.ok) {
      setResult(null);
      setClientErr(parsedMaxTokens.error);
      return;
    }
    const parsedTemperature = optionalTemperature(temperature);
    if (!parsedTemperature.ok) {
      setResult(null);
      setClientErr(parsedTemperature.error);
      return;
    }
    const assignmentMode = mode === "assignment" && hasAssignments;
    if (assignmentMode && !assignmentId) {
      setResult(null);
      setClientErr("Assignment is required.");
      return;
    }

    setClientErr(null);
    playground.mutate({
      mode: assignmentMode ? "assignment" : "direct",
      prompt: prompt.trim(),
      system_prompt: supportsSystemPrompt ? emptyToNull(systemPrompt) : null,
      max_tokens: parsedMaxTokens.value,
      temperature: supportsTemperature ? parsedTemperature.value : null,
      image_url: supportsVision ? emptyToNull(imageUrl) : null,
      assignment_id: assignmentMode ? assignmentId : null,
    });
  }

  function resetPlayground(): void {
    setMode("direct");
    setAssignmentId(assignments[0]?.id ?? "");
    setPrompt("");
    setSystemPrompt("");
    setMaxTokens(
      providerModel.max_tokens_override == null
        ? ""
        : String(providerModel.max_tokens_override),
    );
    setTemperature(
      providerModel.temperature_override == null
        ? ""
        : String(providerModel.temperature_override),
    );
    setImageUrl("");
    setClientErr(null);
    setResult(null);
    playground.reset();
  }

  function preventAccidentalSave(event: KeyboardEvent<HTMLElement>): void {
    const target = event.target;
    if (
      event.key === "Enter" &&
      target instanceof HTMLElement &&
      !(target instanceof HTMLTextAreaElement) &&
      !(target instanceof HTMLButtonElement)
    ) {
      event.preventDefault();
    }
  }

  return (
    <section
      className="llm-playground"
      aria-labelledby="llm-provider-model-playground-title"
      onKeyDownCapture={preventAccidentalSave}
    >
      <header className="llm-playground__head">
        <div>
          <h4 id="llm-provider-model-playground-title">Playground</h4>
          <p>Run a stateless smoke test against this provider-model.</p>
        </div>
        <span className="llm-playground__target mono">{providerModel.api_model_id}</span>
      </header>

      {hasAssignments ? (
        <div className="llm-playground__mode" aria-label="Playground call mode">
          <button
            type="button"
            className={
              "llm-playground__mode-button" +
              (mode === "direct" ? " llm-playground__mode-button--active" : "")
            }
            aria-pressed={mode === "direct"}
            onClick={() => setMode("direct")}
          >
            Direct call
          </button>
          <button
            type="button"
            className={
              "llm-playground__mode-button" +
              (mode === "assignment" ? " llm-playground__mode-button--active" : "")
            }
            aria-pressed={mode === "assignment"}
            onClick={() => {
              setMode("assignment");
              setAssignmentId((current) => current || assignments[0]?.id || "");
            }}
          >
            Via assignment
          </button>
        </div>
      ) : null}

      {mode === "assignment" && hasAssignments ? (
        <FormModalField label="Assignment" requirement="required">
          <select
            value={assignmentId}
            onChange={(event) => setAssignmentId(event.target.value)}
            aria-describedby={errId}
          >
            {assignments.map((assignment) => (
              <option key={assignment.id} value={assignment.id}>
                {assignment.capability} priority {assignment.priority}
              </option>
            ))}
          </select>
        </FormModalField>
      ) : null}

      <FormModalField label="Prompt" requirement="required">
        <AutoGrowTextarea
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          rows={4}
          required
          aria-invalid={clientErr === "Prompt is required."}
          aria-describedby={describedBy(errId, result ? resultId : undefined)}
        />
      </FormModalField>

      <FormModalField
        label="System prompt"
        requirement="optional"
        helpId={!supportsSystemPrompt ? "llm-playground-system-help" : undefined}
        helpText={!supportsSystemPrompt ? "Disabled for this provider-model." : undefined}
      >
        <AutoGrowTextarea
          value={systemPrompt}
          onChange={(event) => setSystemPrompt(event.target.value)}
          rows={3}
          disabled={!supportsSystemPrompt}
          aria-describedby={!supportsSystemPrompt ? "llm-playground-system-help" : undefined}
        />
      </FormModalField>

      <FormModalGrid>
        <FormModalField label="Max tokens" requirement="optional">
          <input
            type="number"
            min="1"
            value={maxTokens}
            onChange={(event) => setMaxTokens(event.target.value)}
            aria-invalid={
              clientErr === "Max tokens must be a positive whole number."
            }
            aria-describedby={errId}
          />
        </FormModalField>
        <FormModalField
          label="Temperature"
          requirement="optional"
          helpId={
            !supportsTemperature ? "llm-playground-temperature-help" : undefined
          }
          helpText={
            !supportsTemperature ? "Disabled for this provider-model." : undefined
          }
        >
          <input
            type="number"
            min="0"
            max="2"
            step="0.1"
            value={temperature}
            onChange={(event) => setTemperature(event.target.value)}
            disabled={!supportsTemperature}
            aria-invalid={clientErr === "Temperature must be between 0 and 2."}
            aria-describedby={describedBy(
              !supportsTemperature ? "llm-playground-temperature-help" : undefined,
              errId,
            )}
          />
        </FormModalField>
      </FormModalGrid>

      {supportsVision ? (
        <FormModalField label="Image URL or data URL" requirement="optional">
          <AutoGrowTextarea
            value={imageUrl}
            onChange={(event) => setImageUrl(event.target.value)}
            rows={2}
          />
        </FormModalField>
      ) : null}

      {clientErr ? (
        <p id="llm-provider-model-playground-error" className="form-error" role="alert">
          {clientErr}
        </p>
      ) : null}

      {playground.isPending ? (
        <p className="llm-playground__status" role="status">
          Running playground test...
        </p>
      ) : null}

      {result ? (
        <PlaygroundResult
          id={resultId}
          result={result}
          assignment={result.assignment_id ? selectedAssignment : undefined}
        />
      ) : null}

      <div className="llm-playground__actions">
        <button
          type="button"
          className="btn btn--moss"
          onClick={runPlayground}
          disabled={playground.isPending}
        >
          {playground.isPending ? "Running..." : "Run playground"}
        </button>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => {
            setClientErr(null);
            setResult(null);
            playground.reset();
          }}
          disabled={playground.isPending || (!result && !clientErr)}
        >
          Clear result
        </button>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={resetPlayground}
          disabled={playground.isPending}
        >
          Reset playground
        </button>
      </div>
    </section>
  );
}

function PlaygroundResult({
  id,
  result,
  assignment,
}: {
  id: string;
  result: LlmProviderModelPlaygroundResponse;
  assignment: LlmAssignment | undefined;
}) {
  const output =
    result.status === "ok"
      ? result.assistant_text || "(empty response)"
      : result.error_message || "Provider returned an error.";
  const diagnostics = [
    ["Provider", result.provider_used],
    ["Model", result.model_used],
    ["Latency", formatNullable(result.latency_ms, " ms")],
    [
      "Tokens",
      `${formatNullable(result.input_tokens)} in / ${formatNullable(
        result.output_tokens,
      )} out`,
    ],
    ["Reasoning tokens", formatNullable(result.reasoning_tokens)],
    ["Cost", formatPlaygroundCost(result.cost_usd)],
    ["Stop reason", formatNullable(result.stop_reason ?? result.finish_reason)],
    [
      "Assignment",
      assignment
        ? `${assignment.capability} priority ${assignment.priority}`
        : result.assignment_id ?? "Direct",
    ],
  ];

  return (
    <section
      id={id}
      className={
        "llm-playground-result" +
        (result.status === "error" ? " llm-playground-result--error" : "")
      }
      role="status"
      aria-live="polite"
    >
      <header className="llm-playground-result__head">
        <strong>{result.status === "ok" ? "Success" : "Failure"}</strong>
      </header>
      <pre className="llm-playground-result__output">{output}</pre>
      {result.reasoning_text ? (
        <details className="llm-playground-result__reasoning">
          <summary>Reasoning</summary>
          <pre>{result.reasoning_text}</pre>
        </details>
      ) : null}
      <dl className="llm-playground-result__diagnostics">
        {diagnostics.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd className="mono">{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
