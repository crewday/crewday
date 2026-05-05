import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { type ListEnvelope } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type { Me, Property, PropertyClosure, Stay } from "@/types/api";
import {
  fallbackProperty,
  mapReservation,
  type PropertyDetailRow,
  type ReservationRow,
} from "./property/lib/propertyDetailMappers";

interface ClosuresPayload {
  property: Property;
  closures: PropertyClosure[];
  stays: Stay[];
}

interface ClosurePayload {
  id: string;
  property_id: string;
  starts_at: string;
  ends_at: string;
  reason: PropertyClosure["reason"];
  source_ical_feed_id?: string | null;
}

function fmtDayMon(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

function dateOnly(iso: string): string {
  return iso.slice(0, 10);
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
    note: "",
  };
}

interface ClosureFormState {
  id: string | null;
  starts_on: string;
  ends_on: string;
  reason: PropertyClosure["reason"];
}

const REASONS: readonly PropertyClosure["reason"][] = [
  "renovation",
  "owner_stay",
  "seasonal",
  "other",
];

function emptyForm(todayIso: string): ClosureFormState {
  return {
    id: null,
    starts_on: todayIso,
    ends_on: todayIso,
    reason: "renovation",
  };
}

function closureBody(form: ClosureFormState, propertyId: string) {
  return {
    property_id: propertyId,
    unit_id: null,
    starts_at: form.starts_on + "T00:00:00Z",
    ends_at: inclusiveDateToExclusiveEndAt(form.ends_on),
    reason: form.reason,
    source_ical_feed_id: null,
  };
}

async function fetchClosuresPayload(pid: string): Promise<ClosuresPayload> {
  const [properties, propertyRow, closures, reservations] = await Promise.all([
    fetchJson<Property[]>("/api/v1/properties"),
    fetchJson<PropertyDetailRow>("/api/v1/properties/" + pid),
    fetchJson<ListEnvelope<ClosurePayload>>(
      "/api/v1/property_closures?property_id=" + encodeURIComponent(pid) + "&limit=100",
    ),
    fetchJson<ListEnvelope<ReservationRow>>(
      "/api/v1/stays/reservations?property_id=" + encodeURIComponent(pid) + "&limit=100",
    ),
  ]);
  return {
    property: properties.find((p) => p.id === pid) ?? fallbackProperty(propertyRow),
    closures: closures.data.map(mapClosure),
    stays: reservations.data.map(mapReservation),
  };
}

