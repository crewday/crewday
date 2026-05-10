// §09 "Ad-hoc bookings" — worker proposes an unscheduled booking
// (swung by for laundry, covered a gap). Always lands with
// `status = pending_approval`; the manager sees it in the queue and
// approves or rejects. The mock implements the minimum viable form;
// the production shell will expand it to match the full §09 body.

import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import FormField from "@/components/FormField";
import type { Booking } from "@/types/api";

export function BookingProposeDialog({
  iso,
  properties,
  onClose,
}: {
  iso: string | null;
  properties: { id: string; name: string; timezone: string }[];
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const qc = useQueryClient();
  const [propertyId, setPropertyId] = useState<string>("");
  const [starts, setStarts] = useState<string>("09:00");
  const [ends, setEnds] = useState<string>("12:00");
  const [notes, setNotes] = useState<string>("");

  // Re-init only when the dialog OPENS (iso flips from null to a date).
  // We deliberately don't depend on `properties`: once the dialog is
  // open, an SSE-driven `qk.mySchedulePrefix()` invalidation regenerates
  // the merged payload (and hence `properties` array reference) on
  // every event — depending on it would clobber the worker's half-typed
  // form mid-edit. Properties are reachable on first paint (the dialog
  // only opens from a loaded day cell), so the empty fallback below
  // never triggers in practice.
  const propertiesRef = useRef(properties);
  propertiesRef.current = properties;
  useEffect(() => {
    if (iso === null) return;
    setPropertyId(propertiesRef.current[0]?.id ?? "");
    setStarts("09:00");
    setEnds("12:00");
    setNotes("");
    const d = dialogRef.current;
    if (d && !d.open) d.showModal();
    return () => {
      if (d && d.open) d.close();
    };
  }, [iso]);

  const m = useMutation({
    mutationFn: (body: unknown) =>
      fetchJson<Booking>("/api/v1/bookings", {
        method: "POST",
        body,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.mySchedulePrefix() });
      qc.invalidateQueries({ queryKey: qk.bookings() });
      onClose();
    },
  });

  if (!iso) return null;

  return (
    <dialog className="modal modal--sheet sheet-form-dialog" ref={dialogRef} onClose={onClose}>
      <form
        className="booking-propose-form sheet-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (!propertyId || !starts || !ends || ends <= starts) return;
          m.mutate({
            property_id: propertyId,
            scheduled_start: `${iso}T${starts}:00`,
            scheduled_end: `${iso}T${ends}:00`,
            notes_md: notes.trim() || null,
          });
        }}
      >
        <header className="booking-propose-form__head sheet-form__head">
          <div>
            <p className="booking-propose-form__eyebrow sheet-form__eyebrow">Schedule change</p>
            <h3 className="booking-propose-form__title sheet-form__title">Propose ad-hoc booking</h3>
            <p className="booking-propose-form__sub sheet-form__sub">
              {iso} · Sent to your manager for approval.
            </p>
          </div>
          <button
            type="button"
            className="booking-propose-form__close sheet-form__close"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </header>

        <div className="booking-propose-form__body sheet-form__body">
        <FormField label="Property" requirement="required" className="booking-propose-form__field sheet-form__field">
          <select
            value={propertyId}
            onChange={(e) => setPropertyId(e.target.value)}
            required
          >
            {properties.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </FormField>

        <div className="booking-propose-form__grid sheet-form__grid">
          <FormField label="From" requirement="required" className="booking-propose-form__field sheet-form__field">
            <input type="time" value={starts} onChange={(e) => setStarts(e.target.value)} required />
          </FormField>
          <FormField label="Until" requirement="required" className="booking-propose-form__field sheet-form__field">
            <input type="time" value={ends} onChange={(e) => setEnds(e.target.value)} required />
          </FormField>
        </div>

        <FormField label="Notes" requirement="optional" className="booking-propose-form__field sheet-form__field">
          <input
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Swung by for forgotten laundry…"
          />
        </FormField>
        </div>

        <footer className="booking-propose-form__footer sheet-form__footer">
          <button type="button" className="btn btn--ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn btn--moss" disabled={m.isPending}>
            {m.isPending ? "Submitting…" : "Propose"}
          </button>
        </footer>
      </form>
    </dialog>
  );
}
