import type { DragEvent, ReactElement } from "react";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Camera, Globe, GripVertical, Hash, Map as MapIcon, Sparkles, Timer } from "lucide-react";

import { Chip, Loading } from "@/components/common";
import DeskPage from "@/components/DeskPage";
import { fetchJson } from "@/lib/api";
import { type ListEnvelope } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import type { ChecklistTemplateItem, TaskPriority, TaskTemplate } from "@/types/task";
import type { WorkRole } from "@/types/employee";

// §08 — decimal qty formatter, trailing zeros trimmed. The wire shape
// is currently integer-only (`inventory_consumption_json: dict[str,
// int]`), but the spec calls for decimals (0.3 bottles of window-
// washer) and the renderer is the same on both sides — so the helper
// stays decimal-aware now to spare a rewrite when storage widens.
function fmtQty(n: number): string {
  const s = n.toFixed(3);
  return s.replace(/\.?0+$/, "");
}

const PRIORITY_TONE: Record<TaskPriority, "ghost" | "sand" | "rust"> = {
  low: "ghost",
  normal: "ghost",
  high: "sand",
  urgent: "rust",
};

const PROPERTY_SCOPE_LABEL: Record<TaskTemplate["property_scope"], string> = {
  any: "Any property",
  one: "One property",
  listed: "Listed properties",
};

const AREA_SCOPE_LABEL: Record<TaskTemplate["area_scope"], string> = {
  any: "Any area",
  one: "One area",
  listed: "Listed areas",
};

const HINTS_MAX_CHARS = 140;

// Coalesce drag-storm reorders into a single PATCH per template. A
// burst of moves (drag + drop, or several arrow-key nudges) lands one
// request after the user pauses; each new move within the window
// resets the timer so the server only sees the final order.
const REORDER_DEBOUNCE_MS = 400;

function truncate(text: string, max: number): string {
  // code-health: ignore[nloc] Lizard misattributes the rest of this TSX module to this two-branch helper.
  if (text.length <= max) return text;
  return `${text.slice(0, max).trimEnd()}…`;
}

