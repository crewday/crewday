import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { ApiError, fetchJson } from "@/lib/api";
import type {
  LlmModel,
  LlmProviderModel,
  LlmProviderModelEmbeddingSmokeRequest,
  LlmProviderModelEmbeddingSmokeResponse,
} from "@/types";

interface LlmEmbeddingSmokeProps {
  providerModel: LlmProviderModel;
  model?: LlmModel;
  titleId?: string;
  description?: string;
}

export default function LlmEmbeddingSmoke({
  providerModel,
  model,
  titleId,
  description,
}: LlmEmbeddingSmokeProps) {
  const [text, setText] = useState("crew.day local embedding smoke test");
  const [clientErr, setClientErr] = useState<string | null>(null);
  const smoke = useMutation({
    mutationFn: (body: LlmProviderModelEmbeddingSmokeRequest) =>
      fetchJson<LlmProviderModelEmbeddingSmokeResponse>(
        `/admin/api/v1/llm/provider-models/${providerModel.id}/embedding-smoke`,
        { method: "POST", body },
      ),
    onError: (error) => setClientErr(embeddingSmokeErrorCopy(error)),
  });

  function runSmoke() {
    const value = text.trim();
    if (!value) {
      setClientErr("Text is required.");
      smoke.reset();
      return;
    }
    setClientErr(null);
    smoke.mutate({ text: value });
  }

  return (
    <section
      className="llm-playground"
      aria-labelledby={titleId}
      aria-label={titleId ? undefined : "Embedding smoke"}
    >
      <header className="llm-playground__head">
        <div>
          <h3 id={titleId}>Embedding smoke</h3>
          {description ? <p>{description}</p> : null}
        </div>
        <span className="llm-playground__target mono">{providerModel.api_model_id}</span>
      </header>
      <label className="llm-playground__field">
        <span>Text Required</span>
        <textarea
          rows={4}
          value={text}
          onChange={(event) => setText(event.target.value)}
          aria-invalid={clientErr === "Text is required."}
         aria-label="llm-playground__field Text Text is required."/>
      </label>
      {smoke.isPending ? (
        <output className="llm-playground__status">
          Running embedding smoke...
        </output>
      ) : null}
      <div className="llm-playground__actions">
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => {
            setText("");
            setClientErr(null);
            smoke.reset();
          }}
          disabled={smoke.isPending}
        >
          Reset smoke
        </button>
        <button
          type="button"
          className="btn btn--moss"
          onClick={runSmoke}
          disabled={smoke.isPending}
        >
          {smoke.isPending ? "Running..." : "Run embedding smoke"}
        </button>
      </div>
      {clientErr ? <EmbeddingSmokeError message={clientErr} /> : null}
      {smoke.data ? <EmbeddingSmokeResult result={smoke.data} model={model} /> : null}
    </section>
  );
}

function EmbeddingSmokeResult({
  result,
  model,
}: {
  result: LlmProviderModelEmbeddingSmokeResponse;
  model?: LlmModel;
}) {
  return (
    <article
      className={
        "llm-playground-result" +
        (result.status === "error" ? " llm-playground-result--error" : "")
      }
    >
      <header className="llm-playground-result__head">
        <strong>{result.status === "ok" ? "Embedding ready" : "Embedding failed"}</strong>
        <span>{result.latency_ms} ms</span>
      </header>
      <dl className="llm-playground-result__diagnostics">
        <div>
          <dt>Provider</dt>
          <dd>{result.provider_used}</dd>
        </div>
        <div>
          <dt>Model</dt>
          <dd>{result.model_used}</dd>
        </div>
        <div>
          <dt>Dimensions</dt>
          <dd>{result.embedding_dimensions ?? model?.embedding_dimensions ?? "Unknown"}</dd>
        </div>
        <div>
          <dt>Norm</dt>
          <dd>{result.vector_norm ?? "Unknown"}</dd>
        </div>
        {result.error_code ? (
          <div>
            <dt>Error</dt>
            <dd>{result.error_code}</dd>
          </div>
        ) : null}
        {result.error_id ? (
          <div>
            <dt>Request</dt>
            <dd>{result.error_id}</dd>
          </div>
        ) : null}
      </dl>
      {result.error_message ? (
        <p className="llm-playground-error__message">{result.error_message}</p>
      ) : null}
    </article>
  );
}

function EmbeddingSmokeError({ message }: { message: string }) {
  return (
    <div className="llm-playground-error" role="alert">
      <strong>Embedding smoke unavailable</strong>
      <p className="llm-playground-error__message">{message}</p>
    </div>
  );
}

function embeddingSmokeErrorCopy(error: unknown): string {
  if (error instanceof ApiError) {
    const code =
      typeof error.problem?.error === "string" ? error.problem.error : undefined;
    if (code === "embedding_smoke_provider_type_not_supported") {
      return "This provider-model is not served by a local embedding provider.";
    }
    if (code === "embedding_smoke_requires_embedding_model") {
      return "This model does not expose embeddings.";
    }
    if (code === "embedding_dimensions_unknown") {
      return "This model does not declare embedding dimensions.";
    }
    return error.detail ?? error.title ?? error.message;
  }
  return error instanceof Error ? error.message : "Embedding smoke failed.";
}
