import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import DateTime from "@/components/DateTime";
import FormModal, { FormModalField } from "@/components/FormModal";
import { Chip } from "@/components/common";
import { ApiError, fetchJson } from "@/lib/api";
import { useModalDialog } from "@/lib/modalDialog";
import { qk } from "@/lib/queryKeys";
import type {
  LlmPromptRevision,
  LlmPromptTemplate,
  LlmPromptTemplateDetail,
} from "@/types";

interface PromptLibraryDrawerProps {
  prompts: LlmPromptTemplate[];
  onClose: () => void;
}

export default function PromptLibraryDrawer({
  prompts,
  onClose,
}: PromptLibraryDrawerProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selectedPrompt = useMemo(
    () => prompts.find((prompt) => prompt.id === selectedId) ?? null,
    [prompts, selectedId],
  );
  // Native <dialog> + showModal() gives the focus trap, Esc-to-close,
  // ::backdrop scrim, focus-restore-on-close, and backdrop click-to-close.
  const dialog = useModalDialog(onClose);

  return (
    <dialog
      ref={dialog.ref}
      className="llm-prompt-drawer"
      aria-label="Prompt library"
      onCancel={dialog.onCancel}
    >
      <header className="llm-prompt-drawer__head">
        <h2>Prompt library</h2>
        <button type="button" className="btn btn--ghost" onClick={onClose}>
          Close
        </button>
      </header>
      <p className="llm-prompt-drawer__hint muted">
        Hash-self-seeding: code defaults seed the row; unmodified prompts
        auto-upgrade when code changes; customisations are preserved.
      </p>
      <ul className="llm-prompt-list">
        {prompts.map((p) => (
          <li key={p.id} className="llm-prompt-list__item">
            <button
              type="button"
              className="llm-prompt-list__button"
              onClick={() => setSelectedId(p.id)}
            >
              <span className="llm-prompt-list__head">
                <code className="inline-code">{p.capability}</code>
                <span className="llm-prompt-list__name">{p.name}</span>
                <span className="llm-prompt-list__ver mono muted">v{p.version}</span>
                {p.is_customised ? (
                  <Chip tone="sand" size="sm">
                    customised
                  </Chip>
                ) : (
                  <Chip tone="ghost" size="sm">
                    default
                  </Chip>
                )}
              </span>
              <span className="llm-prompt-list__preview">{p.preview}</span>
            </button>
            <footer className="llm-prompt-list__foot muted">
              <span>
                {p.revisions_count} revision
                {p.revisions_count === 1 ? "" : "s"}
              </span>
              <span>hash {p.default_hash}</span>
            </footer>
          </li>
        ))}
      </ul>
      {selectedPrompt ? (
        <PromptEditorDialog
          prompt={selectedPrompt}
          onClose={() => setSelectedId(null)}
        />
      ) : null}
    </dialog>
  );
}

interface PromptEditorDialogProps {
  prompt: LlmPromptTemplate;
  onClose: () => void;
}

interface PromptPayload {
  template: string;
  notes: string | null;
}

function promptErrorCopy(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    return error.detail ?? error.title ?? error.message ?? fallback;
  }
  return error instanceof Error ? error.message : fallback;
}

