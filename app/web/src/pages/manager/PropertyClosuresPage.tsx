import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation, useParams } from "react-router-dom";
import { CalendarX } from "lucide-react";
import { fetchJson } from "@/lib/api";
import { type ListEnvelope } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import {
  InlineDateField,
  InlineTextField,
  InlineTableForm,
  type InlineTableColumn,
  type InlineTableRow,
} from "@/components/InlineTableForm";
import { Chip, EmptyState, Loading } from "@/components/common";
import type { Me, Property, PropertyClosure, Stay } from "@/types/api";
import PropertyTabs from "./property/PropertyTabs";
import {
  fallbackProperty,
  mapReservation,
  type PropertyDetailRow,
  type ReservationRow,
} from "./property/lib/propertyDetailMappers";

interface ClosuresPayload {
  property: Property | null;
  closures: PropertyClosure[];
  stays: Stay[];
}

interface ClosurePayload {
  id: string;
  property_id: string;
  starts_at: string;
  ends_at: string;
  reason: string;
  source_ical_feed_id?: string | null;
}

interface CalendarDay {
  day: number;
  iso: string;
}

const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function fmtDayMon(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

function dateOnly(iso: string): string {
  return iso.slice(0, 10);
}

function firstDayOfMonth(iso: string): string {
  return iso.slice(0, 7) + "-01";
}

function buildMonthCalendar(anchorIso: string, monthOffset = 0): {
  days: CalendarDay[];
  label: string;
  leadingBlanks: number;
} {
  const anchorYear = Number(anchorIso.slice(0, 4));
  const anchorMonth = Number(anchorIso.slice(5, 7));
  const firstOfMonth = new Date(Date.UTC(anchorYear, anchorMonth - 1 + monthOffset, 1));
  const year = firstOfMonth.getUTCFullYear();
  const month = firstOfMonth.getUTCMonth() + 1;
  const monthPrefix = `${year}-${String(month).padStart(2, "0")}-`;
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const leadingBlanks = (firstOfMonth.getUTCDay() + 6) % 7;
  const days = Array.from({ length: daysInMonth }, (_, index) => {
    const day = index + 1;
    return {
      day,
      iso: `${monthPrefix}${String(day).padStart(2, "0")}`,
    };
  });
  return {
    days,
    label: firstOfMonth.toLocaleDateString("en-GB", {
      month: "long",
      timeZone: "UTC",
      year: "numeric",
    }),
    leadingBlanks,
  };
}

function buildVisibleCalendars(anchorIso: string) {
  return [0, 1, 2].map((offset) => buildMonthCalendar(anchorIso, offset));
}

function closureCoversDate(closure: PropertyClosure, iso: string): boolean {
  return closure.starts_on <= iso && iso <= closure.ends_on;
}

function stayCoversDate(stay: Stay, iso: string): boolean {
  return dateOnly(stay.check_in) <= iso && iso <= dateOnly(stay.check_out);
}

function inclusiveDateToExclusiveEndAt(isoDate: string): string {
  const date = new Date(isoDate + "T00:00:00Z");
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString();
}

function exclusiveEndAtToInclusiveDate(iso: string): string {
  const date = new Date(iso);
  date.setUTCMilliseconds(date.getUTCMilliseconds() - 1);
  return date.toISOString().slice(0, 10);
}

function mapClosure(row: ClosurePayload): PropertyClosure {
  return {
    id: row.id,
    property_id: row.property_id,
    starts_on: dateOnly(row.starts_at),
    ends_on: exclusiveEndAtToInclusiveDate(row.ends_at),
    reason: row.reason,
    source_ical_feed_id: row.source_ical_feed_id ?? null,
    note: "",
  };
}

function isPropertyDetailRow(value: unknown): value is PropertyDetailRow {
  if (!value || typeof value !== "object") return false;
  const row = value as Record<string, unknown>;
  return (
    typeof row.id === "string"
    && typeof row.name === "string"
    && typeof row.kind === "string"
    && typeof row.country === "string"
    && typeof row.timezone === "string"
    && (typeof row.locale === "string" || row.locale === null)
    && (typeof row.client_org_id === "string" || row.client_org_id === null)
    && (typeof row.owner_user_id === "string" || row.owner_user_id === null)
  );
}

interface ClosureDraft {
  id: string | null;
  starts_on: string;
  ends_on: string;
  reason: string;
  source_ical_feed_id: string | null;
}

const MAX_REASON_LENGTH = 120;

function emptyDraft(todayIso: string): ClosureDraft {
  return {
    id: null,
    starts_on: todayIso,
    ends_on: todayIso,
    reason: "Renovation",
    source_ical_feed_id: null,
  };
}

function draftFromClosure(closure: PropertyClosure): ClosureDraft {
  return {
    id: closure.id,
    starts_on: closure.starts_on,
    ends_on: closure.ends_on,
    reason: closure.reason,
    source_ical_feed_id: closure.source_ical_feed_id,
  };
}

function makeCreateRow(todayIso: string): InlineTableRow<ClosureDraft> {
  return {
    id: "closure-create",
    isNew: true,
    editing: true,
    dirty: false,
    draft: emptyDraft(todayIso),
    committedDraft: emptyDraft(todayIso),
    label: "New closure",
  };
}

function closureBody(form: ClosureDraft, propertyId: string) {
  return {
    property_id: propertyId,
    unit_id: null,
    starts_at: form.starts_on + "T00:00:00Z",
    ends_at: inclusiveDateToExclusiveEndAt(form.ends_on),
    reason: form.reason.trim(),
    source_ical_feed_id: null,
  };
}

function isImportedClosure(closure: PropertyClosure): boolean {
  return closure.source_ical_feed_id !== null;
}

function validateClosureDraft(draft: ClosureDraft): string | null {
  if (!draft.starts_on || !draft.ends_on) return "Start and end dates are required.";
  if (draft.ends_on < draft.starts_on) return "End date must be on or after the start date.";
  const trimmedReason = draft.reason.trim();
  if (!trimmedReason) return "Reason is required.";
  if (trimmedReason.length > MAX_REASON_LENGTH) {
    return "Reason must be 120 characters or fewer.";
  }
  return null;
}

function sourceCell(draft: ClosureDraft) {
  if (draft.source_ical_feed_id !== null) {
    return (
      <span className="property-closure-source-chip">
        <Chip tone="sky" size="sm">Airbnb / VRBO iCal</Chip>
      </span>
    );
  }
  return (
    <span className="property-closure-source-chip">
      <Chip tone="ghost" size="sm">manual</Chip>
    </span>
  );
}

function closureSourceLabel(closure: PropertyClosure): string {
  return isImportedClosure(closure) ? "iCal" : "manual";
}

function closureCalendarLabel(closure: PropertyClosure): string {
  return `${closure.reason} closure, ${closureSourceLabel(closure)} source`;
}

function stayCalendarLabel(stay: Stay): string {
  return `${stay.guest_name} stay, ${stay.source} source`;
}

function calendarDayLabel(iso: string, closures: PropertyClosure[], stays: Stay[]): string {
  const entries = [
    ...closures.map(closureCalendarLabel),
    ...stays.map(stayCalendarLabel),
  ];
  if (entries.length === 0) return iso;
  return `${iso}, ${entries.join(", ")}`;
}

function closureRowLabel(row: InlineTableRow<ClosureDraft>): string {
  if (row.isNew) return "New closure";
  return `${row.draft.reason} closure from ${fmtDayMon(row.draft.starts_on)} to ${fmtDayMon(row.draft.ends_on)}`;
}

async function fetchClosuresPayload(pid: string): Promise<ClosuresPayload> {
  const [properties, propertyRow, closures, reservations] = await Promise.all([
    fetchJson<Property[]>("/api/v1/properties"),
    fetchJson<unknown>("/api/v1/properties/" + pid),
    fetchJson<ListEnvelope<ClosurePayload>>(
      "/api/v1/property_closures?property_id=" + encodeURIComponent(pid) + "&limit=100",
    ),
    fetchJson<ListEnvelope<ReservationRow>>(
      "/api/v1/stays/reservations?property_id=" + encodeURIComponent(pid) + "&limit=100",
    ),
  ]);
  const property = properties.find((p) => p.id === pid)
    ?? (isPropertyDetailRow(propertyRow) ? fallbackProperty(propertyRow) : null);
  return {
    property,
    closures: closures.data.map(mapClosure),
    stays: reservations.data.map(mapReservation),
  };
}

export default function PropertyClosuresPage() {
  // code-health: ignore[nloc] Closure page keeps inline row state, mutations, and calendar composition on one promoted route.
  const { pid = "" } = useParams<{ pid: string }>();
  const { pathname } = useLocation();
  const queryClient = useQueryClient();
  const [rowEdits, setRowEdits] = useState<ReadonlyMap<string, InlineTableRow<ClosureDraft>>>(() => new Map());
  const [createRow, setCreateRow] = useState<InlineTableRow<ClosureDraft>>(() => makeCreateRow("2026-04-01"));
  const [showArchivedClosures, setShowArchivedClosures] = useState(false);
  const dataQ = useQuery({
    queryKey: qk.propertyClosures(pid),
    queryFn: () => fetchClosuresPayload(pid),
    enabled: pid !== "",
  });
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const closures = dataQ.data?.closures ?? [];
  const closureById = useMemo(() => new Map(closures.map((closure) => [closure.id, closure])), [closures]);

  useEffect(() => {
    setRowEdits((current) => {
      const next = new Map<string, InlineTableRow<ClosureDraft>>();
      for (const [rowId, row] of current) {
        if (closureById.has(rowId)) next.set(rowId, row);
      }
      return next.size === current.size ? current : next;
    });
  }, [closureById]);

  useEffect(() => {
    if (!meQ.data) return;
    const todayIso = dateOnly(meQ.data.today);
    setCreateRow((row) => row.dirty ? row : {
      ...row,
      draft: emptyDraft(todayIso),
      committedDraft: emptyDraft(todayIso),
    });
  }, [meQ.data]);

  const saveClosure = useMutation({
    mutationFn: (next: ClosureDraft) => {
      const body = closureBody(next, pid);
      if (next.id) {
        return fetchJson<ClosurePayload>("/api/v1/property_closures/" + next.id, {
          method: "PATCH",
          body: {
            unit_id: body.unit_id,
            starts_at: body.starts_at,
            ends_at: body.ends_at,
            reason: body.reason,
            source_ical_feed_id: body.source_ical_feed_id,
          },
        });
      }
      return fetchJson<ClosurePayload>("/api/v1/property_closures", {
        method: "POST",
        body,
      });
    },
    onSuccess: async (_saved, variables) => {
      if (variables.id) {
        setRowEdits((current) => {
          const next = new Map(current);
          next.delete(variables.id ?? "");
          return next;
        });
      } else {
        setCreateRow(makeCreateRow(dateOnly(meQ.data?.today ?? "2026-04-01")));
      }
      await queryClient.invalidateQueries({ queryKey: qk.propertyClosures(pid) });
    },
    onError: (err, variables) => {
      const message = err instanceof Error ? err.message : "Failed to save closure.";
      if (variables.id) {
        setRowEdits((current) => {
          const row = current.get(variables.id ?? "");
          if (!row) return current;
          const next = new Map(current);
          next.set(row.id, { ...row, error: message });
          return next;
        });
        return;
      }
      setCreateRow((row) => ({ ...row, error: message }));
    },
  });

  const deleteClosure = useMutation({
    mutationFn: (id: string) =>
      fetchJson<null>("/api/v1/property_closures/" + id, { method: "DELETE" }),
    onSuccess: async (_saved, id) => {
      setRowEdits((current) => {
        const next = new Map(current);
        next.delete(id);
        return next;
      });
      await queryClient.invalidateQueries({ queryKey: qk.propertyClosures(pid) });
    },
    onError: (err, id) => {
      const message = err instanceof Error ? err.message : "Failed to delete closure.";
      setRowEdits((current) => {
        const existing = current.get(id);
        const closure = closureById.get(id);
        const row = existing ?? (closure ? {
          id,
          draft: draftFromClosure(closure),
          committedDraft: draftFromClosure(closure),
        } : null);
        if (!row) return current;
        const next = new Map(current);
        next.set(id, { ...row, error: message });
        return next;
      });
    },
  });

  const closureColumns = useMemo<InlineTableColumn<ClosureDraft>[]>(() => [
    {
      key: "starts_on",
      header: "Start",
      width: { px: 156 },
      renderRead: ({ row }) => <span className="mono">{fmtDayMon(row.draft.starts_on)}</span>,
      renderEdit: ({ row, update, disabled }) => (
        <InlineDateField
          value={row.draft.starts_on}
          disabled={disabled}
          ariaLabel="Start date"
          onChange={(starts_on) => update({ starts_on })}
        />
      ),
    },
    {
      key: "ends_on",
      header: "End",
      width: { px: 156 },
      renderRead: ({ row }) => <span className="mono">{fmtDayMon(row.draft.ends_on)}</span>,
      renderEdit: ({ row, update, disabled }) => (
        <InlineDateField
          value={row.draft.ends_on}
          disabled={disabled}
          ariaLabel="End date"
          onChange={(ends_on) => update({ ends_on })}
        />
      ),
    },
    {
      key: "reason",
      header: "Reason",
      width: { flex: 2.2, min: 240 },
      renderRead: ({ row }) => (
        <span className="property-closure-reason" title={row.draft.reason}>{row.draft.reason}</span>
      ),
      renderEdit: ({ row, update, disabled }) => (
        <InlineTextField
          value={row.draft.reason}
          disabled={disabled}
          ariaLabel="Reason"
          placeholder="Renovation"
          onChange={(reason) => update({ reason })}
        />
      ),
    },
    {
      key: "source",
      header: "Source",
      width: { px: 150 },
      className: "property-closure-source",
      renderRead: ({ row }) => sourceCell(row.draft),
      renderEdit: ({ row }) => sourceCell(row.draft),
    },
  ], []);

  const todayIsoForArchive = dateOnly(meQ.data?.today ?? "2026-04-01");
  const currentMonthStartIso = firstDayOfMonth(todayIsoForArchive);
  const archivedClosures = useMemo(
    () => closures.filter((closure) => closure.ends_on < currentMonthStartIso),
    [closures, currentMonthStartIso],
  );
  const tableClosures = useMemo(
    () => showArchivedClosures ? closures : closures.filter((closure) => closure.ends_on >= currentMonthStartIso),
    [closures, currentMonthStartIso, showArchivedClosures],
  );
  const archivedClosureCount = archivedClosures.length;
  const archivedClosureNoun = archivedClosureCount === 1 ? "closure" : "closures";

  const rows = useMemo<InlineTableRow<ClosureDraft>[]>(() => tableClosures.map((closure) => {
    const imported = isImportedClosure(closure);
    const baseDraft = draftFromClosure(closure);
    const localRow = rowEdits.get(closure.id);
    return {
      id: closure.id,
      draft: localRow?.draft ?? baseDraft,
      committedDraft: baseDraft,
      editing: localRow?.editing ?? false,
      dirty: localRow?.dirty ?? false,
      disabled: imported,
      saving: (saveClosure.isPending && saveClosure.variables?.id === closure.id)
        || (deleteClosure.isPending && deleteClosure.variables === closure.id),
      error: localRow?.error,
      validation: localRow?.validation,
      meta: imported ? (
        <span>Imported iCal unavailable date. Edit or remove it in Airbnb / VRBO.</span>
      ) : undefined,
    };
  }), [
    deleteClosure.isPending,
    deleteClosure.variables,
    rowEdits,
    saveClosure.isPending,
    saveClosure.variables,
    tableClosures,
  ]);

  const activeCreateRow = useMemo<InlineTableRow<ClosureDraft>>(() => ({
    ...createRow,
    saving: saveClosure.isPending && saveClosure.variables?.id === null,
    meta: closures.length === 0 && !createRow.dirty ? (
      <EmptyState
        icon={CalendarX}
        title="No closures scheduled"
        copy="Blocked dates and owner stays will appear here."
        variant="compact"
      />
    ) : createRow.meta,
  }), [closures.length, createRow, saveClosure.isPending, saveClosure.variables]);

  function patchRow(rowId: string, patch: Partial<ClosureDraft>) {
    if (rowId === createRow.id) {
      setCreateRow((row) => ({
        ...row,
        draft: { ...row.draft, ...patch },
        dirty: true,
        validation: undefined,
        error: undefined,
        meta: undefined,
      }));
      return;
    }

    const closure = closureById.get(rowId);
    if (!closure || isImportedClosure(closure)) return;
    setRowEdits((current) => {
      const existing = current.get(rowId);
      const committedDraft = existing?.committedDraft ?? draftFromClosure(closure);
      const draft = { ...(existing?.draft ?? committedDraft), ...patch };
      const next = new Map(current);
      next.set(rowId, {
        id: rowId,
        draft,
        committedDraft,
        editing: true,
        dirty: true,
        validation: undefined,
        error: undefined,
      });
      return next;
    });
  }

  function editRow(rowId: string) {
    const closure = closureById.get(rowId);
    if (!closure || isImportedClosure(closure)) return;
    setRowEdits((current) => {
      const existing = current.get(rowId);
      const draft = existing?.draft ?? draftFromClosure(closure);
      const next = new Map(current);
      next.set(rowId, {
        id: rowId,
        draft,
        committedDraft: draftFromClosure(closure),
        editing: true,
        dirty: existing?.dirty ?? false,
        validation: existing?.validation,
        error: existing?.error,
      });
      return next;
    });
  }

  function cancelRow(rowId: string) {
    if (rowId === createRow.id) {
      setCreateRow(makeCreateRow(dateOnly(meQ.data?.today ?? "2026-04-01")));
      return;
    }
    setRowEdits((current) => {
      const next = new Map(current);
      next.delete(rowId);
      return next;
    });
  }

  function saveRow(rowId: string) {
    const row = rowId === createRow.id ? createRow : rowEdits.get(rowId);
    if (!row) return;
    const validation = validateClosureDraft(row.draft);
    if (validation) {
      if (rowId === createRow.id) {
        setCreateRow((current) => ({ ...current, dirty: true, validation }));
        return;
      }
      setRowEdits((current) => {
        const existing = current.get(rowId);
        if (!existing) return current;
        const next = new Map(current);
        next.set(rowId, { ...existing, validation });
        return next;
      });
      return;
    }
    saveClosure.mutate(row.draft);
  }

  function deleteRow(rowId: string) {
    const closure = closureById.get(rowId);
    if (!closure || isImportedClosure(closure)) return;
    deleteClosure.mutate(rowId);
  }

  if (dataQ.isPending || meQ.isPending) {
    return <DeskPage title="Property"><Loading /></DeskPage>;
  }
  if (!dataQ.data || !meQ.data || !dataQ.data.property) {
    return <DeskPage title="Property">Failed to load.</DeskPage>;
  }

  const { property, stays } = dataQ.data;
  const todayIso = dateOnly(meQ.data.today);
  const calendars = buildVisibleCalendars(todayIso);
  const calendarRangeLabel = calendars.map((calendar) => calendar.label).join(", ");

  return (
    <DeskPage
      title={property.name}
      sub={property.city + " · " + property.timezone}
    >
      <PropertyTabs
        pathname={pathname}
        propertyId={property.id}
        activeRelatedPage="closures"
      />

      <div className="panel">
        {archivedClosureCount > 0 ? (
          <div className="property-closure-archive-control">
            <span className="property-closure-archive-control__count" role="status" aria-live="polite">
              {archivedClosureCount} past {archivedClosureNoun} {showArchivedClosures ? "shown" : "hidden"}
            </span>
            <button
              type="button"
              className="property-closure-archive-control__button"
              aria-pressed={showArchivedClosures}
              onClick={() => setShowArchivedClosures((current) => !current)}
            >
              {showArchivedClosures
                ? "Hide past closures"
                : `Show ${archivedClosureCount} past ${archivedClosureNoun}`}
            </button>
          </div>
        ) : null}
        <InlineTableForm
          ariaLabel="Property closures"
          columns={closureColumns}
          rows={rows}
          trailingCreateRow={activeCreateRow}
          saveMode="explicit"
          onDraftChange={patchRow}
          onEdit={editRow}
          onDelete={deleteRow}
          onCancel={cancelRow}
          onSave={saveRow}
          getRowLabel={closureRowLabel}
          emptyState={(
            <EmptyState
              icon={CalendarX}
              title="No closures scheduled"
              copy="Blocked dates and owner stays will appear here."
              variant="compact"
            />
          )}
        />
      </div>

      <div className="panel">
        <header className="panel__head">
          <h2>Calendar view</h2>
          <span className="muted">{calendarRangeLabel}</span>
        </header>
        <div className="property-calendar" role="group" aria-label="Three-month property calendar">
          {calendars.map((calendar) => {
            const monthId = "property-calendar-" + calendar.label.replaceAll(" ", "-");
            return (
              <section key={calendar.label} className="property-calendar__month" aria-labelledby={monthId}>
                <h3 id={monthId}>{calendar.label}</h3>
                <div className="mini-cal" role="grid" aria-label={calendar.label + " property calendar"}>
                  {WEEKDAY_LABELS.map((weekday) => (
                    <div key={weekday} className="mini-cal__weekday" role="columnheader">
                      {weekday}
                    </div>
                  ))}
                  {Array.from({ length: calendar.leadingBlanks }, (_, index) => (
                    <div key={"blank-" + index} className="mini-cal__blank" aria-hidden="true" />
                  ))}
                  {calendar.days.map((day) => {
                    const dayClosures = closures.filter((closure) => closureCoversDate(closure, day.iso));
                    const dayStays = stays.filter((stay) => stayCoversDate(stay, day.iso));
                    const dayLabel = calendarDayLabel(day.iso, dayClosures, dayStays);
                    const closed = dayClosures.length > 0;
                    const cls = [
                      "mini-cal__day",
                      closed ? "mini-cal__day--closed" : "",
                      day.iso === todayIso ? "mini-cal__day--today" : "",
                    ]
                      .filter(Boolean)
                      .join(" ");
                    return (
                      <div
                        key={day.iso}
                        className={cls}
                        role="gridcell"
                        aria-label={dayLabel}
                        title={dayLabel === day.iso ? undefined : dayLabel}
                      >
                        <span className="mini-cal__num">{day.day}</span>
                        {dayClosures.map((closure) => (
                          <span
                            key={closure.id}
                            className="mini-cal__closure"
                            role="group"
                            title={closureCalendarLabel(closure)}
                            aria-label={closureCalendarLabel(closure)}
                          >
                            <span className="mini-cal__closure-reason">{closure.reason}</span>
                            <span className="mini-cal__closure-source">{closureSourceLabel(closure)}</span>
                          </span>
                        ))}
                        {dayStays.map((s) => (
                          <span
                            key={s.id}
                            className={"mini-cal__bar mini-cal__bar--" + property.color}
                            title={s.guest_name + " (" + s.source + ")"}
                          />
                        ))}
                      </div>
                    );
                  })}
                </div>
              </section>
            );
          })}
        </div>
      </div>

    </DeskPage>
  );
}
