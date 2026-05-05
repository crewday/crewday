import { type ChangeEvent, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Ban, Camera, Check, SkipForward } from "lucide-react";
import { Chip, EmptyState, Loading } from "@/components/common";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import ChatLog from "@/components/chat/ChatLog";
import ChatComposer from "@/components/chat/ChatComposer";
import PageHeader from "@/components/PageHeader";
import { fmtTime } from "@/lib/dates";
import type {
  AgentMessage,
  Property,
} from "@/types/api";
import {
  commentToMessage,
  normalizeTaskDetail,
  updateChecklistItem,
  type ApiChecklistItem,
  type ApiTask,
  type ApiTaskState,
  type CommentPayload,
  type RenderTaskStatus,
  type TaskDetailResponse,
} from "./lib/taskDetailMappers";

interface EvidencePayload {
  id: string;
  kind: "photo" | "voice" | "note" | "gps" | "checklist_snapshot";
  blob_hash: string | null;
  note_md: string | null;
  created_at: string;
}

interface EvidenceListResponse {
  data: EvidencePayload[];
  next_cursor: string | null;
  has_more: boolean;
}

interface CommentListResponse {
  data: CommentPayload[];
  next_cursor: string | null;
  has_more: boolean;
}

interface TaskStatePayload {
  task_id: string;
  state: ApiTaskState;
  completed_at: string | null;
  completed_by_user_id: string | null;
  reason: string | null;
}

type LocalEvidenceStatus = "uploading" | "uploaded" | "failed";

interface LocalEvidence {
  localId: string;
  previewUrl: string;
  fileName: string;
  status: LocalEvidenceStatus;
  evidenceId: string | null;
}

interface ChatMutationContext {
  prev: AgentMessage[];
  optimisticAt: string;
  body: string;
}

interface EvidenceMutationInput {
  file: File;
  localId: string;
}

interface EvidenceMutationContext {
  localId: string;
}

interface ChecklistMutationInput {
  itemId: string;
  checked: boolean;
}

interface ChecklistMutationContext {
  previous: ApiTask | TaskDetailResponse | undefined;
}

function fmtQty(n: number): string {
  const s = n.toFixed(3);
  return s.replace(/\.?0+$/, "");
}

const STATUS_TONE: Record<RenderTaskStatus, "moss" | "sky" | "ghost" | "rust"> = {
  completed: "moss",
  in_progress: "sky",
  pending: "ghost",
  scheduled: "ghost",
  skipped: "rust",
  cancelled: "rust",
  overdue: "rust",
};

