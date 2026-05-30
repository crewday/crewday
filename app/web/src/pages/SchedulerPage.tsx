import { Fragment, useCallback, useMemo } from "react";
import type { QueryKey } from "@tanstack/react-query";
import { CalendarDays } from "lucide-react";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import PageHeader from "@/components/PageHeader";
import DeskPage from "@/components/DeskPage";
import { EmptyState, Loading } from "@/components/common";
import { useActiveAppRole, useAuth } from "@/auth";
import {
  addDays,
  isoDate,
  parseIsoDate,
  startOfIsoWeek,
} from "@/pages/employee/schedule/lib/dateHelpers";
import { useInfiniteAgendaCore } from "@/pages/employee/schedule/lib/useInfiniteAgenda";
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

function fmtHeaderDate(d: Date): string {
  return d.toLocaleDateString("en-GB", { month: "short", day: "numeric" });
}

function timeOfTask(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function isoOfTask(iso: string): string {
  return isoDate(new Date(iso));
}

function normalizeSchedulerName(name: string): string {
  return name.trim().replace(/\s+/g, " ");
}

function schedulerRowLabels(
  user: SchedulerUserView,
  scope: "manager" | "employee" | "client",
): { primary: string; secondary: string | null } {
  const firstName = normalizeSchedulerName(user.first_name || "");
  const displayName = normalizeSchedulerName(user.display_name || "");
  const primary = scope === "client" ? firstName || "," : firstName || displayName || ",";
  if (scope === "client") return { primary, secondary: null };

  if (!displayName || displayName.toLocaleLowerCase() === primary.toLocaleLowerCase()) {
    return { primary, secondary: null };
  }
  return { primary, secondary: displayName };
}

interface CellRota {
  assignment: ScheduleAssignment;
  slot: ScheduleRulesetSlot;
}

interface SchedulerDayCell {
  date: Date;
  iso: string;
}

interface SchedulerWeekGroup {
  weekStartIso: string;
  weekLabel: string;
  cells: SchedulerDayCell[];
}

function mergeById<T extends { id: string }>(
  pages: SchedulerCalendarPayload[],
  pick: (page: SchedulerCalendarPayload) => T[],
): T[] {
  const seen = new Map<string, T>();
  pages.forEach((page) => {
    pick(page).forEach((item) => seen.set(item.id, item));
  });
  return Array.from(seen.values());
}

function mergeSchedulerPages(
  pages: SchedulerCalendarPayload[],
): SchedulerCalendarPayload | null {
  if (pages.length === 0) return null;
  const first = pages[0]!;
  const last = pages[pages.length - 1]!;
  return {
    window: { from: first.window.from, to: last.window.to },
    rulesets: mergeById(pages, (page) => page.rulesets),
    slots: mergeById(pages, (page) => page.slots),
    assignments: mergeById(pages, (page) => page.assignments),
    tasks: mergeById(pages, (page) => page.tasks),
    users: mergeById(pages, (page) => page.users),
    properties: mergeById(pages, (page) => page.properties),
  };
}

function buildSchedulerCells(from: Date, days: number): SchedulerDayCell[] {
  return Array.from({ length: days }, (_unused, index) => {
    const date = addDays(from, index);
    return { date, iso: isoDate(date) };
  });
}

function groupSchedulerCells(cells: SchedulerDayCell[]): SchedulerWeekGroup[] {
  const groups: SchedulerWeekGroup[] = [];
  cells.forEach((cell) => {
    const weekStartIso = isoDate(startOfIsoWeek(cell.date));
    const last = groups[groups.length - 1];
    if (last?.weekStartIso === weekStartIso) {
      last.cells.push(cell);
      return;
    }
    const weekStart = parseIsoDate(weekStartIso);
    const weekEnd = addDays(weekStart, 6);
    groups.push({
      weekStartIso,
      weekLabel: `${fmtHeaderDate(weekStart)} - ${fmtHeaderDate(weekEnd)}`,
      cells: [cell],
    });
  });
  return groups;
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
        <span className="rota-slot__warning" title="Scheduled shift with no task assigned">
          No task
        </span>
      )}
    </div>
  );
}