export default function PropertyClosuresPage() {
  // code-health: ignore[nloc] Closure page keeps filter state, create form, and table actions on one promoted route.
  const { pid = "" } = useParams<{ pid: string }>();
  const queryClient = useQueryClient();
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const [form, setForm] = useState<ClosureFormState>(() => emptyForm("2026-04-01"));
  const [formError, setFormError] = useState<string | null>(null);
  const dataQ = useQuery({
    queryKey: qk.propertyClosures(pid),
    queryFn: () => fetchClosuresPayload(pid),
    enabled: pid !== "",
  });
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });

  const saveClosure = useMutation({
    mutationFn: (next: ClosureFormState) => {
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
    onSuccess: async () => {
      setFormError(null);
      dialogRef.current?.close();
      await queryClient.invalidateQueries({ queryKey: qk.propertyClosures(pid) });
    },
    onError: (err) => {
      setFormError(err instanceof Error ? err.message : "Failed to save closure.");
    },
  });

  const deleteClosure = useMutation({
    mutationFn: (id: string) =>
      fetchJson<null>("/api/v1/property_closures/" + id, { method: "DELETE" }),
    onSuccess: async () => {
      setFormError(null);
      dialogRef.current?.close();
      await queryClient.invalidateQueries({ queryKey: qk.propertyClosures(pid) });
    },
    onError: (err) => {
      setFormError(err instanceof Error ? err.message : "Failed to delete closure.");
    },
  });

  function openForm(next: ClosureFormState) {
    setForm(next);
    setFormError(null);
    dialogRef.current?.showModal();
  }

  if (dataQ.isPending || meQ.isPending) {
    return <DeskPage title="Closures"><Loading /></DeskPage>;
  }
  if (!dataQ.data || !meQ.data) {
    return <DeskPage title="Closures">Failed to load.</DeskPage>;
  }

  const { property, closures, stays } = dataQ.data;
  const today = new Date(meQ.data.today);
  const todayDay = today.getDate();
  const todayIso = dateOnly(meQ.data.today);

  const days: number[] = [];
  for (let d = 1; d <= 30; d += 1) days.push(d);

  return (
    <DeskPage
      title={property.name + " — closures"}
      sub={
        <>
          <Link to={"/property/" + property.id} className="link">← Back to property</Link>{" "}
          · iCal "Not available" / "Blocked" events upsert here automatically.
        </>
      }
      actions={
        <button
          type="button"
          className="btn btn--moss"
          onClick={() => openForm(emptyForm(todayIso))}
        >
          + Add closure
        </button>
      }
    >
      <div className="panel">
        <table className="table table--roomy">
          <thead>
            <tr><th>Dates</th><th>Reason</th><th>Note</th><th>Source</th><th></th></tr>
          </thead>
          <tbody>
            {closures.length === 0 ? (
              <tr>
                <td colSpan={5} className="empty-state empty-state--quiet">
                  No closures scheduled.
                </td>
              </tr>
            ) : (
              closures.map((c) => {
                const ical = c.reason === "ical_unavailable";
                return (
                  <tr key={c.id}>
                    <td className="mono">
                      {fmtDayMon(c.starts_on)} → {fmtDayMon(c.ends_on)}
                    </td>
                    <td>
                      <Chip tone={ical ? "sky" : "ghost"} size="sm">{c.reason}</Chip>
                    </td>
                    <td className="table__sub">{c.note}</td>
                    <td>
                      {ical ? (
                        <Chip tone="sky" size="sm">Airbnb / VRBO</Chip>
                      ) : (
                        <Chip tone="ghost" size="sm">manual</Chip>
                      )}
                    </td>
                    <td>
                      {ical ? (
                        <span className="muted">Read-only — edit in Airbnb / VRBO</span>
                      ) : (
                        <button
                          type="button"
                          className="btn btn--sm btn--ghost"
                          onClick={() =>
                            openForm({
                              id: c.id,
                              starts_on: c.starts_on,
                              ends_on: c.ends_on,
                              reason: c.reason,
                            })
                          }
                        >
                          Edit
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head">
          <h2>Calendar view</h2>
          <span className="muted">April 2026</span>
        </header>
        <div className="mini-cal mini-cal--wide">
          {days.map((d) => {
            let closed = false;
            let reason: PropertyClosure["reason"] | null = null;
            for (const c of closures) {
              const cs = new Date(c.starts_on).getDate();
              const ce = new Date(c.ends_on).getDate();
              if (cs <= d && d <= ce) {
                closed = true;
                reason = c.reason;
              }
            }
            const cls = [
              "mini-cal__day",
              closed ? "mini-cal__day--closed" : "",
              d === todayDay ? "mini-cal__day--today" : "",
            ]
              .filter(Boolean)
              .join(" ");
            return (
              <div key={d} className={cls}>
                <span className="mini-cal__num">{d}</span>
                {closed && (
                  <span
                    className="mini-cal__bar mini-cal__bar--closed"
                    title={reason ?? undefined}
                  />
                )}
                {stays.map((s) => {
                  const ci = new Date(s.check_in).getDate();
                  const co = new Date(s.check_out).getDate();
                  if (ci <= d && d <= co) {
                    return (
                      <span
                        key={s.id}
                        className={"mini-cal__bar mini-cal__bar--" + property.color}
                        title={s.guest_name + " (" + s.source + ")"}
                      />
                    );
                  }
                  return null;
                })}
              </div>
            );
          })}
        </div>
      </div>

      <dialog className="modal" ref={dialogRef} onClose={() => setFormError(null)}>
        <form
          className="modal__body"
          onSubmit={(event) => {
            event.preventDefault();
            saveClosure.mutate(form);
          }}
        >
          <h3 className="modal__title">{form.id ? "Edit closure" : "Add closure"}</h3>
          <label>
            <span>Start</span>
            <input
              type="date"
              value={form.starts_on}
              onChange={(event) => setForm((prev) => ({ ...prev, starts_on: event.target.value }))}
              required
            />
          </label>
          <label>
            <span>End</span>
            <input
              type="date"
              value={form.ends_on}
              onChange={(event) => setForm((prev) => ({ ...prev, ends_on: event.target.value }))}
              required
            />
          </label>
          <label>
            <span>Reason</span>
            <select
              value={form.reason}
              onChange={(event) =>
                setForm((prev) => ({
                  ...prev,
                  reason: event.target.value as PropertyClosure["reason"],
                }))
              }
            >
              {REASONS.map((reason) => (
                <option key={reason} value={reason}>{reason}</option>
              ))}
            </select>
          </label>
          {formError && <p className="form-error">{formError}</p>}
          <div className="modal__actions">
            {form.id && (
              <button
                type="button"
                className="btn btn--rust"
                disabled={deleteClosure.isPending || saveClosure.isPending}
                onClick={() => deleteClosure.mutate(form.id ?? "")}
              >
                Delete
              </button>
            )}
            <button type="button" className="btn btn--ghost" onClick={() => dialogRef.current?.close()}>
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn--moss"
              disabled={saveClosure.isPending || deleteClosure.isPending}
            >
              Save
            </button>
          </div>
        </form>
      </dialog>
    </DeskPage>
  );
}