// Strip the lightest layer of Markdown so the card preview reads as
// plain prose. Goes no deeper than that — a real Markdown renderer
// belongs in the template-detail drawer (out of scope for the list
// card).
function plainPreview(md: string): string {
  return md
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/_([^_]+)_/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

export default function TemplatesPage() {
  const tplQ = useQuery({
    queryKey: qk.taskTemplates(),
    queryFn: () =>
      fetchJson<ListEnvelope<TaskTemplate>>("/api/v1/task_templates"),
  });
  const rolesQ = useQuery({
    queryKey: qk.workRoles(),
    queryFn: () => fetchJson<ListEnvelope<WorkRole>>("/api/v1/work_roles"),
  });

  if (tplQ.isPending || rolesQ.isPending) {
    return (
      <DeskPage title="Task templates" actions={<button className="btn btn--moss">+ New template</button>}>
        <Loading />
      </DeskPage>
    );
  }
  if (!tplQ.data || !rolesQ.data) {
    return (
      <DeskPage title="Task templates" actions={<button className="btn btn--moss">+ New template</button>}>
        Failed to load.
      </DeskPage>
    );
  }

  const templates = tplQ.data.data;
  const rolesById = new Map(rolesQ.data.data.map((r) => [r.id, r]));

  return (
    <DeskPage
      title="Task templates"
      sub="Reusable definitions. Schedules materialize tasks from these. Edit once, update everywhere."
      actions={<button className="btn btn--moss">+ New template</button>}
    >
      <section className="grid grid--cards">
        {templates.map((tpl) => {
          const role = tpl.role_id ? rolesById.get(tpl.role_id) : undefined;
          const roleLabel = role?.name ?? (tpl.role_id ? "Unknown role" : "Any role");
          const propertyScopeLabel = renderPropertyScope(tpl);
          const areaScopeLabel = renderAreaScope(tpl);
          const desc = plainPreview(tpl.description_md);
          const hints = tpl.llm_hints_md ? plainPreview(tpl.llm_hints_md) : "";
          return (
            <article key={tpl.id} className="tpl-card">
              <header className="tpl-card__head">
                <h3 className="tpl-card__title">{tpl.name}</h3>
                <div className="tpl-card__chips">
                  <Chip tone="ghost" size="sm">{roleLabel}</Chip>
                  <Chip tone={PRIORITY_TONE[tpl.priority]} size="sm">{tpl.priority}</Chip>
                  {tpl.photo_evidence !== "disabled" && (
                    <Chip tone="sky" size="sm">
                      <Camera size={12} strokeWidth={1.8} aria-hidden="true" /> {tpl.photo_evidence}
                    </Chip>
                  )}
                </div>
              </header>
              {desc && <p className="tpl-card__desc">{desc}</p>}
              <div className="tpl-card__meta">
                <span className="tpl-card__duration">
                  <Timer size={14} strokeWidth={1.75} aria-hidden="true" /> {tpl.duration_minutes} min
                </span>
                <span className="tpl-card__scope">
                  <Globe size={12} strokeWidth={1.75} aria-hidden="true" /> {propertyScopeLabel}
                </span>
                <span className="tpl-card__scope">
                  <MapIcon size={12} strokeWidth={1.75} aria-hidden="true" /> {areaScopeLabel}
                </span>
                {tpl.linked_instruction_ids.length > 0 && (
                  <span className="tpl-card__scope">
                    <Hash size={12} strokeWidth={1.75} aria-hidden="true" /> {tpl.linked_instruction_ids.length} linked
                  </span>
                )}
              </div>
              <ChecklistEditor template={tpl} />
              {tpl.inventory_effects.length > 0 && (
                <div className="tpl-card__effects">
                  {tpl.inventory_effects.some((e) => e.kind === "consume") && (
                    <div className="tpl-effect tpl-effect--consume">
                      <span className="tpl-effect__label">Uses</span>
                      <span>
                        {tpl.inventory_effects
                          .filter((e) => e.kind === "consume")
                          .map((e) => `${fmtQty(e.qty)} ${e.item_ref}`)
                          .join(" · ")}
                      </span>
                    </div>
                  )}
                  {tpl.inventory_effects.some((e) => e.kind === "produce") && (
                    <div className="tpl-effect tpl-effect--produce">
                      <span className="tpl-effect__label">Produces</span>
                      <span>
                        {tpl.inventory_effects
                          .filter((e) => e.kind === "produce")
                          .map((e) => `${fmtQty(e.qty)} ${e.item_ref}`)
                          .join(" · ")}
                      </span>
                    </div>
                  )}
                </div>
              )}
              {hints && (
                <p className="tpl-card__hints">
                  <Sparkles size={12} strokeWidth={1.75} aria-hidden="true" />
                  <span>{truncate(hints, HINTS_MAX_CHARS)}</span>
                </p>
              )}
            </article>
          );
        })}
      </section>
    </DeskPage>
  );
}

// Wire the PATCH body the backend's TaskTemplateUpdate expects (full
// replacement of the mutable body). Read-only fields — id,
// workspace_id, timestamps, the projected inventory_effects — are
// stripped because the DTO uses `extra="forbid"`.
function templateUpdateBody(tpl: TaskTemplate): Record<string, unknown> {
  return {
    name: tpl.name,
    description_md: tpl.description_md,
    role_id: tpl.role_id,
    duration_minutes: tpl.duration_minutes,
    property_scope: tpl.property_scope,
    listed_property_ids: tpl.listed_property_ids,
    area_scope: tpl.area_scope,
    listed_area_ids: tpl.listed_area_ids,
    checklist_template_json: tpl.checklist_template_json,
    photo_evidence: tpl.photo_evidence,
    linked_instruction_ids: tpl.linked_instruction_ids,
    priority: tpl.priority,
    auto_shift_from_occurrence: tpl.auto_shift_from_occurrence,
    inventory_consumption_json: tpl.inventory_consumption_json,
    llm_hints_md: tpl.llm_hints_md,
  };
}

function reorder<T>(items: readonly T[], from: number, to: number): T[] {
  if (from === to) return [...items];
  const next = [...items];
  const [moved] = next.splice(from, 1);
  if (moved === undefined) return next;
  next.splice(to, 0, moved);
  return next;
}

interface ChecklistEditorProps {
  template: TaskTemplate;
}

// Drag-to-reorder + keyboard "move up/down" for a template's checklist
// steps. Keeps a local-state copy so the row stays stable across the
// PATCH round-trip; the source of truth is still the React Query cache
// (we mirror back from props on incoming refetches that aren't ours).
function ChecklistEditor({ template }: ChecklistEditorProps): ReactElement | null {
  const queryClient = useQueryClient();
  const [items, setItems] = useState<ChecklistTemplateItem[]>(
    template.checklist_template_json,
  );
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [overIndex, setOverIndex] = useState<number | null>(null);
  const [announcement, setAnnouncement] = useState<string>("");
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRef = useRef<ChecklistTemplateItem[] | null>(null);
  // Pre-burst snapshot of the React Query cache, captured on the
  // first move of a debounce window so onError can roll back the
  // entire burst even after several intermediate optimistic writes.
  const snapshotRef = useRef<ListEnvelope<TaskTemplate> | null>(null);
  // Stable handle to the mutation so the unmount flush below can fire
  // the queued PATCH without re-running the cleanup on every render.
  const flushRef = useRef<(() => void) | null>(null);

  // Re-sync local state when the canonical template changes from
  // outside this component (cross-tab SSE invalidation, an unrelated
  // refetch). We only adopt the upstream order while no debounce is
  // in flight, so the user's in-progress reorder isn't clobbered.
  useEffect(() => {
    if (pendingRef.current !== null) return;
    setItems(template.checklist_template_json);
  }, [template.checklist_template_json]);

  useEffect(() => {
    return () => {
      if (debounceRef.current !== null) {
        clearTimeout(debounceRef.current);
        debounceRef.current = null;
      }
      // Don't strand the user's reorder if they navigate away during
      // the debounce window — fire the queued PATCH so the server
      // sees the final order. The mutation runs against the still-
      // alive QueryClient; the unmounted component is no longer the
      // observer of its result.
      if (flushRef.current) flushRef.current();
    };
  }, []);

  const reorderMu = useMutation<
    TaskTemplate,
    Error,
    {
      next: ChecklistTemplateItem[];
      previous: ListEnvelope<TaskTemplate> | undefined;
    },
    { previous: ListEnvelope<TaskTemplate> | undefined }
  >({
    mutationFn: (vars) =>
      fetchJson<TaskTemplate>(`/api/v1/task_templates/${template.id}`, {
        method: "PATCH",
        body: { ...templateUpdateBody(template), checklist_template_json: vars.next },
      }),
    onMutate: (vars) => ({ previous: vars.previous }),
    onError: (_err, _vars, ctx) => {
      // If a fresh burst is already queued (the user kept moving rows
      // while this PATCH was in flight), don't roll back: the queued
      // PATCH will resend with the user's latest order and rolling
      // back here would discard work the user just did.
      if (pendingRef.current !== null) return;
      // Otherwise roll the cache back to the pre-burst snapshot and
      // mirror that order locally so the rendered list matches the
      // server's reality after a 4xx/5xx.
      if (ctx?.previous) {
        queryClient.setQueryData(qk.taskTemplates(), ctx.previous);
        const original = ctx.previous.data.find((t) => t.id === template.id);
        if (original) setItems(original.checklist_template_json);
      }
    },
    onSettled: () => {
      // Only clear the snapshot if no fresh burst is queued behind
      // this in-flight PATCH; if there is one, it owns the same
      // pre-burst snapshot until its own debounce fires.
      if (pendingRef.current === null) snapshotRef.current = null;
      void queryClient.invalidateQueries({ queryKey: qk.taskTemplates() });
    },
  });

  function writeOrderToCache(next: ChecklistTemplateItem[]): void {
    const cached = queryClient.getQueryData<ListEnvelope<TaskTemplate>>(
      qk.taskTemplates(),
    );
    if (!cached) return;
    queryClient.setQueryData<ListEnvelope<TaskTemplate>>(qk.taskTemplates(), {
      ...cached,
      data: cached.data.map((t) =>
        t.id === template.id ? { ...t, checklist_template_json: next } : t,
      ),
    });
  }

  function fireQueuedPatch(): void {
    const queued = pendingRef.current;
    if (!queued) return;
    // Clear the queue marker as the PATCH transitions from "queued"
    // to "in-flight". onError uses a non-null pendingRef as the
    // signal that a *newer* burst was queued during the round-trip
    // and the rollback should be skipped.
    pendingRef.current = null;
    reorderMu.mutate({
      next: queued,
      previous: snapshotRef.current ?? undefined,
    });
  }

  function commitReorder(next: ChecklistTemplateItem[]): void {
    // First move of a burst: snapshot the cache so we can roll back if
    // the eventual PATCH fails. Subsequent moves in the same burst
    // reuse that snapshot — the optimistic cache + local state are
    // already mid-flight.
    if (snapshotRef.current === null) {
      snapshotRef.current =
        queryClient.getQueryData<ListEnvelope<TaskTemplate>>(qk.taskTemplates()) ??
        null;
    }
    setItems(next);
    writeOrderToCache(next);
    pendingRef.current = next;
    if (debounceRef.current !== null) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      debounceRef.current = null;
      fireQueuedPatch();
    }, REORDER_DEBOUNCE_MS);
    // Refresh the unmount-flush handle so the cleanup effect can fire
    // the latest queued order if the user navigates away mid-window.
    flushRef.current = fireQueuedPatch;
  }

  function move(from: number, to: number): void {
    if (to < 0 || to >= items.length || from === to) return;
    const moved = items[from];
    const next = reorder(items, from, to);
    commitReorder(next);
    // Polite announcement so screen reader users (the explicit reason
    // the move-up/down buttons exist) hear that their action took
    // effect. The text-only label keeps the message short.
    if (moved) {
      setAnnouncement(
        `Moved “${renderChecklistLabel(moved)}” to position ${to + 1} of ${next.length}.`,
      );
    }
  }

  function onDragStart(index: number) {
    return (event: DragEvent<HTMLLIElement>) => {
      setDragIndex(index);
      event.dataTransfer.effectAllowed = "move";
      // Firefox refuses to fire drag events without payload; the value
      // is unused on our side but keeps the API contract honest.
      event.dataTransfer.setData("text/plain", String(index));
    };
  }

  function onDragOver(index: number) {
    return (event: DragEvent<HTMLLIElement>) => {
      if (dragIndex === null) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      if (overIndex !== index) setOverIndex(index);
    };
  }

  function onDrop(index: number) {
    return (event: DragEvent<HTMLLIElement>) => {
      event.preventDefault();
      if (dragIndex !== null && dragIndex !== index) {
        move(dragIndex, index);
      }
      setDragIndex(null);
      setOverIndex(null);
    };
  }

  function onDragEnd(): void {
    setDragIndex(null);
    setOverIndex(null);
  }

  if (items.length === 0) return null;

  return (
    <>
      <ul className="tpl-card__checklist tpl-card__checklist--editable">
      {items.map((c, idx) => {
        const isDragging = dragIndex === idx;
        const isOver = overIndex === idx && dragIndex !== null && dragIndex !== idx;
        const className = [
          "tpl-card__step",
          isDragging ? "tpl-card__step--dragging" : "",
          isOver ? "tpl-card__step--drop-target" : "",
        ]
          .filter(Boolean)
          .join(" ");
        return (
          <li
            key={c.key}
            className={className}
            draggable
            onDragStart={onDragStart(idx)}
            onDragOver={onDragOver(idx)}
            onDrop={onDrop(idx)}
            onDragEnd={onDragEnd}
          >
            <span
              className="tpl-card__step-handle"
              aria-hidden="true"
              title="Drag to reorder"
            >
              <GripVertical size={14} strokeWidth={1.75} />
            </span>
            <span className="checklist__box" aria-hidden="true" />
            <span className="tpl-card__step-body">{renderChecklistLabel(c)}</span>
            {c.required && <Chip tone="rust" size="sm">required</Chip>}
            {c.guest_visible && <Chip tone="moss" size="sm">guest-visible</Chip>}
            {c.rrule && <Chip tone="sand" size="sm">RRULE</Chip>}
            <span className="tpl-card__step-actions">
              <button
                type="button"
                className="tpl-card__step-btn"
                aria-label={`Move "${renderChecklistLabel(c)}" up`}
                disabled={idx === 0}
                onClick={() => move(idx, idx - 1)}
              >
                <ArrowUp size={12} strokeWidth={1.75} aria-hidden="true" />
              </button>
              <button
                type="button"
                className="tpl-card__step-btn"
                aria-label={`Move "${renderChecklistLabel(c)}" down`}
                disabled={idx === items.length - 1}
                onClick={() => move(idx, idx + 1)}
              >
                <ArrowDown size={12} strokeWidth={1.75} aria-hidden="true" />
              </button>
            </span>
          </li>
        );
      })}
      </ul>
      <span
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="sr-only"
      >
        {announcement}
      </span>
    </>
  );
}

function renderChecklistLabel(item: ChecklistTemplateItem): string {
  return item.text || item.key;
}

function renderPropertyScope(tpl: TaskTemplate): string {
  const base = PROPERTY_SCOPE_LABEL[tpl.property_scope];
  if (tpl.property_scope === "any") return base;
  return `${base} (${tpl.listed_property_ids.length})`;
}

function renderAreaScope(tpl: TaskTemplate): string {
  const base = AREA_SCOPE_LABEL[tpl.area_scope];
  if (tpl.area_scope === "any") return base;
  return `${base} (${tpl.listed_area_ids.length})`;
}