export default function TaskDetailPage() {
  const { tid = "" } = useParams();
  const qc = useQueryClient();
  const modalRef = useRef<HTMLDialogElement>(null);
  const previewUrls = useRef<string[]>([]);
  const [skipReason, setSkipReason] = useState("");
  const [chatDraft, setChatDraft] = useState("");
  const [localEvidence, setLocalEvidence] = useState<LocalEvidence[]>([]);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);

  useEffect(() => {
    return () => {
      for (const url of previewUrls.current) URL.revokeObjectURL(url);
      previewUrls.current = [];
    };
  }, []);

  const q = useQuery({
    queryKey: qk.task(tid),
    queryFn: () => fetchJson<TaskDetailResponse>("/api/v1/tasks/" + tid + "/detail"),
    enabled: Boolean(tid),
  });

  const detail = q.data ? normalizeTaskDetail(q.data) : null;
  const propertyId = detail?.task.property_id ?? null;

  const propertiesQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
    enabled: Boolean(propertyId && !detail?.property),
  });

  const evidenceQ = useQuery({
    queryKey: [...qk.task(tid), "evidence"] as const,
    queryFn: () => fetchJson<EvidenceListResponse>("/api/v1/tasks/" + tid + "/evidence"),
    enabled: Boolean(tid),
  });

  const chatQ = useQuery({
    queryKey: qk.agentTaskChat(tid),
    queryFn: () =>
      fetchJson<CommentListResponse>("/api/v1/tasks/" + tid + "/comments").then((page) =>
        page.data.filter((c) => c.deleted_at === null).map(commentToMessage),
      ),
    enabled: Boolean(tid),
  });

  const chatSend = useMutation<AgentMessage, Error, string, ChatMutationContext>({
    mutationFn: async (body: string) => {
      const comment = await fetchJson<CommentPayload>("/api/v1/tasks/" + tid + "/comments", {
        method: "POST",
        body: { body_md: body, attachments: [] },
      });
      return commentToMessage(comment);
    },
    onMutate: async (body) => {
      await qc.cancelQueries({ queryKey: qk.agentTaskChat(tid) });
      const prev = qc.getQueryData<AgentMessage[]>(qk.agentTaskChat(tid)) ?? [];
      const optimisticAt = new Date().toISOString();
      const optimistic: AgentMessage = { at: optimisticAt, kind: "user", body };
      qc.setQueryData<AgentMessage[]>(qk.agentTaskChat(tid), [...prev, optimistic]);
      return { prev, optimisticAt, body };
    },
    onError: (_e, _v, ctx) => {
      if (ctx) qc.setQueryData(qk.agentTaskChat(tid), ctx.prev);
    },
    onSuccess: (message, _body, ctx) => {
      qc.setQueryData<AgentMessage[]>(qk.agentTaskChat(tid), (prev) => {
        const current = prev ?? [];
        return [
          ...current.filter((m) => !(m.at === ctx.optimisticAt && m.body === ctx.body)),
          message,
        ];
      });
    },
    onSettled: () => qc.invalidateQueries({ queryKey: qk.agentTaskChat(tid) }),
  });

  const evidenceUpload = useMutation<EvidencePayload, Error, EvidenceMutationInput, EvidenceMutationContext>({
    mutationFn: ({ file }) => {
      const form = new FormData();
      form.append("kind", "photo");
      form.append("file", file);
      return fetchJson<EvidencePayload>("/api/v1/tasks/" + tid + "/evidence", {
        method: "POST",
        body: form,
      });
    },
    onMutate: ({ localId }) => {
      setEvidenceError(null);
      return { localId };
    },
    onSuccess: (evidence, _input, ctx) => {
      setLocalEvidence((prev) =>
        prev.map((item) =>
          item.localId === ctx.localId
            ? { ...item, status: "uploaded", evidenceId: evidence.id }
            : item,
        ),
      );
      qc.invalidateQueries({ queryKey: [...qk.task(tid), "evidence"] as const });
    },
    onError: (err, _input, ctx) => {
      setEvidenceError(err.message || "Photo upload failed. Try again.");
      setLocalEvidence((prev) =>
        prev.map((item) =>
          item.localId === ctx?.localId ? { ...item, status: "failed" } : item,
        ),
      );
    },
  });

  const complete = useMutation({
    mutationFn: (photoEvidenceIds: string[]) =>
      fetchJson<TaskStatePayload>("/api/v1/tasks/" + tid + "/complete", {
        method: "POST",
        body: { photo_evidence_ids: photoEvidenceIds },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.task(tid) });
      qc.invalidateQueries({ queryKey: qk.today() });
    },
  });

  const skip = useMutation({
    mutationFn: (reason: string) =>
      fetchJson<TaskStatePayload>("/api/v1/tasks/" + tid + "/skip", {
        method: "POST",
        body: { reason_md: reason },
      }),
    onSuccess: () => {
      modalRef.current?.close();
      setSkipReason("");
      qc.invalidateQueries({ queryKey: qk.task(tid) });
      qc.invalidateQueries({ queryKey: qk.today() });
    },
  });

  const checklistToggle = useMutation<
    ApiChecklistItem,
    Error,
    ChecklistMutationInput,
    ChecklistMutationContext
  >({
    mutationFn: ({ itemId, checked }) =>
      fetchJson<ApiChecklistItem>("/api/v1/tasks/" + tid + "/checklist/" + itemId, {
        method: "PATCH",
        body: { checked },
      }),
    onMutate: async ({ itemId, checked }) => {
      await qc.cancelQueries({ queryKey: qk.task(tid) });
      const previous = qc.getQueryData<ApiTask | TaskDetailResponse>(qk.task(tid));
      qc.setQueryData<ApiTask | TaskDetailResponse>(qk.task(tid), (current) =>
        updateChecklistItem(current, itemId, { checked, done: checked }),
      );
      return { previous };
    },
    onError: (_err, _input, ctx) => {
      qc.setQueryData(qk.task(tid), ctx?.previous);
    },
    onSuccess: (row) => {
      qc.setQueryData<ApiTask | TaskDetailResponse>(qk.task(tid), (current) =>
        updateChecklistItem(current, row.id ?? "", row),
      );
      qc.invalidateQueries({ queryKey: qk.today() });
    },
    onSettled: () => qc.invalidateQueries({ queryKey: qk.task(tid) }),
  });

  if (q.isPending) {
    return (
      <>
        <PageHeader title="Task" />
        <section className="phone__section"><Loading /></section>
      </>
    );
  }
  if (q.isError || !detail) {
    return (
      <>
        <PageHeader title="Task" />
        <section className="phone__section">
          <EmptyState variant="quiet">Task unavailable.</EmptyState>
        </section>
      </>
    );
  }

  const { task, instructions } = detail;
  const property =
    detail.property ?? propertiesQ.data?.find((p) => p.id === task.property_id) ?? null;
  const effects = detail.inventory_effects;
  const consumes = effects.filter((e) => e.kind === "consume");
  const produces = effects.filter((e) => e.kind === "produce");
  const terminal =
    task.status === "completed" || task.status === "skipped" || task.status === "cancelled";
  const serverPhotoIds =
    evidenceQ.data?.data.filter((e) => e.kind === "photo").map((e) => e.id) ?? [];
  const localPhotoIds = localEvidence.flatMap((e) =>
    e.status === "uploaded" && e.evidenceId ? [e.evidenceId] : [],
  );
  const photoEvidenceIds = [...new Set([...serverPhotoIds, ...localPhotoIds])];
  const visibleServerPhotoIds = serverPhotoIds.filter((id) => !localPhotoIds.includes(id));
  const uploadingPhoto = localEvidence.some((e) => e.status === "uploading");
  const completeDisabled =
    complete.isPending ||
    uploadingPhoto ||
    (task.photo_evidence === "required" && photoEvidenceIds.length === 0);

  const onEvidenceChange = (event: ChangeEvent<HTMLInputElement>) => {
    const input = event.currentTarget;
    const file = input.files?.[0] ?? null;
    input.value = "";
    if (!file) return;
    const localId = "local-" + Date.now().toString(36) + "-" + localEvidence.length;
    const previewUrl =
      typeof URL.createObjectURL === "function" ? URL.createObjectURL(file) : "";
    if (previewUrl) previewUrls.current.push(previewUrl);
    setLocalEvidence((prev) => [
      ...prev,
      { localId, previewUrl, fileName: file.name, status: "uploading", evidenceId: null },
    ]);
    evidenceUpload.mutate({ file, localId });
  };

  const completeButtonText = complete.isPending
    ? "Completing..."
    : uploadingPhoto
      ? "Uploading photo..."
      : task.photo_evidence === "required" && photoEvidenceIds.length === 0
        ? <><Camera size={18} strokeWidth={1.8} aria-hidden="true" /> Add photo to complete</>
        : task.photo_evidence === "required"
          ? <><Camera size={18} strokeWidth={1.8} aria-hidden="true" /> Complete with photo</>
          : "Mark done";

  return (
    <>
      <PageHeader
        title={task.title}
        overflow={
          terminal
            ? undefined
            : [
                {
                  label: "Skip this task",
                  icon: <SkipForward size={18} strokeWidth={1.8} aria-hidden="true" />,
                  onSelect: () => modalRef.current?.showModal(),
                  destructive: true,
                },
              ]
        }
      />
      <section className="phone__section phone__section--detail">
        {!terminal && (
          <div className="task-detail__sticky">
            <form
              className="task-detail__sticky-form"
              onSubmit={(e) => {
                e.preventDefault();
                if (!completeDisabled) complete.mutate(photoEvidenceIds);
              }}
            >
              <button
                className="btn btn--moss btn--lg"
                type="submit"
                disabled={completeDisabled}
              >
                {completeButtonText}
              </button>
            </form>
          </div>
        )}

        <header className="task-detail__head">
          <div className="task-detail__chips">
            {property ? (
              <Chip tone={property.color}>{property.name}</Chip>
            ) : task.is_personal ? (
              <Chip tone="ghost">Personal</Chip>
            ) : null}
            {task.area && <Chip tone="ghost">{task.area}</Chip>}
            {(task.priority === "high" || task.priority === "urgent") && (
              <Chip tone="rust">{task.priority === "urgent" ? "Urgent" : "High"}</Chip>
            )}
            {task.photo_evidence === "required" ? (
              <Chip tone="sand"><Camera size={12} strokeWidth={1.8} aria-hidden="true" /> required</Chip>
            ) : task.photo_evidence === "optional" ? (
              <Chip tone="ghost" size="sm"><Camera size={12} strokeWidth={1.8} aria-hidden="true" /> optional</Chip>
            ) : null}
            <Chip tone={STATUS_TONE[task.status]} size="sm">
              {task.status.replace("_", " ")}
            </Chip>
          </div>
          <div className="task-detail__meta">
            {task.scheduled_start ? fmtTime(task.scheduled_start) : "Time TBD"} · est. {task.estimated_minutes} min
          </div>
        </header>

      {task.checklist.length > 0 && (
        <div className="checklist">
          <h3 className="section-title section-title--sm">Checklist</h3>
          <ul>
            {task.checklist.map((item, idx) => (
              <li key={item.id ?? idx}>
                <button
                  className={"checklist__item" + (item.done ? " checklist__item--done" : "")}
                  type="button"
                  disabled={terminal || !item.id || checklistToggle.isPending}
                  onClick={() => {
                    if (item.id) checklistToggle.mutate({ itemId: item.id, checked: !item.done });
                  }}
                >
                  <span className="checklist__box" aria-hidden="true"><Check size={12} strokeWidth={2.5} /></span>
                  <span className="checklist__label">{item.label}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {effects.length > 0 && (
        <section className="task-effects">
          <h3 className="section-title section-title--sm">
            {terminal ? "Used / Produced" : "Will use / Will produce"}
          </h3>
          {consumes.length > 0 && (
            <div className="task-effects__group">
              <span className="task-effects__label">Uses</span>
              <ul>
                {consumes.map((e, idx) => {
                  const short =
                    e.on_hand !== null && e.on_hand - e.qty < 0;
                  return (
                    <li key={`c-${idx}`} className={short ? "task-effects__short" : ""}>
                      <strong className="mono">{fmtQty(e.qty)} {e.unit}</strong>
                      <span>{e.item_name}</span>
                      {short && (
                        <Chip tone="rust" size="sm">
                          only {fmtQty(e.on_hand ?? 0)} on hand
                        </Chip>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
          {produces.length > 0 && (
            <div className="task-effects__group">
              <span className="task-effects__label">Produces</span>
              <ul>
                {produces.map((e, idx) => (
                  <li key={`p-${idx}`}>
                    <strong className="mono">{fmtQty(e.qty)} {e.unit}</strong>
                    <span>{e.item_name}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}

      {instructions.length > 0 && (
        <section className="instructions">
          <h3 className="section-title section-title--sm">Instructions</h3>
          {instructions.map((i, idx) => (
            <details key={i.id} className="instruction-card" open={idx === 0}>
              <summary>
                <span className="instruction-card__title">{i.title}</span>
                <Chip tone="ghost" size="sm">
                  {i.scope === "area"
                    ? i.area
                    : i.scope === "property"
                      ? (property?.name ?? "Property")
                      : "House-wide"}
                </Chip>
              </summary>
              <div className="instruction-card__body">{i.body_md}</div>
            </details>
          ))}
        </section>
      )}

      {(task.photo_evidence === "optional" || task.photo_evidence === "required") && (
        <section className="evidence">
          <h3 className="section-title section-title--sm">
            Evidence {task.photo_evidence === "required" && (
              <Chip tone="sand" size="sm">required</Chip>
            )}
          </h3>
          {evidenceError && (
            <p className="evidence__error" role="alert">
              {evidenceError}
            </p>
          )}
          <label className="evidence__picker">
            <input
              type="file"
              accept="image/*"
              capture="environment"
              onChange={onEvidenceChange}
            />
            <span className="evidence__picker-cta"><Camera size={16} strokeWidth={1.8} aria-hidden="true" /> Take photo</span>
            <span className="evidence__picker-sub">or choose from your gallery</span>
          </label>
          {(visibleServerPhotoIds.length > 0 || localEvidence.length > 0) && (
            <ul className="evidence__preview-list" aria-label="Photo evidence">
              {visibleServerPhotoIds.map((id) => (
                <li key={id} className="evidence__preview evidence__preview--uploaded">
                  <span className="evidence__preview-status">Photo uploaded</span>
                </li>
              ))}
              {localEvidence.map((item) => (
                <li
                  key={item.localId}
                  className={"evidence__preview evidence__preview--" + item.status}
                >
                  {item.previewUrl ? (
                    <img src={item.previewUrl} alt="" className="evidence__preview-img" />
                  ) : (
                    <span className="evidence__preview-placeholder" aria-hidden="true">
                      <Camera size={18} strokeWidth={1.8} />
                    </span>
                  )}
                  <span className="evidence__preview-status">
                    {item.status === "uploading"
                      ? "Uploading photo..."
                      : item.status === "uploaded"
                        ? "Photo ready"
                        : "Upload failed"}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <p className="evidence__note-hint muted">
            Anything the manager should know? Tell the assistant below — it'll
            log a note on this task when it matters.
          </p>
        </section>
      )}

      <section className="comments task-chat">
        <h3 className="section-title section-title--sm">Notes (chat)</h3>
        <p className="muted">
          Messages to and from your workspace assistant — scoped to this task.
        </p>
        <ChatLog
          messages={chatQ.data}
          variant="inline"
          ariaLabel="Task conversation with assistant"
        />
        <ChatComposer
          value={chatDraft}
          onChange={setChatDraft}
          onSubmit={(trimmed) => {
            chatSend.mutate(trimmed);
            setChatDraft("");
          }}
          placeholder="Ask about this task or share what you saw…"
          ariaLabel="Message the assistant about this task"
          variant="inline"
        />
      </section>

      {task.status === "completed" && (
        <div className="done-banner">
          <Check size={16} strokeWidth={2.25} aria-hidden="true" /> Completed
        </div>
      )}
      {task.status === "skipped" && (
        <div className="done-banner done-banner--rust">
          <Ban size={16} strokeWidth={2.25} aria-hidden="true" /> Skipped
        </div>
      )}

      <dialog id="skip-modal" className="modal" ref={modalRef}>
        <form
          className="modal__body"
          onSubmit={(e) => { e.preventDefault(); skip.mutate(skipReason); }}
        >
          <h3 className="modal__title">Skip this task?</h3>
          <p className="modal__sub">Give a quick reason so the manager knows. It'll go in the audit log.</p>
          <label className="field">
            <span>Reason</span>
            <AutoGrowTextarea
              required
              placeholder="e.g. Guest still in the room — came back early from their day."
              value={skipReason}
              onChange={(e) => setSkipReason(e.target.value)}
            />
          </label>
          <div className="modal__actions">
            <button
              className="btn btn--ghost"
              type="button"
              onClick={() => modalRef.current?.close()}
            >
              Cancel
            </button>
            <button className="btn btn--rust" type="submit">Skip task</button>
          </div>
        </form>
      </dialog>
      </section>
    </>
  );
}
