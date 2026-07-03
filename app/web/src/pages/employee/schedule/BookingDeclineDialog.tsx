// §09 "Worker decline" — a worker unilaterally declines a future
// scheduled booking (sick, double-booked, can't make it). The reason is
// surfaced to the manager, so it is required and entered by hand; there
// is no default. Posts to `POST /bookings/{id}/decline`; the row flips
// to `pending_approval` for the manager to reassign (§14 "Day drawer").

import { useState } from "react";
import FormModal, { FormModalField } from "@/components/FormModal";
import type { Booking } from "@/types/api";
import { fmtHM } from "./lib/bookingHelpers";
import { useBookingAction } from "./lib/useBookingAction";

export function BookingDeclineDialog({
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
    <BookingDeclineDialogForm
      key={booking.id}
      booking={booking}
      propertyLabel={propertyLabel}
      onClose={onClose}
    />
  );
}

function BookingDeclineDialogForm({
  booking,
  propertyLabel,
  onClose,
}: {
  booking: Booking;
  propertyLabel: string;
  onClose: () => void;
}) {
  const [reason, setReason] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);
  const mutation = useBookingAction("decline", onClose);

  const reasonValid = reason.trim().length > 0;

  return (
    <FormModal
      open
      title="Decline booking"
      eyebrow="Can't make it"
      subtitle={
        `${fmtHM(booking.scheduled_start)}–${fmtHM(booking.scheduled_end)}` +
        ` · ${propertyLabel}. Your manager will reassign it.`
      }
      formClassName="decline-dialog"
      noValidate
      onClose={onClose}
      onSubmit={(e) => {
        e.preventDefault();
        if (!reasonValid) {
          setValidationError("Tell your manager why you're declining.");
          return;
        }
        setValidationError(null);
        mutation.mutate({ id: booking.id, body: { reason: reason.trim() } });
      }}
      actions={
        <>
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn--rust" disabled={mutation.isPending}>
            {mutation.isPending ? "Sending…" : "Decline booking"}
          </button>
        </>
      }
    >
      <FormModalField
        label="Reason"
        requirement="required"
        className="decline-dialog__field decline-dialog__reason-field"
      >
        <input
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Off sick today…"
          aria-label="Reason"
          aria-invalid={validationError !== null && !reasonValid}
        />
      </FormModalField>

      {validationError && (
        <p className="decline-dialog__error" role="alert">
          {validationError}
        </p>
      )}
    </FormModal>
  );
}
