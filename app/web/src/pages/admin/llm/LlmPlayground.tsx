import { useState } from "react";
import type { KeyboardEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import FileDropZone from "@/components/FileDropZone";
import { FormModalField } from "@/components/FormModal";
import { ApiError, fetchJson } from "@/lib/api";
import type {
  LlmAssignment,
  LlmModel,
  LlmProviderModel,
  LlmProviderModelPlaygroundMode,
  LlmProviderModelPlaygroundResponse,
} from "@/types";

interface LlmPlaygroundProps {
  providerModel: LlmProviderModel;
  model: LlmModel | undefined;
  mode: LlmProviderModelPlaygroundMode;
  assignment?: LlmAssignment;
  titleId: string;
  description: string;
}

export default function LlmPlayground({
  providerModel,
  model,
  mode,
  assignment,
  titleId,
  description,
}: LlmPlaygroundProps) {
  const [prompt, setPrompt] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [imageUrl, setImageUrl] = useState("");
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [clientErr, setClientErr] = useState<string | null>(null);
  const [result, setResult] =
    useState<LlmProviderModelPlaygroundResponse | null>(null);

  const supportsVision = model?.capabilities.includes("vision") ?? false;
  const supportsSystemPrompt = providerModel.supports_system_prompt;
  const resultId = `${titleId}-result`;
  const errId = clientErr ? `${titleId}-error` : undefined;

  const playground = useMutation({
    mutationFn: (body: FormData | Record<string, unknown>) =>
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
    if (supportsVision && imageUrl.trim() && imageFile) {
      setResult(null);
      setClientErr("Use either an image URL or an uploaded image, not both.");
      return;
    }
    if (mode === "assignment" && !assignment) {
      setResult(null);
      setClientErr("Assignment is required.");
      return;
    }

    setClientErr(null);
    playground.mutate(buildPlaygroundBody());
  }

  function buildPlaygroundBody(): FormData | Record<string, unknown> {
    const base = {
      mode,
      prompt: prompt.trim(),
      system_prompt: supportsSystemPrompt ? emptyToNull(systemPrompt) : null,
      max_tokens: null,
      temperature: null,
      image_url: supportsVision ? emptyToNull(imageUrl) : null,
      assignment_id: mode === "assignment" ? (assignment?.id ?? null) : null,
    };
    if (!supportsVision || imageFile === null) return base;

    const body = new FormData();
    for (const [key, value] of Object.entries(base)) {
      if (value !== null) body.append(key, String(value));
    }
    body.append("image_file", imageFile);
    return body;
  }

  function resetPlayground(): void {
    setPrompt("");
    setSystemPrompt("");
    setImageUrl("");
    setImageFile(null);
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
      aria-labelledby={titleId}
      onKeyDownCapture={preventAccidentalSave}
    >
      <header className="llm-playground__head">
        <div>
          <h4 id={titleId}>Playground</h4>
          <p>{description}</p>
        </div>
        <span className="llm-playground__target mono">{providerModel.api_model_id}</span>
      </header>

      {assignment ? <AssignmentDefaults assignment={assignment} /> : null}

      <FormModalField
        label="System prompt"
        requirement="optional"
        helpId={!supportsSystemPrompt ? `${titleId}-system-help` : undefined}
        helpText={!supportsSystemPrompt ? "Disabled for this provider-model." : undefined}
      >
        <AutoGrowTextarea
          value={systemPrompt}
          onChange={(event) => setSystemPrompt(event.target.value)}
          rows={3}
          disabled={!supportsSystemPrompt}
          aria-describedby={
            !supportsSystemPrompt ? `${titleId}-system-help` : undefined
          }
        />
      </FormModalField>

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

      {supportsVision ? (
        <div className="llm-playground__vision">
          <FormModalField label="Image URL" requirement="optional">
            <input
              type="url"
              value={imageUrl}
              onChange={(event) => setImageUrl(event.target.value)}
              placeholder="https://example.com/image.jpg"
              aria-describedby={errId}
            />
          </FormModalField>
          <FileDropZone
            title={imageFile ? imageFile.name : "Upload image"}
            description={
              imageFile
                ? "This image will be submitted with the playground run."
                : "Drop an image here or choose a file."
            }
            inputLabel="Upload playground image"
            accept="image/*"
            disabled={playground.isPending}
            onFiles={(files) => {
              setImageFile(files[0] ?? null);
              setClientErr(null);
            }}
          />
        </div>
      ) : null}

      {clientErr ? (
        <p id={errId} className="form-error" role="alert">
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
          assignment={result.assignment_id ? assignment : undefined}
        />
      ) : null}

      <div className="llm-playground__actions">
        <button
          type="button"
          className="btn btn--ghost"
          onClick={resetPlayground}
          disabled={playground.isPending}
        >
          Reset playground
        </button>
        <button
          type="button"
          className="btn btn--moss"
          onClick={runPlayground}
          disabled={playground.isPending}
        >
          {playground.isPending ? "Running..." : "Run playground"}
        </button>
      </div>
    </section>
  );
}

function AssignmentDefaults({ assignment }: { assignment: LlmAssignment }) {
  const defaults = [
    assignment.max_tokens == null ? null : `Max tokens ${assignment.max_tokens}`,
    assignment.temperature == null ? null : `Temperature ${assignment.temperature}`,
  ].filter((value): value is string => value !== null);

  if (defaults.length === 0) {
    return (
      <p className="llm-playground__defaults">
        Assignment tuning defaults apply automatically.
      </p>
    );
  }

  return (
    <div className="llm-playground__defaults" aria-label="Assignment tuning defaults">
      {defaults.map((value) => (
        <span key={value} className="llm-playground__default-chip">
          {value}
        </span>
      ))}
    </div>
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

function emptyToNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function describedBy(...ids: (string | undefined)[]): string | undefined {
  const value = ids.filter(Boolean).join(" ");
  return value || undefined;
}

function playgroundErrorCopy(error: unknown): string {
  if (error instanceof ApiError) {
    const code =
      typeof error.problem?.error === "string" ? error.problem.error : undefined;
    if (code === "provider_api_key_missing" || code === "provider_client_unavailable") {
      return "The provider client is not configured for playground runs.";
    }
    if (code === "provider_disabled") return "The provider is disabled.";
    if (code === "provider_model_disabled") return "The provider-model is disabled.";
    if (code === "model_inactive") return "The model is inactive.";
    if (code === "assignment_not_found") return "The assignment no longer exists.";
    if (code === "assignment_provider_model_mismatch") {
      return "That assignment no longer points at this provider-model.";
    }
    if (code === "assignment_disabled") return "The assignment is disabled.";
    if (code === "system_prompt_not_supported") {
      return "System prompts are disabled for this provider-model.";
    }
    if (code === "temperature_not_supported") {
      return "Temperature is disabled for this provider-model.";
    }
    if (code === "max_tokens_exceeds_model_limit") {
      return "Max tokens exceeds the selected model limit.";
    }
    if (code === "image_requires_vision_model") {
      return "Images require a vision-capable model.";
    }
    if (code === "playground_image_file_too_large") {
      return "Image upload is too large for a playground run.";
    }
    return error.title ?? "Playground run failed.";
  }
  return error instanceof Error ? error.message : "Playground run failed.";
}

function formatNullable(value: number | string | null, suffix = ""): string {
  if (value === null || value === "") return "n/a";
  return `${value}${suffix}`;
}

function formatPlaygroundCost(value: string | null): string {
  if (value === null) return "n/a";
  return `$${value}`;
}
