import { Fragment, useCallback, useMemo } from "react";
import type { CSSProperties } from "react";
import { Loading } from "@/components/common";
import { qk } from "@/lib/queryKeys";
import type { Employee, Leave, Property, PropertyClosure, Stay } from "@/types/api";
import {
  addDays,
  dayLabel,
  isoDate,
  isoWeekday,
  parseIsoDate,
  startOfIsoWeek,
} from "@/pages/employee/schedule/lib/dateHelpers";
import {
  PALETTE,
  PALETTE_SOLID,
  WEEKDAYS,
} from "@/pages/employee/schedule/lib/palette";
import { useInfiniteAgendaCore } from "@/pages/employee/schedule/lib/useInfiniteAgenda";

interface PageStay extends Stay {
  unit_id: string | null;
}

interface StaysPayload {
  stays: PageStay[];
  closures: PropertyClosure[];
  leaves: Leave[];
}

interface StaysCell {
  date: Date;
  iso: string;
  stays: PageStay[];
  turnovers: PageStay[];
  closures: PropertyClosure[];
  leaves: Leave[];
}

type StaysVariant = "phone" | "desktop";

interface InfiniteStaysAgendaProps {
  variant: StaysVariant;
  today: Date;
  todayIso: string;
  properties: Property[];
  employees: Employee[];
  payload: StaysPayload;
  guestNameForStay: (stay: PageStay) => string;
  showLeaveLayer?: boolean;
}

