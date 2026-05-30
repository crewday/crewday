// Sticky header chrome for `/schedule` (§14 "Schedule view") + the
// dialogs footer that hosts the day drawer, leave + override modals,
// and the propose-booking dialog.
//
// Split out from `InfiniteScheduleBody` so the body can stay focused
// on the agenda + sentinel rendering; the chrome here renders above
// and below the cells without owning any scroll state.

import { LeaveDialog, OverrideDialog } from "@/components/ScheduleDialogs";
import type { Booking, MySchedulePayload } from "@/types/api";
import { BookingProposeDialog, DayDrawer } from "./DayDrawer";
import type { DayCell } from "./lib/buildCells";
import { isoWeekday, parseIsoDate } from "./lib/dateHelpers";

export function ScheduleBanner({
  allPending,
  bannerParts,
  firstPendingIso,
  onReview,
}: {
  allPending: Booking[];
  bannerParts: string[];
  firstPendingIso: string | null;
  onReview: (iso: string) => void;
}) {
  return (
    <output className="schedule-banner schedule-banner--pending">
      <span className="schedule-banner__text">
        <strong>
          {allPending.length} booking{allPending.length === 1 ? "" : "s"}{" "}
          need{allPending.length === 1 ? "s" : ""} attention
        </strong>
        <span className="schedule-banner__detail"> · {bannerParts.join(" · ")}</span>
      </span>
      {firstPendingIso && (
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => onReview(firstPendingIso)}
        >
          Review
        </button>
      )}
    </output>
  );
}

interface ScheduleDialogsFooterProps {
  data: MySchedulePayload;
  empId: string | null;
  selectedCell: DayCell | null;
  setSelectedIso: (iso: string | null) => void;
  leaveIso: string | null;
  setLeaveIso: (iso: string | null) => void;
  overrideIso: string | null;
  setOverrideIso: (iso: string | null) => void;
  proposeIso: string | null;
  setProposeIso: (iso: string | null) => void;
}

export function ScheduleDialogsFooter(props: ScheduleDialogsFooterProps) {
  const {
    data,
    empId,
    selectedCell,
    setSelectedIso,
    leaveIso,
    setLeaveIso,
    overrideIso,
    setOverrideIso,
    proposeIso,
    setProposeIso,
  } = props;
  return (
    <>
      <DayDrawer
        cell={selectedCell}
        data={data}
        onClose={() => setSelectedIso(null)}
        onRequestLeave={(iso) => { setSelectedIso(null); setLeaveIso(iso); }}
        onRequestOverride={(iso) => { setSelectedIso(null); setOverrideIso(iso); }}
        onProposeBooking={(iso) => { setSelectedIso(null); setProposeIso(iso); }}
      />
      <OverrideDialog
        iso={overrideIso}
        employeeId={empId}
        pattern={
          overrideIso
            ? (data.weekly_availability.find(
                (w) => w.weekday === isoWeekday(parseIsoDate(overrideIso)),
              ) ?? null)
            : null
        }
        onClose={() => setOverrideIso(null)}
      />
      <LeaveDialog
        iso={leaveIso}
        employeeId={empId}
        onClose={() => setLeaveIso(null)}
      />
      <BookingProposeDialog
        iso={proposeIso}
        properties={data.properties}
        onClose={() => setProposeIso(null)}
      />
    </>
  );
}
