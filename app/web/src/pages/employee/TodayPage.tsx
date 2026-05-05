import { Link } from "react-router-dom";
import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import {
  enqueueMutation,
  isBrowserOnline,
  subscribeOfflineQueueReplay,
} from "@/lib/offlineQueue";
import { qk } from "@/lib/queryKeys";
import { Camera, Check } from "lucide-react";
import { Chip, EmptyState, Loading, ProgressBar } from "@/components/common";
import PageHeader from "@/components/PageHeader";
import TaskListCard from "@/components/TaskListCard";
import NewTaskButton from "@/components/NewTaskModal";
import { fmtTime } from "@/lib/dates";
import { cap } from "@/lib/strings";
import type { Me, Property, Task } from "@/types/api";
import {
  markCompleted,
  normalizeTodayPayload,
  todayQueryParams,
  type ApiTaskState,
  type TaskListResponse,
  type TodayPayload,
} from "./lib/todayMappers";

interface TaskStatePayload {
  task_id: string;
  state: ApiTaskState;
  completed_at: string | null;
  completed_by_user_id: string | null;
  reason: string | null;
}

type CompleteResult =
  | { queued: false; payload: TaskStatePayload }
  | { queued: true; taskId: string };

interface CompleteContext {
  previous: TodayPayload | undefined;
}

function ctaLabel(t: Task): string {
  if (t.status === "pending") return "Start";
  if (t.photo_evidence === "required") return "Complete with photo";
  return "Mark done";
}

export default function TodayPage() {
  const qc = useQueryClient();
  const me = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });
  const properties = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const today = useQuery({
    queryKey: qk.today(),
    queryFn: () => fetchToday(me.data!),
    enabled: Boolean(me.data),
  });

  useEffect(
    () =>
      subscribeOfflineQueueReplay((entry) => {
        if (entry.kind !== "task.complete") return;
        qc.invalidateQueries({ queryKey: qk.today() });
        qc.invalidateQueries({ queryKey: qk.tasks() });
      }),
    [qc],
  );

  const complete = useMutation<CompleteResult, Error, Task, CompleteContext>({
    mutationFn: async (task) => {
      const path = "/api/v1/tasks/" + task.id + "/complete";
      const body = { photo_evidence_ids: [] };
      if (!isBrowserOnline()) {
        await enqueueMutation({
          kind: "task.complete",
          method: "POST",
          path,
          body,
        });
        return { queued: true, taskId: task.id };
      }
      const payload = await fetchJson<TaskStatePayload>(path, { method: "POST", body });
      return { queued: false, payload };
    },
    onMutate: async (task) => {
      await qc.cancelQueries({ queryKey: qk.today() });
      const previous = qc.getQueryData<TodayPayload>(qk.today());
      qc.setQueryData<TodayPayload>(qk.today(), (current) =>
        current ? markCompleted(current, task.id) : current,
      );
      return { previous };
    },
    onError: (_err, _task, context) => {
      if (context?.previous) qc.setQueryData(qk.today(), context.previous);
    },
    onSuccess: (result) => {
      if (result.queued) return;
      qc.invalidateQueries({ queryKey: qk.today() });
      qc.invalidateQueries({ queryKey: qk.task(result.payload.task_id) });
      qc.invalidateQueries({ queryKey: qk.tasks() });
    },
  });

  const header = (
    <PageHeader
      title="Today"
      sub={me.data ? formatHeaderDate(me.data.today) : null}
      actions={<NewTaskButton />}
    />
  );

  if (me.isPending || properties.isPending || (me.data && today.isPending)) {
    return <>{header}<section className="phone__section"><Loading /></section></>;
  }
  if (me.isError || properties.isError || today.isError || !today.data) {
    return <>{header}<section className="phone__section"><EmptyState>Failed to load.</EmptyState></section></>;
  }

  const { now_task, upcoming, completed } = today.data;
  const propsById = new Map(properties.data.map((p) => [p.id, p]));

  return (
    <>
      {header}
      <section className="phone__section phone__section--hero">
        <h2 className="section-title">Now</h2>
        {now_task ? (
          <NowCard
            task={now_task}
            property={propsById.get(now_task.property_id) ?? null}
            completePending={complete.isPending}
            onComplete={() => complete.mutate(now_task)}
          />
        ) : (
          <EmptyState glyph={<Check size={28} strokeWidth={2} aria-hidden="true" />} variant="celebrate">
            All done for now. Nice work.
          </EmptyState>
        )}
      </section>

      <section className="phone__section">
        <h2 className="section-title">Upcoming today · {upcoming.length}</h2>
        <ul className="task-list">
          {upcoming.length === 0 && (
            <li className="empty-state empty-state--quiet">Nothing else scheduled.</li>
          )}
          {upcoming.map((t) => (
            <li key={t.id}>
              <TaskListCard task={t} property={propsById.get(t.property_id) ?? null} />
            </li>
          ))}
        </ul>
      </section>

      <section className="phone__section">
        <details className="completed-group">
          <summary>
            <span>Completed today</span>
            <Chip tone="ghost" size="sm">{String(completed.length)}</Chip>
          </summary>
          <ul className="task-list">
            {completed.map((t) => (
              <li key={t.id}>
                <TaskListCard task={t} property={propsById.get(t.property_id) ?? null} />
              </li>
            ))}
          </ul>
        </details>
      </section>
    </>
  );
}