function PromptEditorDialog({ prompt, onClose }: PromptEditorDialogProps) {
  // code-health: ignore[ccn nloc] Lizard misattributes the prompt editor TSX body to the preceding error-copy helper.
  const qc = useQueryClient();
  const [draft, setDraft] = useState<PromptPayload | null>(null);
  const [clientErr, setClientErr] = useState<string | null>(null);
  const [serverErr, setServerErr] = useState<string | null>(null);
  const detailQ = useQuery({
    queryKey: [...qk.adminLlmPrompts(), "detail", prompt.id],
    queryFn: () =>
      fetchJson<LlmPromptTemplateDetail>(`/admin/api/v1/llm/prompts/${prompt.id}`),
  });
  const revisionsQ = useQuery({
    queryKey: [...qk.adminLlmPrompts(), "revisions", prompt.id],
    queryFn: () =>
      fetchJson<LlmPromptRevision[]>(
        `/admin/api/v1/llm/prompts/${prompt.id}/revisions`,
      ),
  });

  const save = useMutation({
    mutationFn: (body: PromptPayload) =>
      fetchJson<LlmPromptTemplateDetail>(`/admin/api/v1/llm/prompts/${prompt.id}`, {
        method: "PUT",
        body,
    }),
    onSuccess: async (updated) => {
      setDraft({ template: updated.template, notes: updated.notes ?? "" });
      setClientErr(null);
      setServerErr(null);
      await qc.invalidateQueries({ queryKey: qk.adminLlmPrompts() });
    },
    onError: (error: Error) => setServerErr(promptErrorCopy(error, "Prompt save failed.")),
  });
  const reset = useMutation({
    mutationFn: () =>
      fetchJson<LlmPromptTemplateDetail>(
        `/admin/api/v1/llm/prompts/${prompt.id}/reset-to-default`,
        { method: "POST" },
      ),
    onSuccess: async (updated) => {
      setDraft({ template: updated.template, notes: updated.notes ?? "" });
      setClientErr(null);
      setServerErr(null);
      await qc.invalidateQueries({ queryKey: qk.adminLlmPrompts() });
    },
    onError: (error: Error) => setServerErr(promptErrorCopy(error, "Prompt reset failed.")),
  });

  const detail = detailQ.data;
  const template = draft?.template ?? detail?.template ?? "";
  const notes = draft?.notes ?? detail?.notes ?? "";
  const err = clientErr ?? serverErr;
  const errId = err ? "llm-prompt-error" : undefined;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!template.trim()) return setClientErr("Prompt body is required.");
    setClientErr(null);
    setServerErr(null);
    save.mutate({ template, notes: notes.trim() ? notes.trim() : null });
  }

  return (
    <FormModal
      open
      title={prompt.name}
      titleId="llm-prompt-editor-title"
      eyebrow="Prompt template"
      width="wide"
      onClose={onClose}
      onSubmit={submit}
      noValidate
      actions={
        <>
          <button
            type="button"
            className="btn btn--rust llm-registry-form__delete"
            onClick={() => reset.mutate()}
            disabled={reset.isPending || save.isPending}
          >
            {reset.isPending ? "Resetting…" : "Reset to default"}
          </button>
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="btn btn--moss"
            disabled={save.isPending || reset.isPending}
          >
            {save.isPending ? "Saving…" : "Save prompt"}
          </button>
        </>
      }
    >
          <div className="llm-prompt-dialog__meta">
            <code className="inline-code">{prompt.capability}</code>
            <span>v{detail?.version ?? prompt.version}</span>
            <span>hash {detail?.default_hash ?? prompt.default_hash}</span>
            <span>
              Updated{" "}
              <DateTime value={detail?.updated_at ?? prompt.updated_at} showTime empty="," />
            </span>
            {(detail?.is_customised ?? prompt.is_customised) ? (
              <Chip tone="sand" size="sm">
                customised
              </Chip>
            ) : (
              <Chip tone="ghost" size="sm">
                default
              </Chip>
            )}
          </div>
          {detailQ.isPending ? <p className="muted">Loading prompt body…</p> : null}
          {detailQ.isError ? <p className="form-error">Prompt body failed to load.</p> : null}
          <FormModalField label="Active template body" requirement="required">
            <AutoGrowTextarea
              value={template}
              onChange={(e) => setDraft({ template: e.target.value, notes })}
              rows={14}
              required
              aria-invalid={clientErr === "Prompt body is required."}
              aria-describedby={errId}
            />
          </FormModalField>
          <FormModalField label="Notes" requirement="optional">
            <AutoGrowTextarea
              value={notes}
              onChange={(e) => setDraft({ template, notes: e.target.value })}
              rows={3}
            />
          </FormModalField>
          <section className="llm-prompt-revisions" aria-label="Prompt revision history">
            <h4>Revision history</h4>
            {revisionsQ.isPending ? <p className="muted">Loading revisions…</p> : null}
            {revisionsQ.isError ? <p className="form-error">Revisions failed to load.</p> : null}
            {revisionsQ.data?.length === 0 ? (
              <p className="muted">No previous revisions.</p>
            ) : null}
            {revisionsQ.data?.map((revision) => (
              <article key={revision.id} className="llm-prompt-revision">
                <header>
                  <strong>v{revision.version}</strong>
                  <DateTime value={revision.created_at} showTime className="mono muted" />
                </header>
                <p>{revision.body.slice(0, 180)}</p>
                {revision.notes ? <p className="muted">{revision.notes}</p> : null}
              </article>
            ))}
          </section>
          {err ? (
            <p id={errId} className="form-error">
              {err}
            </p>
          ) : null}
    </FormModal>
  );
}
