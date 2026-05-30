import type { Booking } from "@/types/api";

export function computePendingState(bookings: Booking[]): {
  allPending: Booking[];
  firstPendingIso: string | null;
  bannerParts: string[];
} {
  // §14 "Pending banner", count of bookings in the visible window
  // that need manager attention. Two buckets: proposal
  // (pending_approval) and self-amend (pending_amend_minutes). The
  // first day with any of either is the scroll target.
  const pendingProposal = bookings.filter((b) => b.status === "pending_approval");
  const pendingAmend = bookings.filter((b) => b.pending_amend_minutes != null);
  const allPending = [...pendingProposal, ...pendingAmend];
  let firstPendingIso: string | null = null;
  for (const booking of allPending) {
    const day = booking.scheduled_start.slice(0, 10);
    if (firstPendingIso === null || day.localeCompare(firstPendingIso) < 0) {
      firstPendingIso = day;
    }
  }
  const bannerParts: string[] = [];
  if (pendingProposal.length > 0) {
    bannerParts.push(`${pendingProposal.length} awaiting manager approval`);
  }
  if (pendingAmend.length > 0) {
    bannerParts.push(
      `${pendingAmend.length} amendment${pendingAmend.length === 1 ? "" : "s"} pending`,
    );
  }
  return { allPending, firstPendingIso, bannerParts };
}
