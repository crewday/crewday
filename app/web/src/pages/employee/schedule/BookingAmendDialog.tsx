// §09 "Amend operation" — the worker records what really happened on a
// booking (overrun, underrun, or a close-out correction) via a proper
// time-and-reason form (§14 "Day drawer" → Amend). The minutes field is
// prefilled with the booking's current computed minutes so the worker
// adjusts from the true scheduled value; the reason is always entered by
// hand. The server applies §09 auto-approve gating — we just post what
// the worker typed to `POST /bookings/{id}/amend`.

import { useState } from "react";
import FormModal, { FormModalField } from "@/components/FormModal";
import type { Booking } from "@/types/api";
import { bookingMinutes, fmtDuration, fmtHM } from "./lib/bookingHelpers";
import { useBookingAction } from "./lib/useBookingAction";

// A single day's booking can't sanely run past 24h; anything above is a
// typo, not a real overrun, and would poison payroll.
const MIN_MINUTES = 1;
const MAX_MINUTES = 24 * 60;

export function BookingAmendDialog({
  booking,
  propertyLabel,
  onClose,
}: {
  booking: Booking | null;
  propertyLabel: string;
  onClose: () => void;
}) {
  if (!booking) return null;
  return (
    <BookingAmendDialogForm
      key={booking.id}
      booking={booking}
      propertyLabel={propertyLabel}
      onClose={onClose}
    />
  );
}

function BookingAmendDialogForm({
  booking,
  propertyLabel,
  onClose,
}: {
  booking: Booking;
  propertyLabel: string;
  onClose: () => void;
}) {
  const scheduledMinutes = bookingMinutes(booking);
  const [minutes, setMinutes] = useState(() => String(scheduledMinutes));
  const [reason, setReason] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);
  const mutation = useBookingAction("amend", onClose);

  const parsedMinutes = Number(minutes);
  const minutesValid =
    minutes.trim() !== "" &&
    Number.isInteger(parsedMinutes) &&
    parsedMinutes >= MIN_MINUTES &&
    parsedMinutes <= MAX_MINUTES;
  const reasonValid = reason.trim().length > 0;

  return (
    <FormModal
      open
      title="Amend booking"
      eyebrow="Time worked"
      subtitle={
        `${fmtHM(booking.scheduled_start)}–${fmtHM(booking.scheduled_end)}` +
        ` · ${propertyLabel}. Scheduled ${fmtDuration(scheduledMinutes)}.`
      }
      formClassName="amend-dialog"
      noValidate
      onClose={onClose}
      onSubmit={(e) => {
        e.preventDefault();
        if (!minutesValid) {
          setValidationError(`Enter the minutes worked (${MIN_MINUTES}–${MAX_MINUTES}).`);
          return;
        }
        if (!reasonValid) {
          setValidationError("Add a reason so your manager knows why the time changed.");
          return;
        }
        setValidationError(null);
        mutation.mutate({
          id: booking.id,
          body: { actual_minutes: parsedMinutes, reason: reason.trim() },
        });
      }}
      actions={
        <>
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn--moss" disabled={mutation.isPending}>
            {mutation.isPending ? "Sending…" : "Submit amend"}
          </button>
        </>
      }
    >
      <FormModalField
        label="Minutes worked"
        requirement="required"
        className="amend-dialog__field amend-dialog__minutes-field"
      >
        <input
          type="number"
          inputMode="numeric"
          min={MIN_MINUTES}
          max={MAX_MINUTES}
          step={1}
          value={minutes}
          onChange={(e) => setMinutes(e.target.value)}
          aria-label="Minutes worked"
          aria-invalid={validationError !== null && !minutesValid}
        />
      </FormModalField>

      <FormModalField
        label="Reason"
        requirement="required"
        className="amend-dialog__field amend-dialog__reason-field"
      >
        <input
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Stayed on to finish the deep clean…"
          aria-label="Reason"
          aria-invalid={validationError !== null && minutesValid && !reasonValid}
        />
      </FormModalField>

      {validationError && (
        <p className="amend-dialog__error" role="alert">
          {validationError}
        </p>
      )}
    </FormModal>
  );
}