export function InfiniteStaysAgenda(props: InfiniteStaysAgendaProps) {
  // code-health: ignore[nloc] Infinite stays agenda wires one reusable infinite-scroll core to stay-specific renderers.
  const {
    variant,
    today,
    todayIso,
    properties,
    employees,
    payload,
    guestNameForStay,
    showLeaveLayer = true,
  } = props;
  const payloadSignature = useMemo(() => {
    const stays = payload.stays.map((stay) => [
      stay.id,
      stay.property_id,
      stay.unit_id,
      stay.check_in,
      stay.check_out,
      stay.status,
      stay.source,
    ]);
    const closures = payload.closures.map((closure) => [
      closure.id,
      closure.property_id,
      closure.starts_on,
      closure.ends_on,
      closure.reason,
    ]);
    const leaves = payload.leaves.map((leave) => [
      leave.id,
      leave.employee_id,
      leave.starts_on,
      leave.ends_on,
      leave.category,
      leave.approved_at,
    ]);
    return JSON.stringify({ stays, closures, leaves });
  }, [payload]);
  const mergePages = useCallback((pages: StaysPayload[]) => mergeStaysPages(pages), []);
  const buildCells = useCallback((from: Date, days: number, data: StaysPayload) => {
    return buildStaysCells(from, days, data);
  }, []);
  const queryKey = useCallback((mondayIso: string) => {
    return [...qk.stays(), "projection", payloadSignature, mondayIso] as const;
  }, [payloadSignature]);
  const {
    q,
    merged,
    cells,
    containerRef,
    topSentinelRef,
    bottomSentinelRef,
    monthLabel,
    todayInView,
    scrollToToday,
  } = useInfiniteAgendaCore<StaysPayload, StaysPayload, StaysCell>({
    today,
    todayIso,
    queryKey,
    queryFn: async () => payload,
    mergePages,
    buildCells,
  });

  const groups = useMemo(() => groupCells(cells), [cells]);
  const employeesById = useMemo(
    () => new Map(employees.map((employee) => [employee.id, employee])),
    [employees],
  );

  if (q.isPending) {
    return (
      <div ref={containerRef} className={`schedule schedule--${variant} stays-agenda`}>
        <Loading />
      </div>
    );
  }
  if (!merged) {
    return (
      <div ref={containerRef} className={`schedule schedule--${variant} stays-agenda`}>
        <p className="muted">Failed to load stays calendar.</p>
      </div>
    );
  }

  return (
    <div ref={containerRef} className={`schedule schedule--${variant} stays-agenda`}>
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
        <div className="schedule__legend" aria-label="Stays calendar legend">
          {properties.map((property, index) => (
            <span
              key={property.id}
              className="schedule__legend-item"
              style={propertyVars(index)}
            >
              <span className="schedule__legend-swatch" aria-hidden />
              {property.name}
            </span>
          ))}
          <span className="schedule__legend-item stays-legend__item--turnover">
            <span className="schedule__legend-swatch" aria-hidden />
            Turnover
          </span>
          <span className="schedule__legend-item stays-legend__item--closed">
            <span className="schedule__legend-swatch" aria-hidden />
            Closure
          </span>
          {showLeaveLayer ? (
            <span className="schedule__legend-item stays-legend__item--leave">
              <span className="schedule__legend-swatch" aria-hidden />
              Approved leave
            </span>
          ) : null}
        </div>
      </div>

      <div className="schedule__agenda" role="list">
        <div
          ref={topSentinelRef}
          className="schedule__sentinel schedule__sentinel--top"
          aria-hidden
        >
          {q.isFetchingPreviousPage ? (
            <span className="schedule__sentinel-spinner">Loading earlier…</span>
          ) : (
            <span className="schedule__sentinel-hint">Scroll up for past weeks</span>
          )}
        </div>

        {groups.map((group, gi) => (
          <Fragment key={group.weekStartIso}>
            {gi > 0 && (
              <div className="schedule__weekgap" aria-hidden>
                <span>{group.weekLabel}</span>
              </div>
            )}
            {variant === "desktop" ? (
              <StaysWeekGrid
                group={group}
                today={today}
                properties={properties}
                employeesById={employeesById}
                guestNameForStay={guestNameForStay}
                showLeaveLayer={showLeaveLayer}
                hideLabel={gi > 0}
              />
            ) : (
              <StaysPhoneWeek
                group={group}
                today={today}
                properties={properties}
                employeesById={employeesById}
                guestNameForStay={guestNameForStay}
                showLeaveLayer={showLeaveLayer}
              />
            )}
          </Fragment>
        ))}

        <div
          ref={bottomSentinelRef}
          className="schedule__sentinel schedule__sentinel--bot"
          aria-hidden
        >
          {q.isFetchingNextPage ? (
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
}

function StaysWeekGrid(props: {
  group: StaysGroup;
  today: Date;
  properties: Property[];
  employeesById: Map<string, Employee>;
  guestNameForStay: (stay: PageStay) => string;
  showLeaveLayer: boolean;
  hideLabel: boolean;
}) {
  const {
    group,
    today,
    properties,
    employeesById,
    guestNameForStay,
    showLeaveLayer,
    hideLabel,
  } = props;
  return (
    <div className="schedule-week stays-week" role="grid" aria-label={group.weekLabel}>
      {!hideLabel && <div className="schedule-week__label">{group.weekLabel}</div>}
      <div className="schedule-week__header-row">
        {group.cells.map((cell) => {
          const { day } = dayLabel(cell.date);
          return (
            <div key={cell.iso} className="schedule-week__header">
              <strong>{WEEKDAYS[isoWeekday(cell.date)]!.short}</strong>
              <span>{day}</span>
            </div>
          );
        })}
      </div>
      <div className="schedule-week__row">
        {group.cells.map((cell) => (
          <StaysDayCell
            key={cell.iso}
            cell={cell}
            today={today}
            properties={properties}
            employeesById={employeesById}
            guestNameForStay={guestNameForStay}
            showLeaveLayer={showLeaveLayer}
          />
        ))}
      </div>
    </div>
  );
}

function StaysPhoneWeek(props: {
  group: StaysGroup;
  today: Date;
  properties: Property[];
  employeesById: Map<string, Employee>;
  guestNameForStay: (stay: PageStay) => string;
  showLeaveLayer: boolean;
}) {
  const { group, today, properties, employeesById, guestNameForStay, showLeaveLayer } = props;
  return (
    <>
      {group.cells.map((cell) => (
        <div key={cell.iso} role="listitem">
          <StaysDayCell
            cell={cell}
            today={today}
            properties={properties}
            employeesById={employeesById}
            guestNameForStay={guestNameForStay}
            showLeaveLayer={showLeaveLayer}
          />
        </div>
      ))}
    </>
  );
}

function StaysDayCell(props: {
  cell: StaysCell;
  today: Date;
  properties: Property[];
  employeesById: Map<string, Employee>;
  guestNameForStay: (stay: PageStay) => string;
  showLeaveLayer: boolean;
}) {
  const { cell, today, properties, employeesById, guestNameForStay, showLeaveLayer } = props;
  const isToday = isoDate(today) === cell.iso;
  const hasEvents =
    cell.stays.length > 0 ||
    cell.turnovers.length > 0 ||
    cell.closures.length > 0 ||
    (showLeaveLayer && cell.leaves.length > 0);
  const { weekday, day, month } = dayLabel(cell.date);
  return (
    <article
      className={`schedule-day stays-day${isToday ? " schedule-day--today" : ""}${hasEvents ? "" : " schedule-day--empty"}`}
      data-schedule-iso={cell.iso}
      aria-label={`${weekday} ${day} ${month}`}
    >
      <header className="schedule-day__header">
        <span className="schedule-day__wd">{weekday}</span>
        <span className="schedule-day__num">{day}</span>
        <span className="schedule-day__mo">{month}</span>
      </header>
      {hasEvents ? (
        <div className="stays-day__layers">
          {cell.stays.map((stay) => (
            <StaysEvent
              key={`stay-${stay.id}`}
              tone={propertyTone(stay.property_id, properties)}
              label={guestNameForStay(stay)}
              meta={`${propertyName(stay.property_id, properties)} · ${stay.source}`}
              title={`${guestNameForStay(stay)} at ${propertyName(stay.property_id, properties)}`}
            />
          ))}
          {cell.turnovers.map((stay) => (
            <StaysEvent
              key={`turnover-${stay.id}`}
              modifier="turnover"
              label="Turnover"
              meta={propertyName(stay.property_id, properties)}
              title={`Turnover after ${guestNameForStay(stay)}`}
            />
          ))}
          {cell.closures.map((closure) => (
            <StaysEvent
              key={`closure-${closure.id}`}
              modifier="closed"
              label="Closure"
              meta={`${propertyName(closure.property_id, properties)} · ${closure.reason.replace("_", " ")}`}
              title={`Closure: ${closure.reason}`}
            />
          ))}
          {showLeaveLayer
            ? cell.leaves.map((leave) => {
                const employee = employeesById.get(leave.employee_id);
                return (
                  <StaysEvent
                    key={`leave-${leave.id}`}
                    modifier="leave"
                    label={employee?.avatar_initials ?? "Leave"}
                    meta={`${employee?.name ?? "Employee"} · ${leave.category}`}
                    title={`${employee?.name ?? "Employee"} approved leave`}
                  />
                );
              })
            : null}
        </div>
      ) : (
        <div className="schedule-day__empty">
          <span className="schedule-day__empty-label">open</span>
        </div>
      )}
    </article>
  );
}

function StaysEvent(props: {
  tone?: CSSProperties;
  modifier?: "turnover" | "closed" | "leave";
  label: string;
  meta: string;
  title: string;
}) {
  const { tone, modifier, label, meta, title } = props;
  const cls = modifier ? ` stays-day__event stays-day__event--${modifier}` : "stays-day__event";
  return (
    <span className={cls} style={tone} title={title}>
      <strong>{label}</strong>
      <span>{meta}</span>
    </span>
  );
}

interface StaysGroup {
  weekStartIso: string;
  weekLabel: string;
  cells: StaysCell[];
}

function groupCells(cells: StaysCell[]): StaysGroup[] {
  const groups: StaysGroup[] = [];
  for (const cell of cells) {
    const weekStartIso = isoDate(startOfIsoWeek(cell.date));
    const last = groups[groups.length - 1];
    if (!last || last.weekStartIso !== weekStartIso) {
      const weekStart = parseIsoDate(weekStartIso);
      const weekEnd = addDays(weekStart, 6);
      const weekLabel =
        weekStart.toLocaleDateString("en-GB", { day: "numeric", month: "short" }) +
        " – " +
        weekEnd.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
      groups.push({ weekStartIso, weekLabel, cells: [cell] });
    } else {
      last.cells.push(cell);
    }
  }
  return groups;
}

function buildStaysCells(from: Date, days: number, data: StaysPayload): StaysCell[] {
  const cells: StaysCell[] = [];
  for (let i = 0; i < days; i += 1) {
    const date = addDays(from, i);
    const iso = isoDate(date);
    cells.push({
      date,
      iso,
      stays: data.stays.filter((stay) => stay.status !== "cancelled" && stay.check_in <= iso && iso < stay.check_out),
      turnovers: data.stays.filter((stay) => stay.status !== "cancelled" && stay.check_out === iso),
      closures: data.closures.filter((closure) => closure.starts_on <= iso && closure.ends_on >= iso),
      leaves: data.leaves.filter((leave) => leave.starts_on <= iso && leave.ends_on >= iso),
    });
  }
  return cells;
}

function mergeStaysPages(pages: StaysPayload[]): StaysPayload | null {
  if (pages.length === 0) return null;
  return {
    stays: dedupById(pages.flatMap((page) => page.stays)),
    closures: dedupById(pages.flatMap((page) => page.closures)),
    leaves: dedupById(pages.flatMap((page) => page.leaves)),
  };
}

function dedupById<T extends { id: string }>(items: T[]): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const item of items) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    out.push(item);
  }
  return out;
}

function propertyName(propertyId: string, properties: Property[]): string {
  return properties.find((property) => property.id === propertyId)?.name ?? "Property";
}

function propertyTone(propertyId: string, properties: Property[]): CSSProperties {
  const idx = Math.max(0, properties.findIndex((property) => property.id === propertyId));
  return propertyVars(idx);
}

function propertyVars(index: number): CSSProperties {
  return {
    "--rota-tint": PALETTE[index % PALETTE.length],
    "--rota-tint-solid": PALETTE_SOLID[index % PALETTE_SOLID.length],
  } as CSSProperties;
}
