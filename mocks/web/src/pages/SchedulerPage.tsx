import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import PageHeader from "@/components/PageHeader";
import DeskPage from "@/components/DeskPage";
import { Loading } from "@/components/common";
import { useRole } from "@/context/RoleContext";
import type {
  SchedulerCalendarPayload,
  ScheduleAssignment,
  ScheduleRulesetSlot,
  SchedulerTaskView,
  SchedulerUserView,
} from "@/types/api";

const WEEKDAYS: { idx: number; short: string; long: string }[] = [
  { idx: 0, short: "Mon", long: "Monday" },
  { idx: 1, short: "Tue", long: "Tuesday" },
  { idx: 2, short: "Wed", long: "Wednesday" },
  { idx: 3, short: "Thu", long: "Thursday" },
  { idx: 4, short: "Fri", long: "Friday" },
  { idx: 5, short: "Sat", long: "Saturday" },
  { idx: 6, short: "Sun", long: "Sunday" },
];

function startOfIsoWeek(d: Date): Date {
  const out = new Date(d);
  out.setHours(0, 0, 0, 0);
  const iso = (out.getDay() + 6) % 7;
  out.setDate(out.getDate() - iso);
  return out;
}

function addDays(d: Date, n: number): Date {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

function fmtIsoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function fmtHeaderDate(d: Date): string {
  return d.toLocaleDateString("en-GB", { month: "short", day: "numeric" });
}

function timeOfTask(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function weekdayOfTask(iso: string): number {
  const d = new Date(iso);
  return (d.getDay() + 6) % 7;
}

interface CellRota {
  assignment: ScheduleAssignment;
  slot: ScheduleRulesetSlot;
}

function SchedulerCell({
  rotas,
  tasks,
  propertyColor,
  scope,
}: {
  rotas: CellRota[];
  tasks: SchedulerTaskView[];
  propertyColor: (pid: string) => string;
  scope: "manager" | "employee" | "client";
}) {
  if (rotas.length === 0 && tasks.length === 0) {
    return <div className="scheduler-cell scheduler-cell--empty">·</div>;
  }
  return (
    <div className="scheduler-cell">
      {rotas.map(({ assignment, slot }) => (
        <div
          key={`rota-${assignment.id}-${slot.id}`}
          className="rota-slot"
          data-property={assignment.property_id}
          style={{ "--rota-tint": propertyColor(assignment.property_id) } as React.CSSProperties}
        >
          <span className="rota-slot__time">
            {slot.starts_local}–{slot.ends_local}
          </span>
        </div>
      ))}
      {tasks.map((t) => (
        <div
          key={`task-${t.id}`}
          className={`rota-task rota-task--${t.status}`}
          data-property={t.property_id}
          style={{ "--rota-tint": propertyColor(t.property_id) } as React.CSSProperties}
        >
          <span className="rota-task__time">{timeOfTask(t.scheduled_start)}</span>
          <span className="rota-task__title">{t.title}</span>
        </div>
      ))}
      {scope !== "client" && rotas.length > 0 && tasks.length === 0 && (
        <span className="rota-slot__warning" title="Rota slot with no task assigned">
          gap
        </span>
      )}
    </div>
  );
}

export default function SchedulerPage() {
  const { role } = useRole();
  const scope: "manager" | "employee" | "client" =
    role === "client" ? "client" : role === "employee" ? "employee" : "manager";

  const [weekStart, setWeekStart] = useState<Date>(() => startOfIsoWeek(new Date()));
  const from = fmtIsoDate(weekStart);
  const to = fmtIsoDate(addDays(weekStart, 6));

  const calQ = useQuery({
    queryKey: qk.schedulerCalendar(from, to),
    queryFn: () =>
      fetchJson<SchedulerCalendarPayload>(
        `/api/v1/scheduler/calendar?from_=${from}&to=${to}`,
      ),
  });

  const { propertyColor, usersToShow, rotasByCell, tasksByCell } = useMemo(() => {
    if (!calQ.data) {
      return {
        propertyColor: () => "var(--moss-soft)",
        usersToShow: [] as SchedulerUserView[],
        rotasByCell: new Map<string, CellRota[]>(),
        tasksByCell: new Map<string, SchedulerTaskView[]>(),
      };
    }
    const palette = [
      "rgba(63, 110, 59, 0.18)",  // moss
      "rgba(217, 164, 65, 0.20)", // sand
      "rgba(176, 74, 39, 0.16)",  // rust
      "rgba(91, 114, 140, 0.18)", // slate
      "rgba(146, 94, 57, 0.18)",  // earth
    ];
    const propertyIndex = new Map<string, number>();
    calQ.data.properties.forEach((p, i) => propertyIndex.set(p.id, i));
    const color = (pid: string): string => {
      const idx = (propertyIndex.get(pid) ?? 0) % palette.length;
      return palette[idx] ?? palette[0]!;
    };

    const slotsById = new Map<string, ScheduleRulesetSlot[]>();
    calQ.data.slots.forEach((s) => {
      const arr = slotsById.get(s.schedule_ruleset_id) ?? [];
      arr.push(s);
      slotsById.set(s.schedule_ruleset_id, arr);
    });

    const rotas = new Map<string, CellRota[]>();
    calQ.data.assignments.forEach((a) => {
      if (!a.schedule_ruleset_id || !a.user_id) return;
      const slots = slotsById.get(a.schedule_ruleset_id) ?? [];
      slots.forEach((slot) => {
        const key = `${a.user_id}|${slot.weekday}`;
        const arr = rotas.get(key) ?? [];
        arr.push({ assignment: a, slot });
        rotas.set(key, arr);
      });
    });

    const tasks = new Map<string, SchedulerTaskView[]>();
    calQ.data.tasks.forEach((t) => {
      if (!t.user_id) return;
      const key = `${t.user_id}|${weekdayOfTask(t.scheduled_start)}`;
      const arr = tasks.get(key) ?? [];
      arr.push(t);
      tasks.set(key, arr);
    });

    const users = calQ.data.users;

    return {
      propertyColor: color,
      usersToShow: users,
      rotasByCell: rotas,
      tasksByCell: tasks,
    };
  }, [calQ.data]);

  const sub =
    scope === "client"
      ? "Who's booked at your properties — week view."
      : scope === "employee"
        ? "Your rota and scheduled tasks for the week."
        : "Who is booked where — rota + materialised tasks (§06).";

  const title = "Scheduler";

  const weekNav = (
    <div className="scheduler-weeknav">
      <button
        type="button"
        className="btn btn--ghost btn--sm"
        onClick={() => setWeekStart((w) => addDays(w, -7))}
      >
        ← Previous
      </button>
      <span className="scheduler-weeknav__label">
        {fmtHeaderDate(weekStart)} – {fmtHeaderDate(addDays(weekStart, 6))}
      </span>
      <button
        type="button"
        className="btn btn--ghost btn--sm"
        onClick={() => setWeekStart(startOfIsoWeek(new Date()))}
      >
        This week
      </button>
      <button
        type="button"
        className="btn btn--ghost btn--sm"
        onClick={() => setWeekStart((w) => addDays(w, 7))}
      >
        Next →
      </button>
    </div>
  );

  const body = (() => {
    if (calQ.isPending) return <Loading />;
    if (!calQ.data) return <p>Failed to load scheduler.</p>;
    if (usersToShow.length === 0) {
      return (
        <div className="panel empty-state">
          No rota data for this workspace yet.{" "}
          {scope === "manager"
            ? "Assign employees to properties and attach a schedule ruleset to see the grid."
            : "Ask your manager to set up the rota."}
        </div>
      );
    }

    return (
      <>
        {weekNav}
        <div className="panel scheduler-grid-panel">
          <div className="scheduler-grid" role="grid">
            <div className="scheduler-grid__header scheduler-grid__header--user">Employee</div>
            {WEEKDAYS.map((wd, i) => (
              <div key={wd.idx} className="scheduler-grid__header">
                <strong>{wd.short}</strong>
                <span className="scheduler-grid__date">
                  {fmtHeaderDate(addDays(weekStart, i))}
                </span>
              </div>
            ))}
            {usersToShow.map((u) => (
              <div key={u.id} className="scheduler-row" role="row">
                <div className="scheduler-row__user">
                  <strong>{u.first_name || "—"}</strong>
                  {scope !== "client" && u.display_name && (
                    <span className="scheduler-row__sub">{u.display_name}</span>
                  )}
                </div>
                {WEEKDAYS.map((wd) => {
                  const key = `${u.id}|${wd.idx}`;
                  const rotas = rotasByCell.get(key) ?? [];
                  const tasks = tasksByCell.get(key) ?? [];
                  return (
                    <SchedulerCell
                      key={key}
                      rotas={rotas}
                      tasks={tasks}
                      propertyColor={propertyColor}
                      scope={scope}
                    />
                  );
                })}
              </div>
            ))}
          </div>
          <div className="scheduler-legend">
            {calQ.data.properties.map((p) => (
              <span
                key={p.id}
                className="scheduler-legend__item"
                style={{ "--rota-tint": propertyColor(p.id) } as React.CSSProperties}
              >
                <span className="scheduler-legend__swatch" aria-hidden />
                {p.name}
              </span>
            ))}
          </div>
          {scope !== "client" && (
            <p className="muted">
              Tip: <em>rota gap</em> markers surface weekday slots with no task yet.
              Managers edit rulesets on the Schedules page; workers request overrides
              via Leave.
            </p>
          )}
        </div>
      </>
    );
  })();

  if (scope === "manager") {
    return <DeskPage title={title} sub={sub}>{body}</DeskPage>;
  }
  return (
    <>
      <PageHeader title={title} sub={sub} />
      <div className="page-stack">{body}</div>
    </>
  );
}