function SchedulerWeekGrid({
  group,
  usersToShow,
  rotasByCell,
  tasksByCell,
  propertyColor,
  scope,
  hideLabel,
}: {
  group: SchedulerWeekGroup;
  usersToShow: SchedulerUserView[];
  rotasByCell: Map<string, CellRota[]>;
  tasksByCell: Map<string, SchedulerTaskView[]>;
  propertyColor: (pid: string) => string;
  scope: "manager" | "employee" | "client";
  hideLabel: boolean;
}) {
  return (
    <div className="panel scheduler-grid-panel">
      {!hideLabel && <div className="scheduler-grid-panel__label">{group.weekLabel}</div>}
      <div className="scheduler-grid" role="grid" aria-label={group.weekLabel}>
        <div className="scheduler-grid__header scheduler-grid__header--user">Employee</div>
        {group.cells.map((cell) => {
          const wd = WEEKDAYS[cell.date.getDay() === 0 ? 6 : cell.date.getDay() - 1]!;
          return (
            <div
              key={cell.iso}
              className="scheduler-grid__header"
              data-scheduler-iso={cell.iso}
            >
              <strong>{wd.short}</strong>
              <span className="scheduler-grid__date">{fmtHeaderDate(cell.date)}</span>
            </div>
          );
        })}
        {usersToShow.map((u) => {
          const labels = schedulerRowLabels(u, scope);
          return (
            <div key={`${group.weekStartIso}-${u.id}`} className="scheduler-row">
              <div className="scheduler-row__user">
                <strong>{labels.primary}</strong>
                {labels.secondary && (
                  <span className="scheduler-row__sub">{labels.secondary}</span>
                )}
              </div>
              {group.cells.map((cell) => {
                const weekday = (cell.date.getDay() + 6) % 7;
                const rotaKey = `${u.id}|${weekday}`;
                const taskKey = `${u.id}|${cell.iso}`;
                return (
                  <SchedulerCell
                    key={`${u.id}-${cell.iso}`}
                    rotas={rotasByCell.get(rotaKey) ?? []}
                    tasks={tasksByCell.get(taskKey) ?? []}
                    propertyColor={propertyColor}
                    scope={scope}
                  />
                );
              })}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function SchedulerPage() {
  const role = useActiveAppRole();
  const auth = useAuth();
  const scope: "manager" | "employee" | "client" =
    role === "client" ? "client" : role === "employee" ? "employee" : "manager";

  const today = useMemo(() => new Date(), []);
  const todayIso = useMemo(() => isoDate(today), [today]);
  const queryKey = useCallback((mondayIso: string): QueryKey => {
    return qk.schedulerCalendar(mondayIso, isoDate(addDays(parseIsoDate(mondayIso), 6)));
  }, []);
  const mergePages = useCallback(
    (pages: SchedulerCalendarPayload[]) => mergeSchedulerPages(pages),
    [],
  );
  const buildCells = useCallback((from: Date, days: number) => {
    return buildSchedulerCells(from, days);
  }, []);
  const {
    q: calQ,
    merged: calendar,
    cells,
    containerRef,
    topSentinelRef,
    bottomSentinelRef,
    monthLabel,
    todayInView,
    scrollToToday,
  } = useInfiniteAgendaCore<
    SchedulerCalendarPayload,
    SchedulerCalendarPayload,
    SchedulerDayCell
  >({
    today,
    todayIso,
    queryKey,
    queryFn: (fromIso) =>
      fetchJson<SchedulerCalendarPayload>(
        `/api/v1/scheduler/calendar?from=${fromIso}&to=${isoDate(addDays(parseIsoDate(fromIso), 6))}`,
      ),
    mergePages,
    buildCells,
    dataAttribute: "schedulerIso",
  });

  const { propertyColor, usersToShow, rotasByCell, tasksByCell } = useMemo(() => {
    if (!calendar) {
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
    calendar.properties.forEach((p, i) => propertyIndex.set(p.id, i));
    const color = (pid: string): string => {
      const idx = (propertyIndex.get(pid) ?? 0) % palette.length;
      return palette[idx] ?? palette[0]!;
    };

    const slotsById = new Map<string, ScheduleRulesetSlot[]>();
    calendar.slots.forEach((s) => {
      const arr = slotsById.get(s.schedule_ruleset_id) ?? [];
      arr.push(s);
      slotsById.set(s.schedule_ruleset_id, arr);
    });

    const rotas = new Map<string, CellRota[]>();
    calendar.assignments.forEach((a) => {
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
    calendar.tasks.forEach((t) => {
      if (!t.user_id) return;
      const key = `${t.user_id}|${isoOfTask(t.scheduled_start)}`;
      const arr = tasks.get(key) ?? [];
      arr.push(t);
      tasks.set(key, arr);
    });

    const users =
      calendar.users.length === 0
      && calendar.assignments.length === 0
      && calendar.tasks.length === 0
      && scope !== "client"
      && auth.user
        ? [
            {
              id: auth.user.user_id,
              first_name: auth.user.display_name.trim().split(/\s+/)[0] ?? "",
              display_name: auth.user.display_name,
            },
          ]
        : calendar.users;

    return {
      propertyColor: color,
      usersToShow: users,
      rotasByCell: rotas,
      tasksByCell: tasks,
    };
  }, [auth.user, calendar, scope]);
  const weekGroups = useMemo(() => groupSchedulerCells(cells), [cells]);

  const sub =
    scope === "client"
      ? "Who's booked at your properties, week view."
      : scope === "employee"
        ? "Your scheduled shifts and tasks for the week."
        : "Who is booked where, with scheduled shifts and assigned tasks.";

  const title = "Scheduler";

  const body = (() => {
    if (calQ.isPending) {
      return (
        <div ref={containerRef} className="schedule schedule--desktop scheduler-agenda">
          <Loading />
        </div>
      );
    }
    if (!calendar) {
      return (
        <div ref={containerRef} className="schedule schedule--desktop scheduler-agenda">
          <p>Failed to load scheduler.</p>
        </div>
      );
    }
    if (usersToShow.length === 0) {
      return (
        <div ref={containerRef} className="panel">
          <EmptyState
            icon={CalendarDays}
            title="No schedule data yet"
            copy={
              scope === "manager"
                ? "Assign employees to properties and set up schedules to see the grid."
                : "Ask your manager to set up your schedule."
            }
            variant="compact"
          />
        </div>
      );
    }

    return (
      <div ref={containerRef} className="schedule schedule--desktop scheduler-agenda">
        <div className="schedule__sticky-top">
          <div className="schedule__monthbar" aria-live="polite" aria-atomic="true">
            <span className="schedule__monthbar-label">{monthLabel}</span>
            {!todayInView && (
              <button type="button" className="schedule__monthbar-jump" onClick={scrollToToday}>
                Today
              </button>
            )}
          </div>
        </div>
        <div className="schedule__intro">
          <div className="schedule__legend" aria-label="Scheduler legend">
            {calendar.properties.map((p) => (
              <span
                key={p.id}
                className="schedule__legend-item"
                style={{ "--rota-tint": propertyColor(p.id) } as React.CSSProperties}
              >
                <span className="schedule__legend-swatch" aria-hidden />
                {p.name}
              </span>
            ))}
          </div>
          {scope !== "client" && (
            <p className="muted schedule__intro-help">
              Tip: <em>No task</em> markers show scheduled shifts that do not have
              assigned work yet. Managers update schedules on the Schedules page;
              workers request time away through Leave.
            </p>
          )}
        </div>

        <div className="schedule__agenda">
          <div
            ref={topSentinelRef}
            className="schedule__sentinel schedule__sentinel--top"
            aria-hidden
          >
            {calQ.isFetchingPreviousPage ? (
              <span className="schedule__sentinel-spinner">Loading earlier…</span>
            ) : (
              <span className="schedule__sentinel-hint">Scroll up for past weeks</span>
            )}
          </div>

          {weekGroups.map((group, groupIndex) => (
            <Fragment key={group.weekStartIso}>
              {groupIndex > 0 && (
                <div className="schedule__weekgap" aria-hidden>
                  <span>{group.weekLabel}</span>
                </div>
              )}
              <SchedulerWeekGrid
                group={group}
                usersToShow={usersToShow}
                rotasByCell={rotasByCell}
                tasksByCell={tasksByCell}
                propertyColor={propertyColor}
                scope={scope}
                hideLabel={groupIndex > 0}
              />
            </Fragment>
          ))}

          <div
            ref={bottomSentinelRef}
            className="schedule__sentinel schedule__sentinel--bot"
            aria-hidden
          >
            {calQ.isFetchingNextPage ? (
              <span className="schedule__sentinel-spinner">Loading next week…</span>
            ) : (
              <span className="schedule__sentinel-hint">Keep scrolling for more</span>
            )}
          </div>
        </div>

        {!todayInView && (
          <button
            type="button"
            className="schedule__today-fab"
            onClick={scrollToToday}
            aria-label="Jump to today"
          >
            Today
          </button>
        )}
      </div>
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
