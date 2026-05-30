import type { KeyboardEvent } from "react";
import type { ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import FileDropZone from "@/components/FileDropZone";
import { FormModalField } from "@/components/FormModal";
import { ApiError, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { usePatchReducer } from "@/lib/usePatchReducer";
import type {
  LlmAssignment,
  LlmModel,
  LlmProviderModel,
  LlmProviderModelPlaygroundMode,
  LlmProviderModelPlaygroundResponse,
  LlmThinkingLevel,
  LlmThinkingStrategy,
} from "@/types";

interface LlmPlaygroundProps {
  providerModel: LlmProviderModel;
  model: LlmModel | undefined;
  mode: LlmProviderModelPlaygroundMode;
  assignment?: LlmAssignment;
  thinkingLevel?: LlmThinkingLevel;
  thinkingStrategy?: LlmThinkingStrategy;
  titleId: string;
  description?: string;
}

interface PlaygroundErrorNotice {
  message: string;
  code: string;
  errorId: string | null;
}

interface LlmPlaygroundState {
  prompt: string;
  systemPrompt: string;
  imageUrl: string;
  imageFile: File | null;
  audioUrl: string;
  audioFile: File | null;
  clientErr: PlaygroundErrorNotice | null;
  result: LlmProviderModelPlaygroundResponse | null;
}

const DEFAULT_VISION_ONLY_PROMPT = "Extract the text from this image.";

// react-doctor-disable-next-line react-doctor/no-giant-component -- Existing promoted surface is intentionally deferred until a focused component split preserves behavior.
export default function LlmPlayground({
  providerModel,
  model,
  mode,
  assignment,
  thinkingLevel,
  thinkingStrategy,
  titleId,
  description,
}: LlmPlaygroundProps) {
  const qc = useQueryClient();
  const [playgroundState, setPlaygroundState] = usePatchReducer<LlmPlaygroundState>({
    prompt: "",
    systemPrompt: "",
    imageUrl: "",
    imageFile: null,
    audioUrl: "",
    audioFile: null,
    clientErr: null,
    result: null,
  });
  const {
    prompt,
    systemPrompt,
    imageUrl,
    imageFile,
    audioUrl,
    audioFile,
    clientErr,
    result,
  } = playgroundState;

  const supportsChat = model?.capabilities.includes("chat") ?? false;
  const supportsVision = model?.capabilities.includes("vision") ?? false;
  const supportsAudio = model?.capabilities.includes("audio_input") ?? false;
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
      void qc.invalidateQueries({ queryKey: qk.adminLlmCalls() });
      setPlaygroundState({ clientErr: null, result: response });
    },
    onError: (error: Error) => {
      setPlaygroundState({ result: null, clientErr: playgroundErrorCopy(error) });
    },
  });

  function runPlayground(): void {
    if (supportsVision && imageUrl.trim() && imageFile) {
      setPlaygroundState({
        result: null,
        clientErr: {
          message: "Use either an image URL or an uploaded image, not both.",
          code: "playground_image_multiple_sources",
          errorId: null,
        },
      });
      return;
    }
    if (
      !supportsChat &&
      supportsVision &&
      !supportsAudio &&
      !imageFile &&
      !imageUrl.trim()
    ) {
      setPlaygroundState({
        result: null,
        clientErr: {
          message: "Image is required for this vision-only model.",
          code: "playground_image_required",
          errorId: null,
        },
      });
      return;
    }
    if (!supportsChat && supportsAudio && !audioFile && !audioUrl.trim()) {
      setPlaygroundState({
        result: null,
        clientErr: {
          message: "Audio is required for this audio-only model.",
          code: "playground_audio_required",
          errorId: null,
        },
      });
      return;
    }
    if (supportsAudio && audioUrl.trim() && audioFile) {
      setPlaygroundState({
        result: null,
        clientErr: {
          message: "Use either an audio URL or an uploaded audio file, not both.",
          code: "playground_audio_multiple_sources",
          errorId: null,
        },
      });
      return;
    }
    if (!effectivePrompt()) {
      setPlaygroundState({
        result: null,
        clientErr: {
          message: "Prompt is required.",
          code: "prompt_required",
          errorId: null,
        },
      });
      return;
    }
    if (mode === "assignment" && !assignment) {
      setPlaygroundState({
        result: null,
        clientErr: {
          message: "Assignment is required.",
          code: "assignment_required",
          errorId: null,
        },
      });
      return;
    }

    setPlaygroundState({ clientErr: null });
    playground.mutate(buildPlaygroundBody());
  }

  function buildPlaygroundBody(): FormData | Record<string, unknown> {
    const base = {
      mode,
      prompt: effectivePrompt(),
      system_prompt: supportsSystemPrompt ? emptyToNull(systemPrompt) : null,
      max_tokens: null,
      temperature: null,
      image_url: supportsVision ? emptyToNull(imageUrl) : null,
      audio_url: supportsAudio ? emptyToNull(audioUrl) : null,
      assignment_id: mode === "assignment" ? (assignment?.id ?? null) : null,
      thinking_level: thinkingLevel ?? null,
      thinking_strategy: thinkingStrategy ?? null,
    };
    if ((!supportsVision || imageFile === null) && (!supportsAudio || audioFile === null)) {
      return base;
    }

    const body = new FormData();
    for (const [key, value] of Object.entries(base)) {
      if (value !== null) body.append(key, String(value));
    }
    if (supportsVision && imageFile !== null) body.append("image_file", imageFile);
    if (supportsAudio && audioFile !== null) body.append("audio_file", audioFile);
    return body;
  }

  function effectivePrompt(): string {
    const trimmed = prompt.trim();
    if (trimmed) return trimmed;
    if (!supportsChat && supportsVision && !supportsAudio && (imageFile || imageUrl.trim())) {
      return DEFAULT_VISION_ONLY_PROMPT;
    }
    return "";
  }

  function resetPlayground(): void {
    setPlaygroundState({
      prompt: "",
      systemPrompt: "",
      imageUrl: "",
      imageFile: null,
      audioUrl: "",
      audioFile: null,
      clientErr: null,
      result: null,
    });
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
          {description ? <p>{description}</p> : null}
        </div>
        <span className="llm-playground__target mono">{providerModel.api_model_id}</span>
      </header>

      {assignment ? <AssignmentDefaults assignment={assignment} /> : null}

      <PlaygroundDisclosure title="System prompt">
        <FormModalField
          label="System prompt"
          requirement="optional"
          helpId={!supportsSystemPrompt ? `${titleId}-system-help` : undefined}
          helpText={!supportsSystemPrompt ? "Disabled for this provider-model." : undefined}
        >
          <AutoGrowTextarea
            value={systemPrompt}
            onChange={(event) => setPlaygroundState({ systemPrompt: event.target.value })}
            rows={3}
            disabled={!supportsSystemPrompt}
            aria-describedby={
              !supportsSystemPrompt ? `${titleId}-system-help` : undefined
            }
          />
        </FormModalField>
      </PlaygroundDisclosure>

      <FormModalField label="Prompt" requirement="required">
        <AutoGrowTextarea
          value={prompt}
          onChange={(event) => setPlaygroundState({ prompt: event.target.value })}
          rows={4}
          required
          aria-invalid={clientErr?.code === "prompt_required"}
          aria-describedby={describedBy(errId, result ? resultId : undefined)}
        />
      </FormModalField>

      {supportsVision ? (
        <PlaygroundDisclosure title="Image">
          <FormModalField label="Image URL" requirement="optional">
            <input
              type="url"
              value={imageUrl}
              onChange={(event) => setPlaygroundState({ imageUrl: event.target.value })}
              placeholder="https://example.com/image.jpg"
              aria-describedby={errId}
             aria-label="Image URL"/>
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
              setPlaygroundState({ imageFile: files[0] ?? null, clientErr: null });
            }}
          />
        </PlaygroundDisclosure>
      ) : null}

      {supportsAudio ? (
        <PlaygroundDisclosure title="Audio">
          <FormModalField label="Audio URL" requirement="optional">
            <input
              type="url"
              value={audioUrl}
              onChange={(event) => setPlaygroundState({ audioUrl: event.target.value })}
              placeholder="https://example.com/audio.mp3"
              aria-describedby={errId}
             aria-label="Audio URL"/>
          </FormModalField>
          <FileDropZone
            title={audioFile ? audioFile.name : "Upload audio"}
            description={
              audioFile
                ? "This audio file will be submitted with the playground run."
                : "Drop an audio file here or choose a file."
            }
            inputLabel="Upload playground audio"
            accept="audio/*"
            disabled={playground.isPending}
            onFiles={(files) => {
              setPlaygroundState({ audioFile: files[0] ?? null, clientErr: null });
            }}
          />
        </PlaygroundDisclosure>
      ) : null}

      {clientErr ? (
        <PlaygroundErrorAlert id={errId} error={clientErr} />
      ) : null}

      {playground.isPending ? (
        <output className="llm-playground__status">
          Running playground test…
        </output>
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
          {playground.isPending ? "Running…" : "Run playground"}
        </button>
      </div>
    </section>
  );
}

