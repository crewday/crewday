import { useId, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import FormModal, {
  FormModalField,
  FormModalGrid,
} from "@/components/FormModal";
import SearchableSelect, { type SearchableSelectOption } from "@/components/SearchableSelect";
import { ApiError, fetchJson } from "@/lib/api";
import { formatDecimal, formatInteger } from "@/lib/numberFormat";
import { qk } from "@/lib/queryKeys";
import type {
  LlmAudioInputTransform,
  LlmImageInputFormat,
  LlmModel,
  LlmPriceSource,
  LlmPriceSourceOverride,
  LlmProvider,
  LlmProviderModel,
  LlmProviderType,
  LlmThinkingLevel,
  LlmThinkingStrategy,
} from "@/types";
import LlmPlayground from "./LlmPlayground";
import LlmEmbeddingSmoke from "./LlmEmbeddingSmoke";
import type { LlmIndexes } from "./lib/llmIndexes";
import {
  THINKING_LEVEL_OPTIONS,
  THINKING_STRATEGY_OPTIONS,
  isThinkingLevel,
  isThinkingStrategy,
  thinkingLevelLabel,
  thinkingStrategyLabel,
} from "./lib/llmThinking";
import RegistryCheckPill from "./RegistryCheckPill";

export type RegistryDialogState =
  | { kind: "provider"; mode: "create" }
  | { kind: "provider"; mode: "edit"; id: string }
  | { kind: "model"; mode: "create" }
  | { kind: "model"; mode: "edit"; id: string }
  | { kind: "providerModel"; mode: "create" }
  | {
      kind: "providerModel";
      mode: "edit";
      id: string;
      providerModel?: LlmProviderModel;
    };

interface RegistryModalsProps {
  dialog: RegistryDialogState | null;
  providers: LlmProvider[];
  models: LlmModel[];
  providerModels: LlmProviderModel[];
  indexes: LlmIndexes;
  onClose: () => void;
  onOpenProviderModel: (providerModel: LlmProviderModel) => void;
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
  titleId?: string;
  onClose: () => void;
}

interface ProviderPayload {
  name: string;
  provider_type: LlmProviderType;
  api_endpoint: string | null;
  default_model: string | null;
  timeout_s: number;
  requests_per_minute: number;
  is_enabled: boolean;
}

interface ModelPayload {
  canonical_name: string;
  display_name: string;
  capabilities: string[];
  context_window: number | null;
  max_output_tokens: number | null;
  embedding_dimensions: number | null;
  temperature: number | null;
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
  inputCostPerMillion: number | null;
  outputCostPerMillion: number | null;
  fixedCostPerCallUsd: number | null;
  audioCostPerHourUsd: number | null;
}

interface ProviderModelPayload {
  provider_id: string;
  model_id: string;
  api_model_id: string;
  input_cost_per_million: number | null;
  output_cost_per_million: number | null;
  fixed_cost_per_call_usd: number | null;
  audio_cost_per_hour_usd: number | null;
  audio_input_transform: LlmAudioInputTransform;
  image_input_format: LlmImageInputFormat;
  image_input_max_edge_px: number | null;
  max_tokens_override: number | null;
  supports_system_prompt: boolean;
  supports_temperature: boolean;
  thinking_strategy_override: LlmThinkingStrategy | null;
  extra_api_params: Record<string, unknown>;
  price_source_override: LlmPriceSourceOverride;
  price_source_model_id_override: string | null;
  is_enabled: boolean;
}

interface LlmProviderModelSyncPricingResponse {
  provider_model: LlmProviderModel;
  pricing_sync_result: {
    status: "updated" | "unchanged" | "skipped_not_syncable" | "error";
  };
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

const GENERATIVE_CAPABILITY_TAGS = new Set<string>([
  "chat",
  "vision",
  "audio_input",
  "reasoning",
  "function_calling",
  "json_mode",
  "streaming",
]);

const AUDIO_INPUT_TRANSFORM_OPTIONS: {
  value: LlmAudioInputTransform;
  label: string;
}[] = [
  { value: "passthrough", label: "Passthrough" },
  { value: "wav_16khz_mono", label: "WAV, 16 kHz mono" },
];

const IMAGE_INPUT_FORMAT_OPTIONS: {
  value: LlmImageInputFormat;
  label: string;
}[] = [
  { value: "preserve", label: "Preserve" },
  { value: "jpeg", label: "JPEG" },
  { value: "png", label: "PNG" },
  { value: "webp", label: "WEBP" },
];

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

function modelHasCapability(
  model: LlmModel | undefined,
  capability: string,
): boolean {
  return model?.capabilities.includes(capability) ?? false;
}

function capabilitiesInclude(capabilities: string[], capability: string): boolean {
  return capabilities.includes(capability);
}

function capabilitiesSupportGeneration(capabilities: string[]): boolean {
  return capabilities.some((capability) => GENERATIVE_CAPABILITY_TAGS.has(capability));
}

function modelSupportsGeneration(model: LlmModel | undefined): boolean {
  return model ? capabilitiesSupportGeneration(model.capabilities) : false;
}

function isAudioInputTransform(value: string): value is LlmAudioInputTransform {
  return value === "passthrough" || value === "wav_16khz_mono";
}

function isImageInputFormat(value: string): value is LlmImageInputFormat {
  return (
    value === "preserve" || value === "jpeg" || value === "png" || value === "webp"
  );
}

function persistedMediaPayload(
  providerModel: LlmProviderModel,
  model: LlmModel | undefined,
): Pick<
  ProviderModelPayload,
  "audio_input_transform" | "image_input_format" | "image_input_max_edge_px"
> {
  return {
    audio_input_transform: modelHasCapability(model, "audio_input")
      ? providerModel.audio_input_transform
      : "passthrough",
    image_input_format: modelHasCapability(model, "vision")
      ? providerModel.image_input_format
      : "preserve",
    image_input_max_edge_px: modelHasCapability(model, "vision")
      ? providerModel.image_input_max_edge_px
      : null,
  };
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

function redactedCopy(copy: string, values: (string | null | undefined)[]): string {
  const uniqueValues = [
    ...new Set(values.filter((value): value is string => Boolean(value))),
  ];
  return uniqueValues.reduce((current, value) => {
    if (!value) return current;
    return current.split(value).join("[redacted]");
  }, copy);
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

function formatCostPerMillion(value: number): string {
  return `$${formatDecimal(value, {
    minimumFractionDigits: value === 0 ? 0 : 2,
    maximumFractionDigits: value < 0.01 && value > 0 ? 6 : 2,
  })}/M`;
}

function formatOptionalCostPerMillion(value: number | null): string {
  return formatCostPerMillion(value ?? 0);
}

function formatUsdAmount(value: number): string {
  return `$${formatDecimal(value, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 6,
  })}`;
}

function providerTypeLabel(providerType: LlmProviderType): string {
  if (providerType === "openrouter") return "OpenRouter provider";
  if (providerType === "openai_compatible") return "OpenAI compatible";
  if (providerType === "ollama") return "Ollama";
  if (providerType === "local_embedding") return "Local embedding";
  return "Fake";
}

function providerOption(provider: LlmProvider): SearchableSelectOption {
  return {
    value: provider.id,
    label: provider.name,
    secondaryText: providerTypeLabel(provider.provider_type),
    searchText: [
      provider.name,
      provider.provider_type,
      provider.endpoint ?? "",
      provider.id,
    ].join(" "),
  };
}

function providerAllowsApiKey(providerType: LlmProviderType): boolean {
  return providerType !== "fake" && providerType !== "local_embedding";
}

function modelSupportsPlayground(model: LlmModel | undefined): boolean {
  return Boolean(
    model?.capabilities.includes("chat") ||
      model?.capabilities.includes("audio_input"),
  );
}

function modelSupportsEmbeddings(model: LlmModel | undefined): boolean {
  return Boolean(model?.capabilities.includes("embeddings"));
}

function modelOption(model: LlmModel): SearchableSelectOption {
  const capabilities = model.capabilities.join(", ");
  return {
    value: model.id,
    label: model.display_name,
    secondaryText: [model.canonical_name, capabilities].filter(Boolean).join(" - "),
    searchText: [
      model.display_name,
      model.canonical_name,
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
  const {
    dialog,
    providers,
    models,
    providerModels,
    indexes,
    onClose,
    onOpenProviderModel,
  } = props;

  const titleId = dialog ? `llm-${dialog.kind}-${dialog.mode}-title` : undefined;

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
          providers={providers}
          providerModels={providerModels}
          titleId={titleId}
          onClose={onClose}
          onOpenProviderModel={onOpenProviderModel}
        />
      ) : null}
      {dialog?.kind === "providerModel" ? (
        <ProviderModelForm
          mode={dialog.mode}
          providerModel={
            dialog.mode === "edit"
              ? indexes.pmById.get(dialog.id) ?? dialog.providerModel
              : undefined
          }
          providers={providers}
          models={models}
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
  const [apiKey, setApiKey] = useState("");
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
  function providerErrorCopy(error: Error, fallback: string): string {
    return redactedCopy(apiErrorCopy(error, fallback), [
      apiKey,
      apiKey.trim(),
      provider?.api_key_ref,
    ]);
  }

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
    onError: (error: Error) =>
      setServerErr(providerErrorCopy(error, "Provider save failed.")),
  });
  const setKey = useMutation({
    mutationFn: (key: string) =>
      fetchJson<LlmProvider>(`/admin/api/v1/llm/providers/${provider?.id}/key`, {
        method: "PUT",
        body: { api_key: key },
      }),
    onSuccess: async () => {
      setApiKey("");
      await invalidate();
    },
    onError: (error: Error) =>
      setServerErr(providerErrorCopy(error, "Provider key update failed.")),
  });
  const clearKey = useMutation({
    mutationFn: () =>
      fetchJson<LlmProvider>(`/admin/api/v1/llm/providers/${provider?.id}/key`, {
        method: "DELETE",
      }),
    onSuccess: async () => {
      setApiKey("");
      await invalidate();
    },
    onError: (error: Error) =>
      setServerErr(providerErrorCopy(error, "Provider key clear failed.")),
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
      setServerErr(providerErrorCopy(error, "Provider delete failed.")),
  });

  const err = clientErr ?? serverErr;
  const errId = err ? "llm-provider-error" : undefined;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const timeoutValue = Number(timeout);
    const rpmValue = Number(rpm);
    if (!name.trim()) return setClientErr("Name is required.");
    if (
      (providerType === "openai_compatible" || providerType === "ollama") &&
      !apiEndpoint.trim()
    ) {
      return setClientErr(`${providerTypeLabel(providerType)} providers need an API endpoint.`);
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
      default_model: emptyToNull(defaultModel),
      timeout_s: timeoutValue,
      requests_per_minute: rpmValue,
      is_enabled: enabled,
    });
  }

  function submitKey() {
    const key = apiKey.trim();
    if (!key) return setClientErr("API key is required.");
    setClientErr(null);
    setServerErr(null);
    setKey.mutate(key);
  }

  function keyStatusCopy(): string {
    if (provider?.api_key_status === "present") return "Present";
    if (provider?.api_key_status === "rotating") return "Rotating";
    return "Missing";
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
              disabled={
                remove.isPending ||
                save.isPending ||
                setKey.isPending ||
                clearKey.isPending
              }
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
            disabled={
              save.isPending ||
              remove.isPending ||
              setKey.isPending ||
              clearKey.isPending
            }
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
              <option value="ollama">Ollama</option>
              <option value="local_embedding">Local embedding</option>
              <option value="fake">Fake</option>
            </select>
          </FormModalField>
          <FormModalField label="API endpoint" requirement="optional">
            <input
              value={apiEndpoint}
              onChange={(e) => setApiEndpoint(e.target.value)}
              aria-invalid={
                clientErr ===
                `${providerTypeLabel(providerType)} providers need an API endpoint.`
              }
              aria-describedby={errId}
            />
          </FormModalField>
        </FormModalGrid>
        <FormModalField label="Enabled" requirement="required">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
        </FormModalField>
        {mode === "edit" && provider && providerAllowsApiKey(provider.provider_type) ? (
          <div className="llm-provider-key">
            <div className="llm-provider-key__head">
              <span className="llm-provider-key__label">API key</span>
              <span
                className={`llm-provider-key__status llm-provider-key__status--${
                  provider?.api_key_status ?? "missing"
                }`}
              >
                {keyStatusCopy()}
              </span>
            </div>
            <div className="llm-provider-key__control">
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={
                  provider?.api_key_status === "present"
                    ? "Paste replacement key"
                    : "Paste API key"
                }
                autoComplete="new-password"
                aria-label="API key"
                aria-invalid={clientErr === "API key is required."}
                aria-describedby={errId}
              />
              <button
                type="button"
                className="btn btn--ghost llm-provider-key__button"
                onClick={submitKey}
                disabled={setKey.isPending || clearKey.isPending}
              >
                {setKey.isPending
                  ? "Saving key…"
                  : provider?.api_key_status === "present"
                    ? "Rotate key"
                    : "Set key"}
              </button>
              {provider?.api_key_status === "present" ? (
                <button
                  type="button"
                  className="btn btn--rust llm-provider-key__button"
                  onClick={() => {
                    setClientErr(null);
                    setServerErr(null);
                    clearKey.mutate();
                  }}
                  disabled={setKey.isPending || clearKey.isPending}
                >
                  {clearKey.isPending ? "Clearing…" : "Clear key"}
                </button>
              ) : null}
            </div>
          </div>
        ) : null}
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
  providers,
  providerModels,
  titleId,
  onClose,
  onOpenProviderModel,
}: {
  mode: "create" | "edit";
  model?: LlmModel;
  providers: LlmProvider[];
  providerModels: LlmProviderModel[];
  titleId?: string;
  onClose: () => void;
  onOpenProviderModel: (providerModel: LlmProviderModel) => void;
}) {
  const qc = useQueryClient();
  const openRouterInputId = useId();
  const [canonicalName, setCanonicalName] = useState(model?.canonical_name ?? "");
  const [displayName, setDisplayName] = useState(model?.display_name ?? "");
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
  const [embeddingDimensions, setEmbeddingDimensions] = useState(
    model?.embedding_dimensions === null || model?.embedding_dimensions === undefined
      ? ""
      : String(model.embedding_dimensions),
  );
  const [temperature, setTemperature] = useState(
    model?.temperature === null || model?.temperature === undefined
      ? ""
      : String(model.temperature),
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
  const [openRouterModel, setOpenRouterModel] = useState(
    mode === "edit" && model
      ? (model.price_source_model_id ?? model.canonical_name)
      : "",
  );
  const [openRouterErr, setOpenRouterErr] = useState<string | null>(null);
  const [openRouterStatus, setOpenRouterStatus] = useState<string | null>(null);
  const [openRouterPricing, setOpenRouterPricing] =
    useState<OpenRouterPricingPreview | null>(null);
  const [clientErr, setClientErr] = useState<string | null>(null);
  const [serverErr, setServerErr] = useState<string | null>(null);
  const [creatingProviderId, setCreatingProviderId] = useState<string | null>(null);
  const supportsEmbeddings = capabilitiesInclude(capabilities, "embeddings");
  const supportsReasoning = capabilitiesInclude(capabilities, "reasoning");
  const supportsGeneration = capabilitiesSupportGeneration(capabilities);

  const modelProviderRows = useMemo(() => {
    if (mode !== "edit" || !model) return [];
    const providerModelByProviderId = new Map(
      providerModels
        .filter((providerModel) => providerModel.model_id === model.id)
        .map((providerModel) => [providerModel.provider_id, providerModel] as const),
    );
    return providers
      .map((provider) => ({
        provider,
        providerModel: providerModelByProviderId.get(provider.id) ?? null,
      }))
      .sort((left, right) => {
        const byName = left.provider.name.localeCompare(right.provider.name);
        return byName || left.provider.id.localeCompare(right.provider.id);
      });
  }, [mode, model, providerModels, providers]);

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
  const createProviderModel = useMutation({
    mutationFn: (provider: LlmProvider) => {
      if (!model) throw new Error("Model is required.");
      const body: ProviderModelPayload = {
        provider_id: provider.id,
        model_id: model.id,
        api_model_id: model.price_source_model_id ?? model.canonical_name,
        input_cost_per_million: null,
        output_cost_per_million: null,
        fixed_cost_per_call_usd: null,
        audio_cost_per_hour_usd: null,
        audio_input_transform: "passthrough",
        image_input_format: "preserve",
        image_input_max_edge_px: null,
        max_tokens_override: null,
        supports_system_prompt: true,
        supports_temperature: true,
        thinking_strategy_override: null,
        extra_api_params: {},
        price_source_override: "",
        price_source_model_id_override: null,
        is_enabled: provider.is_enabled,
      };
      return fetchJson<LlmProviderModel>("/admin/api/v1/llm/provider-models", {
        method: "POST",
        body,
      });
    },
    onMutate: (provider) => {
      setCreatingProviderId(provider.id);
      setClientErr(null);
      setServerErr(null);
    },
    onSuccess: async (providerModel) => {
      await invalidate();
      onOpenProviderModel(providerModel);
    },
    onError: (error: Error) =>
      setServerErr(apiErrorCopy(error, "Provider-model create failed.")),
    onSettled: () => setCreatingProviderId(null),
  });
  const openRouterPreview = useMutation({
    mutationFn: (modelIdOrUrl: string) =>
      fetchJson<OpenRouterModelPreviewResponse>(
        "/admin/api/v1/llm/models/openrouter-preview",
        { method: "POST", body: { model_id_or_url: modelIdOrUrl } },
      ),
    onSuccess: (preview) => {
      applyModelPayload(preview.model_payload);
      void invalidate();
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
              audioCostPerHourUsd: firstPreview.payload.audio_cost_per_hour_usd,
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
  const openRouterLoader = (
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
          <span className="llm-openrouter-loader__pricing-main">
            Provider price preview: {openRouterPricing.providerName}{" "}
            {openRouterPricing.audioCostPerHourUsd !== null &&
            openRouterPricing.audioCostPerHourUsd > 0
              ? `${formatUsdAmount(openRouterPricing.audioCostPerHourUsd)}/audio hour`
              : `${formatOptionalCostPerMillion(openRouterPricing.inputCostPerMillion)} in, ${formatOptionalCostPerMillion(openRouterPricing.outputCostPerMillion)} out`}
          </span>
          {openRouterPricing.audioCostPerHourUsd !== null &&
          openRouterPricing.audioCostPerHourUsd > 0 &&
          ((openRouterPricing.inputCostPerMillion ?? 0) > 0 ||
            (openRouterPricing.outputCostPerMillion ?? 0) > 0) ? (
            <span className="llm-openrouter-loader__pricing-detail">
              Token pricing:{" "}
              {formatOptionalCostPerMillion(openRouterPricing.inputCostPerMillion)} in,{" "}
              {formatOptionalCostPerMillion(openRouterPricing.outputCostPerMillion)} out
            </span>
          ) : null}
          {openRouterPricing.fixedCostPerCallUsd !== null ? (
            <span className="llm-openrouter-loader__pricing-detail">
              Fixed cost: {formatUsdAmount(openRouterPricing.fixedCostPerCallUsd)}
            </span>
          ) : null}
          {openRouterPricing.providerCount > 1
            ? ` Across ${formatInteger(openRouterPricing.providerCount)} OpenRouter providers`
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
  );

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
    setEmbeddingDimensions(
      payload.embedding_dimensions === null || payload.embedding_dimensions === undefined
        ? ""
        : String(payload.embedding_dimensions),
    );
    setTemperature(
      payload.temperature === null || payload.temperature === undefined
        ? ""
        : String(payload.temperature),
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
    const outputValue = supportsGeneration ? numberOrNull(maxOutput) : null;
    const embeddingDimensionsValue = supportsEmbeddings
      ? numberOrNull(embeddingDimensions)
      : null;
    const temperatureValue = supportsGeneration ? numberOrNull(temperature) : null;
    if (!canonicalName.trim()) return setClientErr("Canonical name is required.");
    if (!displayName.trim()) return setClientErr("Display name is required.");
    if (contextValue !== null && (!Number.isInteger(contextValue) || contextValue < 1)) {
      return setClientErr("Context window must be a positive whole number.");
    }
    if (outputValue !== null && (!Number.isInteger(outputValue) || outputValue < 1)) {
      return setClientErr("Max output tokens must be a positive whole number.");
    }
    if (
      embeddingDimensionsValue !== null &&
      (!Number.isInteger(embeddingDimensionsValue) || embeddingDimensionsValue < 1)
    ) {
      return setClientErr("Embedding dimensions must be a positive whole number.");
    }
    if (
      temperatureValue !== null &&
      (!Number.isFinite(temperatureValue) || temperatureValue < 0 || temperatureValue > 2)
    ) {
      return setClientErr("Temperature must be between 0 and 2.");
    }
    setClientErr(null);
    setServerErr(null);
    save.mutate({
      canonical_name: canonicalName.trim(),
      display_name: displayName.trim(),
      capabilities,
      context_window: contextValue,
      max_output_tokens: outputValue,
      embedding_dimensions: embeddingDimensionsValue,
      temperature: temperatureValue,
      thinking_level: supportsReasoning ? thinkingLevel : "disabled",
      thinking_strategy: supportsReasoning ? thinkingStrategy : "none",
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
        {mode === "create" ? openRouterLoader : null}
        <FormModalGrid>
          <FormModalField label="Canonical name" requirement="required">
            <input
              value={canonicalName}
              onChange={(e) => setCanonicalName(e.target.value)}
              required
              aria-invalid={clientErr === "Canonical name is required."}
              aria-describedby={errId}
            />
          </FormModalField>
          <FormModalField label="Display name" requirement="required">
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              required
              aria-invalid={clientErr === "Display name is required."}
              aria-describedby={errId}
            />
          </FormModalField>
        </FormModalGrid>
        <fieldset className="llm-registry-form__fieldset">
          <legend>Capabilities</legend>
          <div className="llm-registry-form__checks">
            {CAPABILITY_TAGS.map((tag) => (
              <RegistryCheckPill
                key={tag}
                checked={capabilities.includes(tag)}
                onChange={() => toggleCapability(tag)}
              >
                {tag}
              </RegistryCheckPill>
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
          {supportsGeneration ? (
            <FormModalField label="Max output tokens" requirement="optional">
              <input
                type="number"
                min="1"
                value={maxOutput}
                onChange={(e) => setMaxOutput(e.target.value)}
                aria-invalid={
                  clientErr === "Max output tokens must be a positive whole number."
                }
                aria-describedby={errId}
              />
            </FormModalField>
          ) : null}
        </FormModalGrid>
        {supportsEmbeddings || supportsGeneration ? (
          <FormModalGrid>
            {supportsEmbeddings ? (
              <FormModalField label="Embedding dimensions" requirement="optional">
                <input
                  type="number"
                  min="1"
                  value={embeddingDimensions}
                  onChange={(e) => setEmbeddingDimensions(e.target.value)}
                  aria-invalid={
                    clientErr ===
                    "Embedding dimensions must be a positive whole number."
                  }
                  aria-describedby={errId}
                />
              </FormModalField>
            ) : null}
            {supportsGeneration ? (
              <FormModalField label="Temperature" requirement="optional">
                <input
                  type="number"
                  min="0"
                  max="2"
                  step="0.1"
                  value={temperature}
                  onChange={(e) => setTemperature(e.target.value)}
                  aria-invalid={clientErr === "Temperature must be between 0 and 2."}
                  aria-describedby={errId}
                />
              </FormModalField>
            ) : null}
          </FormModalGrid>
        ) : null}
        {supportsReasoning ? (
          <FormModalGrid>
            <FormModalField label="Thinking strategy" requirement="optional">
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
            <FormModalField label="Thinking level" requirement="optional">
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
          </FormModalGrid>
        ) : null}
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
        <FormModalField label="Active" requirement="optional">
          <input
            type="checkbox"
            checked={active}
            onChange={(e) => setActive(e.target.checked)}
          />
        </FormModalField>
        <FormModalField label="Notes" requirement="optional">
          <AutoGrowTextarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={3} />
        </FormModalField>
        {mode === "edit" && model ? (
          <section
            className="llm-model-providers"
            aria-labelledby="llm-model-providers-title"
          >
            <header className="llm-model-providers__head">
              <div>
                <h3 id="llm-model-providers-title">Providers</h3>
              </div>
            </header>
            <div className="llm-model-providers__list">
              {modelProviderRows.map(({ provider, providerModel }) => {
                const pending = creatingProviderId === provider.id;
                const providerCopy = (
                  <span className="llm-model-providers__provider">
                    <span className="llm-model-providers__name">{provider.name}</span>
                    <span className="llm-model-providers__meta">
                      {providerTypeLabel(provider.provider_type)}
                      {provider.is_enabled ? "" : " / disabled"}
                    </span>
                  </span>
                );
                return (
                  <div key={provider.id} className="llm-model-providers__item">
                    {providerModel ? (
                      <button
                        type="button"
                        className="llm-model-providers__button"
                        aria-label={`Edit provider-model for ${provider.name}`}
                        onClick={() => onOpenProviderModel(providerModel)}
                      >
                        {providerCopy}
                      </button>
                    ) : (
                      <>
                        {providerCopy}
                        <button
                          type="button"
                          className="btn btn--ghost btn--sm"
                          aria-label={`Create provider-model for ${provider.name}`}
                          onClick={() => createProviderModel.mutate(provider)}
                          disabled={
                            save.isPending ||
                            remove.isPending ||
                            createProviderModel.isPending
                          }
                        >
                          {pending ? "..." : "+"}
                        </button>
                      </>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        ) : null}
        {mode === "edit" ? openRouterLoader : null}
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
  const { mode, providerModel, providers, models, titleId, onClose } = props;
  const qc = useQueryClient();
  const priceSourceModelOverrideId = useId();
  const [providerId, setProviderId] = useState(
    providerModel?.provider_id ?? providers[0]?.id ?? "",
  );
  const [modelId, setModelId] = useState(providerModel?.model_id ?? models[0]?.id ?? "");
  const [apiModelId, setApiModelId] = useState(providerModel?.api_model_id ?? "");
  const [inputCost, setInputCost] = useState(
    providerModel?.input_cost_per_million == null
      ? ""
      : String(providerModel.input_cost_per_million),
  );
  const [outputCost, setOutputCost] = useState(
    providerModel?.output_cost_per_million == null
      ? ""
      : String(providerModel.output_cost_per_million),
  );
  const [fixedCost, setFixedCost] = useState(
    providerModel?.fixed_cost_per_call_usd == null
      ? ""
      : String(providerModel.fixed_cost_per_call_usd),
  );
  const [audioCost, setAudioCost] = useState(
    providerModel?.audio_cost_per_hour_usd == null
      ? ""
      : String(providerModel.audio_cost_per_hour_usd),
  );
  const [audioInputTransform, setAudioInputTransform] =
    useState<LlmAudioInputTransform>(
      providerModel?.audio_input_transform ?? "passthrough",
    );
  const [imageInputFormat, setImageInputFormat] = useState<LlmImageInputFormat>(
    providerModel?.image_input_format ?? "preserve",
  );
  const [imageInputMaxEdgePx, setImageInputMaxEdgePx] = useState(
    providerModel?.image_input_max_edge_px == null
      ? ""
      : String(providerModel.image_input_max_edge_px),
  );
  const [maxTokens, setMaxTokens] = useState(
    providerModel?.max_tokens_override == null
      ? ""
      : String(providerModel.max_tokens_override),
  );
  const [supportsSystemPrompt, setSupportsSystemPrompt] = useState(
    providerModel?.supports_system_prompt ?? true,
  );
  const [supportsTemperature, setSupportsTemperature] = useState(
    providerModel?.supports_temperature ?? true,
  );
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
  const [syncErr, setSyncErr] = useState<string | null>(null);
  const pricingSyncState = useRef({
    canSyncPricing: false,
    priceSourceDraftChanged: false,
    priceSourceOverride: "" as LlmPriceSourceOverride,
    priceSourceModelOverride: null as string | null,
  });
  const providerOptions = useMemo(() => providers.map(providerOption), [providers]);
  const modelOptions = useMemo(() => models.map(modelOption), [models]);
  const selectedProvider = useMemo(
    () => providers.find((item) => item.id === providerId),
    [providerId, providers],
  );
  const selectedModel = useMemo(
    () => models.find((item) => item.id === modelId),
    [modelId, models],
  );
  const supportsAudioInput = modelHasCapability(selectedModel, "audio_input");
  const supportsVision = modelHasCapability(selectedModel, "vision");
  const supportsReasoning = modelHasCapability(selectedModel, "reasoning");
  const supportsGeneration = modelSupportsGeneration(selectedModel);
  const persistedModel = useMemo(
    () =>
      providerModel
        ? models.find((item) => item.id === providerModel.model_id)
        : undefined,
    [models, providerModel],
  );
  const inheritedThinkingLevel = selectedModel?.thinking_level ?? "disabled";
  const inheritedThinkingStrategy =
    selectedModel?.thinking_strategy ??
    providerModel?.effective_thinking_strategy ??
    "none";
  const playgroundThinkingLevel = inheritedThinkingLevel;
  const playgroundThinkingStrategy =
    thinkingStrategyOverride === "inherit"
      ? inheritedThinkingStrategy
      : thinkingStrategyOverride;
  const effectivePriceSource =
    priceSourceOverride === "openrouter"
      ? "openrouter"
      : priceSourceOverride === ""
        ? selectedModel?.price_source
        : "manual";
  const canSyncPricing =
    mode === "edit" &&
    providerModel !== undefined &&
    modelId === providerModel.model_id &&
    effectivePriceSource === "openrouter";
  const priceSourceDraftChanged =
    providerModel !== undefined &&
    (priceSourceOverride !== providerModel.price_source_override ||
      emptyToNull(priceSourceModelOverride) !==
        providerModel.price_source_model_id_override);
  pricingSyncState.current = {
    canSyncPricing,
    priceSourceDraftChanged,
    priceSourceOverride,
    priceSourceModelOverride: emptyToNull(priceSourceModelOverride),
  };

  const invalidate = async () => {
    await qc.invalidateQueries({ queryKey: qk.adminLlmGraph() });
  };
  const syncPricing = useMutation({
    mutationFn: (_expected: {
      priceSourceOverride: LlmPriceSourceOverride;
      priceSourceModelOverride: string | null;
    }) => {
      if (!providerModel) throw new Error("Provider-model is required.");
      return fetchJson<LlmProviderModelSyncPricingResponse>(
        `/admin/api/v1/llm/provider-models/${providerModel.id}/sync-pricing`,
        { method: "POST" },
      );
    },
    onSuccess: async (response, expected) => {
      const current = pricingSyncState.current;
      if (
        !current.canSyncPricing ||
        current.priceSourceDraftChanged ||
        current.priceSourceOverride !== expected.priceSourceOverride ||
        current.priceSourceModelOverride !== expected.priceSourceModelOverride
      ) {
        return;
      }
      applyProviderModelPricing(response.provider_model);
      setSyncErr(null);
      setClientErr(null);
      setServerErr(null);
      await invalidate();
    },
    onError: (error: Error) =>
      setSyncErr(apiErrorCopy(error, "Provider-model pricing sync failed.")),
  });
  const syncDraftPricing = useMutation({
    mutationFn: (body: ProviderModelPayload) => {
      if (!providerModel) throw new Error("Provider-model is required.");
      return fetchJson<LlmProviderModel>(
        `/admin/api/v1/llm/provider-models/${providerModel.id}`,
        { method: "PUT", body },
      );
    },
    onSuccess: async (synced, body) => {
      const current = pricingSyncState.current;
      if (
        !current.canSyncPricing ||
        body.price_source_override !== current.priceSourceOverride ||
        body.price_source_model_id_override !== current.priceSourceModelOverride
      ) {
        return;
      }
      applyProviderModelPricing(synced);
      setSyncErr(null);
      setClientErr(null);
      setServerErr(null);
      await invalidate();
    },
    onError: (error: Error) =>
      setSyncErr(apiErrorCopy(error, "Provider-model pricing sync failed.")),
  });
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
  const syncErrId = syncErr ? "llm-provider-model-sync-error" : undefined;
  const extraHelpId = "llm-provider-model-extra-help";
  const thinkingStrategyHelpId = "llm-provider-model-thinking-strategy-help";
  const syncPending = syncPricing.isPending || syncDraftPricing.isPending;
  const priceSourceModelOverrideDescribedBy = describedBy(syncErrId);

  function applyProviderModelPricing(row: LlmProviderModel) {
    setInputCost(
      row.input_cost_per_million == null ? "" : String(row.input_cost_per_million),
    );
    setOutputCost(
      row.output_cost_per_million == null ? "" : String(row.output_cost_per_million),
    );
    setFixedCost(
      row.fixed_cost_per_call_usd == null ? "" : String(row.fixed_cost_per_call_usd),
    );
    setAudioCost(
      row.audio_cost_per_hour_usd == null ? "" : String(row.audio_cost_per_hour_usd),
    );
  }

  function persistedPayloadWithPriceSourceDraft(): ProviderModelPayload | null {
    if (!providerModel) return null;
    const mediaPayload = persistedMediaPayload(providerModel, persistedModel);
    return {
      provider_id: providerModel.provider_id,
      model_id: providerModel.model_id,
      api_model_id: providerModel.api_model_id,
      input_cost_per_million: providerModel.input_cost_per_million,
      output_cost_per_million: providerModel.output_cost_per_million,
      fixed_cost_per_call_usd: providerModel.fixed_cost_per_call_usd,
      audio_cost_per_hour_usd: providerModel.audio_cost_per_hour_usd,
      ...mediaPayload,
      max_tokens_override: providerModel.max_tokens_override,
      supports_system_prompt: providerModel.supports_system_prompt,
      supports_temperature: providerModel.supports_temperature,
      thinking_strategy_override: providerModel.thinking_strategy_override,
      extra_api_params: providerModel.extra_api_params,
      price_source_override: priceSourceOverride,
      price_source_model_id_override: emptyToNull(priceSourceModelOverride),
      is_enabled: providerModel.is_enabled,
    };
  }

  function syncCurrentPricing() {
    if (!canSyncPricing || syncPending) return;
    setSyncErr(null);
    if (priceSourceDraftChanged) {
      const body = persistedPayloadWithPriceSourceDraft();
      if (body) syncDraftPricing.mutate(body);
      return;
    }
    syncPricing.mutate({
      priceSourceOverride,
      priceSourceModelOverride: emptyToNull(priceSourceModelOverride),
    });
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    // code-health: ignore[ccn] Provider-model submit intentionally keeps all field validation next to the payload it sends.
    event.preventDefault();
    if (!providerId) return setClientErr("Provider is required.");
    if (!modelId) return setClientErr("Model is required.");
    if (!apiModelId.trim()) return setClientErr("API model id is required.");
    const inputParsed = optionalNonNegativeNumber(inputCost, "Input cost");
    if (!inputParsed.ok) return setClientErr(inputParsed.error);
    const outputParsed = optionalNonNegativeNumber(outputCost, "Output cost");
    if (!outputParsed.ok) return setClientErr(outputParsed.error);
    const fixedParsed = optionalNonNegativeNumber(fixedCost, "Fixed cost");
    if (!fixedParsed.ok) return setClientErr(fixedParsed.error);
    const audioParsed: ReturnType<typeof optionalNonNegativeNumber> = supportsAudioInput
      ? optionalNonNegativeNumber(audioCost, "Audio cost")
      : { ok: true, value: null };
    if (!audioParsed.ok) return setClientErr(audioParsed.error);
    const imageMaxEdgeValue = supportsVision ? numberOrNull(imageInputMaxEdgePx) : null;
    if (
      imageMaxEdgeValue !== null &&
      (!Number.isInteger(imageMaxEdgeValue) || imageMaxEdgeValue < 1)
    ) {
      return setClientErr("Image max edge must be a positive whole number.");
    }
    const maxTokensValue = supportsGeneration ? numberOrNull(maxTokens) : null;
    if (maxTokensValue !== null && (!Number.isInteger(maxTokensValue) || maxTokensValue < 1)) {
      return setClientErr("Max tokens override must be a positive whole number.");
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
      audio_cost_per_hour_usd: audioParsed.value,
      audio_input_transform: supportsAudioInput
        ? audioInputTransform
        : "passthrough",
      image_input_format: supportsVision ? imageInputFormat : "preserve",
      image_input_max_edge_px: supportsVision ? imageMaxEdgeValue : null,
      max_tokens_override: maxTokensValue,
      supports_system_prompt: supportsGeneration ? supportsSystemPrompt : true,
      supports_temperature: supportsGeneration ? supportsTemperature : true,
      thinking_strategy_override:
        supportsReasoning && thinkingStrategyOverride !== "inherit"
          ? thinkingStrategyOverride
          : null,
      extra_api_params: parsedExtra,
      price_source_override: priceSourceOverride,
      price_source_model_id_override: emptyToNull(priceSourceModelOverride),
      is_enabled: enabled,
    });
  }

  return (
    <FormModal
      open
      title={
        mode === "create"
          ? "Create provider-model"
          : `${selectedProvider?.name ?? "Unknown provider"} / ${selectedModel?.display_name ?? providerModel?.api_model_id ?? "Unknown model"}`
      }
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
              disabled={remove.isPending || save.isPending || syncPending}
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
            disabled={save.isPending || remove.isPending || syncPending}
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
        {mode === "create" ? (
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
        ) : null}
        <FormModalField label="API model id" requirement="required">
          <input
            value={apiModelId}
            onChange={(e) => setApiModelId(e.target.value)}
            required
            aria-invalid={clientErr === "API model id is required."}
            aria-describedby={errId}
          />
        </FormModalField>
        {supportsGeneration || supportsReasoning ? (
          <FormModalGrid className="llm-provider-model-costs">
            {supportsGeneration ? (
              <FormModalField label="Max tokens override" requirement="optional">
                <input
                  type="number"
                  min="1"
                  value={maxTokens}
                  onChange={(e) => setMaxTokens(e.target.value)}
                  aria-invalid={
                    clientErr ===
                    "Max tokens override must be a positive whole number."
                  }
                  aria-describedby={errId}
                />
              </FormModalField>
            ) : null}
            {supportsReasoning ? (
              <FormModalField
                label="Thinking strategy"
                requirement="optional"
                helpId={thinkingStrategyHelpId}
                helpText={`Model default: ${thinkingStrategyLabel(inheritedThinkingStrategy)}.`}
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
            ) : null}
          </FormModalGrid>
        ) : null}
        <FormModalGrid className="llm-provider-model-costs">
          <FormModalField label="Input cost per 1M" requirement="optional">
            <input
              type="number"
              min="0"
              step="0.0001"
              value={inputCost}
              onChange={(e) => setInputCost(e.target.value)}
              aria-invalid={clientErr === "Input cost must be zero or more."}
              aria-describedby={errId}
            />
          </FormModalField>
          <FormModalField label="Output cost per 1M" requirement="optional">
            <input
              type="number"
              min="0"
              step="0.0001"
              value={outputCost}
              onChange={(e) => setOutputCost(e.target.value)}
              aria-invalid={clientErr === "Output cost must be zero or more."}
              aria-describedby={errId}
            />
          </FormModalField>
        </FormModalGrid>
        <FormModalGrid className="llm-provider-model-costs">
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
          {supportsAudioInput ? (
            <FormModalField label="Audio cost per hour" requirement="optional">
              <input
                type="number"
                min="0"
                step="0.0001"
                value={audioCost}
                onChange={(e) => setAudioCost(e.target.value)}
                aria-invalid={clientErr === "Audio cost must be zero or more."}
                aria-describedby={errId}
              />
            </FormModalField>
          ) : null}
        </FormModalGrid>
        <FormModalGrid>
          <FormModalField label="Price source override" requirement="optional">
            <select
              value={priceSourceOverride}
              onChange={(e) => {
                setSyncErr(null);
                setPriceSourceOverride(e.target.value as LlmPriceSourceOverride);
              }}
            >
              <option value="">Use model default</option>
              <option value="openrouter">OpenRouter</option>
              <option value="none">Manual / pinned</option>
            </select>
          </FormModalField>
          <div className="field form-field form-field--optional form-modal__field llm-price-source-sync">
            <label
              htmlFor={priceSourceModelOverrideId}
              className="llm-price-source-sync__label"
            >
              <span className="form-field__label">
                Price source model override{" "}
                <span className="form-field__requirement form-field__requirement--optional">
                  Optional
                </span>
              </span>
            </label>
            <div className="llm-price-source-sync__control">
              <input
                id={priceSourceModelOverrideId}
                value={priceSourceModelOverride}
                onChange={(e) => {
                  setSyncErr(null);
                  setPriceSourceModelOverride(e.target.value);
                }}
                aria-invalid={syncErr ? true : undefined}
                aria-describedby={priceSourceModelOverrideDescribedBy}
              />
              {canSyncPricing ? (
                <button
                  type="button"
                  className="btn btn--ghost llm-price-source-sync__button"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={syncCurrentPricing}
                  disabled={syncPending || save.isPending || remove.isPending}
                >
                  {syncPending ? "Syncing..." : "Sync pricing"}
                </button>
              ) : null}
            </div>
            {syncErr ? (
              <p
                id="llm-provider-model-sync-error"
                className="form-error llm-price-source-sync__error"
                role="alert"
              >
                {syncErr}
              </p>
            ) : null}
          </div>
        </FormModalGrid>
        <FormModalField label="Active" requirement="optional">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
        </FormModalField>
        {supportsGeneration ? (
          <fieldset className="llm-registry-form__fieldset">
            <legend>Provider support overrides</legend>
            <div className="llm-registry-form__checks">
              <RegistryCheckPill
                checked={supportsSystemPrompt}
                onChange={setSupportsSystemPrompt}
              >
                System prompt
              </RegistryCheckPill>
              <RegistryCheckPill
                checked={supportsTemperature}
                onChange={setSupportsTemperature}
              >
                Temperature
              </RegistryCheckPill>
            </div>
          </fieldset>
        ) : null}
        {supportsVision ? (
          <fieldset className="llm-registry-form__fieldset">
            <legend>Image input</legend>
            <FormModalGrid>
              <FormModalField label="Image input format" requirement="optional">
                <select
                  value={imageInputFormat}
                  onChange={(e) => {
                    if (isImageInputFormat(e.target.value)) {
                      setImageInputFormat(e.target.value);
                    }
                  }}
                >
                  {IMAGE_INPUT_FORMAT_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </FormModalField>
              <FormModalField label="Image max edge" requirement="optional">
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={imageInputMaxEdgePx}
                  onChange={(e) => setImageInputMaxEdgePx(e.target.value)}
                  aria-invalid={
                    clientErr === "Image max edge must be a positive whole number."
                  }
                  aria-describedby={errId}
                />
              </FormModalField>
            </FormModalGrid>
          </fieldset>
        ) : null}
        {supportsAudioInput ? (
          <fieldset className="llm-registry-form__fieldset">
            <legend>Audio input</legend>
            <FormModalField label="Audio input transform" requirement="optional">
              <select
                value={audioInputTransform}
                onChange={(e) => {
                  if (isAudioInputTransform(e.target.value)) {
                    setAudioInputTransform(e.target.value);
                  }
                }}
              >
                {AUDIO_INPUT_TRANSFORM_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </FormModalField>
          </fieldset>
        ) : null}
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
        {mode === "edit" && providerModel && modelSupportsPlayground(persistedModel) ? (
          <LlmPlayground
            providerModel={providerModel}
            model={persistedModel}
            mode="direct"
            thinkingLevel={playgroundThinkingLevel}
            thinkingStrategy={playgroundThinkingStrategy}
            titleId="llm-provider-model-playground-title"
          />
        ) : null}
        {mode === "edit" &&
        providerModel &&
        !modelSupportsPlayground(persistedModel) &&
        modelSupportsEmbeddings(persistedModel) ? (
          <LlmEmbeddingSmoke
            providerModel={providerModel}
            model={persistedModel}
            titleId="llm-provider-model-embedding-smoke-title"
            description="Run a stateless smoke test against this embedding provider-model."
          />
        ) : null}
    </FormModal>
  );
}
