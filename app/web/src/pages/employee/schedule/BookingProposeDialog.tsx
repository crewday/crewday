// §09 "Ad-hoc bookings", worker proposes an unscheduled booking
// (swung by for laundry, covered a gap). Always lands with
// `status = pending_approval`; the manager sees it in the queue and
// approves or rejects. The mock implements the minimum viable form;
// the production shell will expand it to match the full §09 body.

import { useMemo, useState } from "react";
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
  if (!iso) return null;
  return <BookingProposeDialogForm key={iso} iso={iso} properties={properties} onClose={onClose} />;
}

function BookingProposeDialogForm({
  iso,
  properties,
  onClose,
}: {
  iso: string;
  properties: { id: string; name: string; timezone: string }[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [form, setForm] = useState(() => ({
    propertyId: properties[0]?.id ?? "",
    starts: "09:00",
    ends: "12:00",
    notes: "",
  }));
  const { propertyId, starts, ends, notes } = form;
  const propertyOptions = useMemo(() => properties.map(propertySelectOption), [properties]);

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
        onChange={(nextPropertyId) => setForm((current) => ({ ...current, propertyId: nextPropertyId }))}
        required
      />

      <FormModalGrid className="booking-propose-form__grid">
        <FormModalField label="From" requirement="required" className="booking-propose-form__field">
          <input
            type="time"
            value={starts}
            onChange={(e) => setForm((current) => ({ ...current, starts: e.target.value }))}
            required
            aria-label="From"
          />
        </FormModalField>
        <FormModalField label="Until" requirement="required" className="booking-propose-form__field">
          <input
            type="time"
            value={ends}
            onChange={(e) => setForm((current) => ({ ...current, ends: e.target.value }))}
            required
            aria-label="Until"
          />
        </FormModalField>
      </FormModalGrid>

      <FormModalField label="Notes" requirement="optional" className="booking-propose-form__field">
        <input
          value={notes}
          onChange={(e) => setForm((current) => ({ ...current, notes: e.target.value }))}
          placeholder="Swung by for forgotten laundry…"
          aria-label="Notes"
        />
      </FormModalField>
    </FormModal>
  );
}
