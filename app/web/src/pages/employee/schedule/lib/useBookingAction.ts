// Shared mutation wiring for the §09 worker booking actions surfaced in
// the day drawer (amend, decline). Both post to
// `POST /api/v1/bookings/{id}/{action}`, invalidate the same schedule +
// bookings caches on success, and surface failures to the worker via the
// global error-toast bus. Extracted so the amend and decline dialogs
// share one payroll-critical success/error contract.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchJson, toDisplayError } from "@/lib/api";
import { publishGlobalErrorToast } from "@/lib/errorToastBus";
import { qk } from "@/lib/queryKeys";
import type { Booking } from "@/types/api";

export type BookingAction = "amend" | "decline";

export function useBookingAction(action: BookingAction, onDone: () => void) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      fetchJson<Booking>(`/api/v1/bookings/${id}/${action}`, { method: "POST", body }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.mySchedulePrefix() });
      qc.invalidateQueries({ queryKey: qk.bookings() });
      onDone();
    },
    // A named onError suppresses the query-client's global mutation toast
    // (see lib/queryClient.ts), so republish it here to keep worker
    // feedback on failed pay-affecting posts.
    onError: (err: unknown) => {
      publishGlobalErrorToast({ error: toDisplayError(err), source: "mutation" });
    },
  });
}