function PlaygroundDisclosure({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <details className="llm-playground-disclosure">
      <summary>{title}</summary>
      <div className="llm-playground-disclosure__body">{children}</div>
    </details>
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
    ...(result.status === "error"
      ? [
          ["Error ID", result.error_id ?? null],
          ["Error code", result.error_code ?? null],
        ]
      : []),
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

function PlaygroundErrorAlert({
  id,
  error,
}: {
  id: string | undefined;
  error: PlaygroundErrorNotice;
}) {
  return (
    <section
      id={id}
      className="llm-playground-error"
      role="alert"
      aria-live="assertive"
    >
      <strong>{error.message}</strong>
      <dl className="llm-playground-error__meta">
        {error.errorId ? (
          <div>
            <dt>Error ID</dt>
            <dd className="mono">{error.errorId}</dd>
          </div>
        ) : null}
        <div>
          <dt>Code</dt>
          <dd className="mono">{error.code}</dd>
        </div>
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

function playgroundErrorCopy(error: unknown): PlaygroundErrorNotice {
  if (error instanceof ApiError) {
    const code =
      typeof error.problem?.error === "string" ? error.problem.error : undefined;
    const detail = error.detail;
    const errorId = error.requestId;
    if (code === "provider_api_key_missing" || code === "provider_client_unavailable") {
      return {
        message: "The provider client is not configured for playground runs.",
        code,
        errorId,
      };
    }
    if (code === "provider_disabled") {
      return { message: "The provider is disabled.", code, errorId };
    }
    if (code === "provider_model_disabled") {
      return { message: "The provider-model is disabled.", code, errorId };
    }
    if (code === "model_inactive") {
      return { message: "The model is inactive.", code, errorId };
    }
    if (code === "assignment_not_found") {
      return { message: "The assignment no longer exists.", code, errorId };
    }
    if (code === "assignment_provider_model_mismatch") {
      return {
        message: "That assignment no longer points at this provider-model.",
        code,
        errorId,
      };
    }
    if (code === "assignment_disabled") {
      return { message: "The assignment is disabled.", code, errorId };
    }
    if (code === "system_prompt_not_supported") {
      return {
        message: "System prompts are disabled for this provider-model.",
        code,
        errorId,
      };
    }
    if (code === "temperature_not_supported") {
      return {
        message: "Temperature is disabled for this provider-model.",
        code,
        errorId,
      };
    }
    if (code === "max_tokens_exceeds_model_limit") {
      return {
        message: detail ?? "Max tokens exceeds the selected model limit.",
        code,
        errorId,
      };
    }
    if (code === "max_tokens_exceeds_playground_limit") {
      return {
        message: detail ?? "Max tokens exceeds the playground limit.",
        code,
        errorId,
      };
    }
    if (code === "image_requires_vision_model") {
      return { message: "Images require a vision-capable model.", code, errorId };
    }
    if (code === "audio_requires_audio_model") {
      return { message: "Audio requires an audio-input model.", code, errorId };
    }
    if (code === "playground_image_required") {
      return {
        message: "Image is required for this vision-only model.",
        code,
        errorId,
      };
    }
    if (code === "playground_audio_required") {
      return {
        message: "Audio is required for this audio-only model.",
        code,
        errorId,
      };
    }
    if (code === "playground_image_file_too_large") {
      return {
        message: "Image upload is too large for a playground run.",
        code,
        errorId,
      };
    }
    if (code === "playground_image_url_unavailable") {
      return {
        message: detail ?? "Image URL must be a public HTTPS image or a data URL.",
        code,
        errorId,
      };
    }
    if (code === "playground_image_type_unsupported") {
      return {
        message: detail ?? "Image input must be an image file.",
        code,
        errorId,
      };
    }
    if (code === "playground_audio_file_too_large") {
      return {
        message: "Audio upload is too large for a playground run.",
        code,
        errorId,
      };
    }
    if (code === "playground_audio_url_unavailable") {
      return {
        message: detail ?? "Audio URL must be a public HTTPS audio file or a data URL.",
        code,
        errorId,
      };
    }
    if (code === "playground_audio_type_unsupported") {
      return {
        message: detail ?? "Audio input must be a supported audio file.",
        code,
        errorId,
      };
    }
    return {
      message: detail ?? error.title ?? "Playground run failed.",
      code: code ?? error.type ?? `http_${error.status}`,
      errorId,
    };
  }
  return {
    message: error instanceof Error ? error.message : "Playground run failed.",
    code: "client_error",
    errorId: null,
  };
}

function formatNullable(value: number | string | null, suffix = ""): string {
  if (value === null || value === "") return "n/a";
  return `${value}${suffix}`;
}

function formatPlaygroundCost(value: string | null): string {
  if (value === null) return "n/a";
  return `$${value}`;
}