function NowCard({
  task,
  property,
  completePending,
  onComplete,
}: {
  task: Task;
  property: Property | null;
  completePending: boolean;
  onComplete: () => void;
}) {
  const doneSteps = task.checklist.filter((i) => i.done).length;
  const total = task.checklist.length;
  const pct = total > 0 ? Math.round((doneSteps / total) * 100) : 0;
  const cls = "task-card task-card--now" + (task.is_personal ? " task-card--personal" : "");
  const body = (
    <>
      <div className="task-card__head">
        {property ? (
          <Chip tone={property.color}>{property.name}</Chip>
        ) : task.is_personal ? (
          <Chip tone="ghost">Personal</Chip>
        ) : null}
        {(task.priority === "high" || task.priority === "urgent") && (
          <Chip tone="rust">{cap(task.priority)} priority</Chip>
        )}
        {task.photo_evidence === "required" && (
          <Chip tone="sand"><Camera size={12} strokeWidth={1.8} aria-hidden="true" /> photo required</Chip>
        )}
        <span className="task-card__when">{fmtTime(task.scheduled_start)} · {task.estimated_minutes} min</span>
      </div>
      <h3 className="task-card__title">{task.title}</h3>
      {task.area && <div className="task-card__meta">{task.area}</div>}
      {total > 0 && (
        <div className="task-card__progress">
          <ProgressBar value={pct} />
          <span className="progress-label">{doneSteps}/{total} steps</span>
        </div>
      )}
    </>
  );

  if (task.photo_evidence === "required") {
    return (
      <Link to={"/task/" + task.id} className={cls}>
        {body}
        <div className="task-card__cta">{ctaLabel(task)} {"->"}</div>
      </Link>
    );
  }

  return (
    <article className={cls}>
      <Link to={"/task/" + task.id} className="task-card__body-link">
        {body}
      </Link>
      <button
        type="button"
        className="task-card__cta"
        disabled={completePending}
        onClick={onComplete}
      >
        {completePending ? "Completing..." : ctaLabel(task)}
      </button>
    </article>
  );
}

async function fetchToday(me: Me): Promise<TodayPayload> {
  const params = todayQueryParams(me);
  const page = await fetchJson<TaskListResponse>("/api/v1/tasks?" + params.toString());
  return normalizeTodayPayload(page, me.now);
}

function formatHeaderDate(today: string): string {
  return new Date(today + "T00:00:00").toLocaleDateString("en-GB", {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
  });
}
