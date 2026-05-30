// §09 "Ad-hoc bookings" — worker proposes an unscheduled booking
// (swung by for laundry, covered a gap). Always lands with
// `status = pending_approval`; the manager sees it in the queue and
// approves or rejects. The mock implements the minimum viable form;
// the production shell will expand it to match the full §09 body.

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import FormModal, { FormModalField, FormModalGrid } from "@/components/FormModal";
import SearchableSelect from "@/components/SearchableSelect";
import { propertySelectOption } from "@/lib/propertySelectOptions";
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
  const qc = useQueryClient();
  const [propertyId, setPropertyId] = useState<string>("");
  const [starts, setStarts] = useState<string>("09:00");
  const [ends, setEnds] = useState<string>("12:00");
  const [notes, setNotes] = useState<string>("");
  const propertyOptions = useMemo(() => properties.map(propertySelectOption), [properties]);

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
    <FormModal
      open={iso !== null}
      title="Propose ad-hoc booking"
      eyebrow="Schedule change"
      subtitle={`${iso} · Sent to your manager for approval.`}
      formClassName="booking-propose-form"
      onClose={onClose}
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
      actions={
        <>
          <button type="button" className="btn btn--ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn btn--moss" disabled={m.isPending}>
            {m.isPending ? "Submitting…" : "Propose"}
          </button>
        </>
      }
    >
      <SearchableSelect
        label="Property"
        name="property_id"
        requirement="required"
        className="form-modal__field booking-propose-form__field"
        value={propertyId}
        options={propertyOptions}
        onChange={setPropertyId}
        required
      />

      <FormModalGrid className="booking-propose-form__grid">
        <FormModalField label="From" requirement="required" className="booking-propose-form__field">
          <input type="time" value={starts} onChange={(e) => setStarts(e.target.value)} required  aria-label="From"/>
        </FormModalField>
        <FormModalField label="Until" requirement="required" className="booking-propose-form__field">
          <input type="time" value={ends} onChange={(e) => setEnds(e.target.value)} required  aria-label="Until"/>
        </FormModalField>
      </FormModalGrid>

      <FormModalField label="Notes" requirement="optional" className="booking-propose-form__field">
        <input
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Swung by for forgotten laundry…"
         aria-label="Notes"/>
      </FormModalField>
    </FormModal>
  );
}
