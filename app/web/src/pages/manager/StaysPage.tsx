import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useMemo, useRef, useState } from "react";
import { ApiError, fetchJson } from "@/lib/api";
import { type ListEnvelope } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import FormField from "@/components/FormField";
import { Chip, Loading } from "@/components/common";
import { useWorkspace } from "@/context/WorkspaceContext";
import type {
  Employee,
  Leave,
  PropertyWorkspace,
  Property,
  PropertyClosure,
  Stay,
} from "@/types/api";
import type { AuthMe } from "@/auth/types";
import { isoDate } from "@/pages/employee/schedule/lib/dateHelpers";
import { useIsPhone } from "@/pages/employee/schedule/lib/useIsPhone";
import { InfiniteStaysAgenda } from "./stays/InfiniteStaysAgenda";

type IcalProvider = "airbnb" | "vrbo" | "booking" | "gcal" | "generic";
type StaySource = Stay["source"];
type StayStatus = Stay["status"];

interface PageStay extends Stay {
  unit_id: string | null;
}

interface StaysPayload {
  stays: PageStay[];
  closures: PropertyClosure[];
  leaves: Leave[];
}

interface ReservationPayload {
  id: string;
  property_id: string;
  unit_id?: string | null;
  check_in: string;
  check_out: string;
  guest_name: string | null;
  guest_count: number | null;
  status: string;
  source: string;
}

interface UnitPayload {
  id: string;
  property_id: string;
  name: string;
}

interface MembershipPayload {
  property_id: string;
  workspace_id: string;
  label: string;
  membership_role: PropertyWorkspace["membership_role"];
  share_guest_identity: boolean;
  created_at: string;
}

interface IcalFeedPayload {
  id: string;
  property_id: string;
  unit_id: string | null;
  provider: string;
  provider_override: string | null;
  url_preview: string;
  enabled: boolean;
  poll_cadence: string;
  last_polled_at: string | null;
  last_error: string | null;
}

interface LeaveListPayload {
  id: string;
  user_id: string;
  starts_on: string;
  ends_on: string;
  category: string;
  note_md: string | null;
  approved_at: string | null;
}

const STAY_TONE: Record<Stay["status"], "sky" | "moss" | "ghost" | "rust" | "sand"> = {
  tentative: "sand",
  confirmed: "sky",
  in_house: "moss",
  checked_out: "ghost",
  cancelled: "rust",
};

const PROVIDERS: { value: IcalProvider; label: string }[] = [
  { value: "airbnb", label: "Airbnb" },
  { value: "vrbo", label: "VRBO" },
  { value: "booking", label: "Booking.com" },
  { value: "gcal", label: "Google Calendar" },
  { value: "generic", label: "Generic ICS" },
];

const manualNoticeId = "manual-stay-form-notice";
const manualPrivacyId = "manual-stay-privacy-note";
const manualOverlapId = "manual-stay-overlap-note";
const manualUnitErrorId = "manual-stay-unit-error";
const icalNoticeId = "ical-feed-form-notice";
const icalDuplicateId = "ical-feed-duplicate-note";
const icalUnitErrorId = "ical-feed-unit-error";

interface ManualStayForm {
  propertyId: string;
  unitId: string;
  guestName: string;
  guestCount: string;
  checkIn: string;
  checkOut: string;
  status: StayStatus;
}

interface IcalForm {
  propertyId: string;
  unitId: string;
  provider: IcalProvider;
  url: string;
}

interface FormNotice {
  tone: "success" | "error";
  text: string;
}

function fmtAbbrevDate(iso: string): string {
  // code-health: ignore[ccn nloc] Tiny date formatter is a lizard TS parser artifact.
  return new Date(iso).toLocaleDateString("en-GB", {
    weekday: "short",
    day: "2-digit",
    month: "short",
  });
}

function mapStatus(status: string): Stay["status"] {
  if (status === "cancelled") return "cancelled";
  if (status === "scheduled") return "confirmed";
  if (status === "checked_in") return "in_house";
  if (status === "completed") return "checked_out";
  if (status === "tentative" || status === "confirmed" || status === "in_house" || status === "checked_out") {
    return status;
  }
  return "confirmed";
}

function mapSource(source: string): Stay["source"] {
  if (source === "api") return "manual";
  if (source === "gcal") return "google_calendar";
  if (source === "manual" || source === "airbnb" || source === "vrbo" || source === "booking" || source === "google_calendar" || source === "ical") {
    return source;
  }
  return "ical";
}

function dateOnly(iso: string): string {
  return iso.slice(0, 10);
}

function mapReservation(row: ReservationPayload): PageStay {
  return {
    id: row.id,
    property_id: row.property_id,
    unit_id: row.unit_id ?? null,
    guest_name: row.guest_name ?? "Guest",
    source: mapSource(row.source),
    check_in: dateOnly(row.check_in),
    check_out: dateOnly(row.check_out),
    guests: row.guest_count ?? 0,
    status: mapStatus(row.status),
  };
}

function mapLeaveCategory(kind: string): Leave["category"] {
  if (kind === "vacation" || kind === "sick" || kind === "personal" || kind === "bereavement" || kind === "other") {
    return kind;
  }
  return "other";
}

function mapLeave(row: LeaveListPayload): Leave {
  return {
    id: row.id,
    employee_id: row.user_id,
    starts_on: dateOnly(row.starts_on),
    ends_on: dateOnly(row.ends_on),
    category: mapLeaveCategory(row.category),
    note: row.note_md ?? "",
    approved_at: row.approved_at,
  };
}

function mapMembership(row: MembershipPayload): PropertyWorkspace {
  return {
    property_id: row.property_id,
    workspace_id: row.workspace_id,
    membership_role: row.membership_role,
    share_guest_identity: row.share_guest_identity,
    invite_id: null,
    added_at: row.created_at,
    added_by_user_id: null,
    added_via: "system",
  };
}

type StaysPayloadInput = Omit<StaysPayload, "leaves"> & { leaves?: unknown };

function normalizeStaysPayload(payload: StaysPayloadInput): StaysPayload {
  return {
    ...payload,
    leaves: Array.isArray(payload.leaves) ? payload.leaves : [],
  };
}

function leafRowsFromEnvelope(envelope: unknown): LeaveListPayload[] {
  if (typeof envelope !== "object" || envelope === null || !("data" in envelope)) return [];
  const data = envelope.data;
  return Array.isArray(data) ? (data as LeaveListPayload[]) : [];
}

async function fetchStaysPayload(): Promise<StaysPayload> {
  const [reservations, leaves] = await Promise.all([
    fetchJson<ListEnvelope<ReservationPayload>>("/api/v1/stays/reservations?limit=500"),
    fetchJson<unknown>("/api/v1/user_leaves?approved=true&limit=500"),
  ]);
  return {
    stays: reservations.data.map(mapReservation),
    closures: [],
    leaves: leafRowsFromEnvelope(leaves).map(mapLeave),
  };
}

async function fetchPropertyUnits(propertyId: string): Promise<UnitPayload[]> {
  const rows = await fetchJson<ListEnvelope<UnitPayload>>(
    "/api/v1/properties/" + encodeURIComponent(propertyId) + "/units?limit=100",
  );
  return rows.data;
}

async function fetchPropertyMemberships(propertyId: string): Promise<PropertyWorkspace[]> {
  const rows = await fetchJson<ListEnvelope<MembershipPayload>>(
    "/api/v1/properties/" + encodeURIComponent(propertyId) + "/share",
  );
  return rows.data.map(mapMembership);
}

async function fetchIcalFeeds(): Promise<IcalFeedPayload[]> {
  return fetchJson<IcalFeedPayload[]>("/api/v1/stays/ical-feeds");
}

function initialManualForm(properties: Property[], units: UnitPayload[]): ManualStayForm {
  // code-health: ignore[ccn] Initial form factory has two defaulting branches; lizard over-counts the surrounding TSX module.
  const propertyId = properties[0]?.id ?? "";
  const propertyUnits = units.filter((unit) => unit.property_id === propertyId);
  return {
    propertyId,
    unitId: propertyUnits[0]?.id ?? "",
    guestName: "",
    guestCount: "1",
    checkIn: "",
    checkOut: "",
    status: "confirmed",
  };
}

function initialIcalForm(properties: Property[], units: UnitPayload[]): IcalForm {
  const propertyId = properties[0]?.id ?? "";
  const propertyUnits = units.filter((unit) => unit.property_id === propertyId);
  return {
    propertyId,
    unitId: propertyUnits[0]?.id ?? "",
    provider: "airbnb",
    url: "",
  };
}

function providerLabel(provider: string): string {
  return PROVIDERS.find((entry) => entry.value === provider)?.label ?? provider;
}

function overlapWarning(
  form: ManualStayForm,
  stays: PageStay[],
  guestNameForStay: (stay: PageStay) => string,
): string | null {
  if (!form.propertyId || !form.checkIn || !form.checkOut) return null;
  const checkIn = Date.parse(form.checkIn);
  const checkOut = Date.parse(form.checkOut);
  if (!Number.isFinite(checkIn) || !Number.isFinite(checkOut) || checkIn >= checkOut) return null;
  const overlap = stays.find((stay) => {
    if (stay.property_id !== form.propertyId || stay.status === "cancelled") return false;
    if (stay.unit_id && stay.unit_id !== form.unitId) return false;
    return Date.parse(stay.check_in) < checkOut && checkIn < Date.parse(stay.check_out);
  });
  if (!overlap) return null;
  const arrival = fmtAbbrevDate(overlap.check_in);
  const departure = fmtAbbrevDate(overlap.check_out);
  return `Overlaps ${guestNameForStay(overlap)} from ${arrival} to ${departure}. The server may still allow the stay and mark the conflict.`;
}

function validateManualForm(form: ManualStayForm, canShareGuestName: boolean): string | null {
  // code-health: ignore[ccn nloc] Linear validation copy stays explicit so each manager-facing error remains local and stable.
  if (!form.propertyId) return "Pick a property.";
  if (!form.unitId) return "Pick a unit before creating a stay.";
  if (!form.checkIn || !form.checkOut) return "Enter check-in and check-out dates.";
  if (form.checkIn >= form.checkOut) return "Check-out must be after check-in.";
  const guestCount = Number.parseInt(form.guestCount, 10);
  if (!Number.isFinite(guestCount) || guestCount < 1) return "Guest count must be at least 1.";
  if (canShareGuestName && !form.guestName.trim()) return "Guest name is required for this property.";
  return null;
}

function validateIcalForm(form: IcalForm): string | null {
  if (!form.propertyId) return "Pick a property.";
  if (!form.unitId) return "Map this feed to a unit.";
  try {
    const url = new URL(form.url);
    if (url.protocol !== "https:") return "Enter a valid https:// iCal feed URL.";
  } catch {
    return "Enter a valid https:// iCal feed URL.";
  }
  return null;
}

function problemMessage(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) return fallback;
  const detail = error.detail ?? error.title ?? error.message;
  const lowerDetail = detail.toLowerCase();
  const problem = error.problem;
  const rawError = typeof problem?.error === "string" ? problem.error : "";
  const fieldErrors = error.fieldErrors.map((fieldError) => fieldError.msg).filter(Boolean).join(" ");
  const combined = `${rawError} ${lowerDetail} ${fieldErrors}`.toLowerCase();

  if (combined.includes("duplicate") || combined.includes("already exists")) {
    return "This iCal feed already exists for the selected property or unit.";
  }
  if (combined.includes("overlap") || rawError === "stay_overlap") {
    return "This stay overlaps another stay for the selected unit. Review the dates or save once the server allows the conflict.";
  }
  if (combined.includes("ical_url_malformed") || combined.includes("malformed")) {
    return "The iCal URL is malformed. Paste the full provider export URL.";
  }
  if (combined.includes("ical_url_private_address")) {
    return "That iCal URL resolves to a private address and was blocked.";
  }
  if (combined.includes("ical_url_insecure_scheme")) {
    return "iCal feeds must use https:// URLs.";
  }
  if (combined.includes("ical_url_unreachable") || combined.includes("ical_url_timeout")) {
    return "The server could not reach that iCal feed. Check the provider export URL and try again.";
  }
  if (fieldErrors) return fieldErrors;
  return detail || fallback;
}

function originPreview(rawUrl: string): string | null {
  try {
    return new URL(rawUrl).origin;
  } catch {
    return null;
  }
}

function duplicateFeedNotice(form: IcalForm, feeds: IcalFeedPayload[]): string | null {
  const preview = originPreview(form.url);
  if (!preview) return null;
  const duplicate = feeds.find((feed) => {
    return (
      feed.property_id === form.propertyId &&
      feed.unit_id === form.unitId &&
      feed.url_preview === preview
    );
  });
  return duplicate ? "A feed from this host is already mapped to that unit." : null;
}

function describedBy(...ids: Array<string | false | null | undefined>): string | undefined {
  const liveIds = ids.filter(Boolean);
  return liveIds.length > 0 ? liveIds.join(" ") : undefined;
}

export default function StaysPage() {
  // code-health: ignore[nloc] Stays page is declarative reservation/closure composition over promoted route data.
  const { workspaceId } = useWorkspace();
  const isPhone = useIsPhone();
  const queryClient = useQueryClient();
  const manualDialogRef = useRef<HTMLDialogElement | null>(null);
  const icalDialogRef = useRef<HTMLDialogElement | null>(null);
  const today = useMemo(() => new Date(), []);
  const todayIso = useMemo(() => isoDate(today), [today]);
  const [manualForm, setManualForm] = useState<ManualStayForm | null>(null);
  const [icalForm, setIcalForm] = useState<IcalForm | null>(null);
  const [manualNotice, setManualNotice] = useState<FormNotice | null>(null);
  const [icalNotice, setIcalNotice] = useState<FormNotice | null>(null);

  const dataQ = useQuery({
    queryKey: qk.stays(),
    queryFn: fetchStaysPayload,
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const empsQ = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });
  const meQ = useQuery({
    queryKey: qk.authMe(),
    queryFn: () => fetchJson<AuthMe>("/api/v1/auth/me"),
  });
  const wsQ = useQuery({
    queryKey: qk.meWorkspaces(),
    queryFn: () => fetchJson<{ workspace_id: string; slug: string; name: string }[]>("/api/v1/me/workspaces"),
  });
  const feedsQ = useQuery({
    queryKey: qk.icalFeeds(),
    queryFn: fetchIcalFeeds,
  });
  const propertyIds = propsQ.data?.map((property) => property.id) ?? [];
  const unitQs = useQueries({
    queries: propertyIds.map((propertyId) => ({
      queryKey: qk.propertyUnits(propertyId),
      queryFn: () => fetchPropertyUnits(propertyId),
    })),
  });
  const membershipQs = useQueries({
    queries: propertyIds.map((propertyId) => ({
      queryKey: qk.propertyWorkspaces(propertyId),
      queryFn: () => fetchPropertyMemberships(propertyId),
    })),
  });
  const metadataPending = propsQ.isPending || (
    propsQ.data
      ? unitQs.some((query) => query.isPending) || membershipQs.some((query) => query.isPending)
      : false
  );

  const createStay = useMutation({
    mutationFn: (body: {
      property_id: string;
      unit_id: string;
      check_in_at: string;
      check_out_at: string;
      guest_name: string | null;
      guest_count: number;
      guest_kind: "guest";
      status: StayStatus;
      source: StaySource;
    }) => fetchJson<ReservationPayload>("/api/v1/stays", { method: "POST", body }),
    onSuccess: (reservation) => {
      queryClient.setQueryData<StaysPayload>(qk.stays(), (current) => {
        if (!current) return current;
        return { ...current, stays: [mapReservation(reservation), ...current.stays] };
      });
      void queryClient.invalidateQueries({ queryKey: qk.stays() });
      setManualNotice({ tone: "success", text: "Stay created and added to the list." });
      manualDialogRef.current?.close();
    },
    onError: (error) => {
      setManualNotice({
        tone: "error",
        text: problemMessage(error, "The stay could not be created. Check the fields and try again."),
      });
    },
  });

  const createIcalFeed = useMutation({
    mutationFn: (body: {
      property_id: string;
      unit_id: string;
      url: string;
      provider_override: IcalProvider;
    }) => fetchJson<IcalFeedPayload>("/api/v1/stays/ical-feeds", { method: "POST", body }),
    onSuccess: (feed) => {
      queryClient.setQueryData<IcalFeedPayload[]>(qk.icalFeeds(), (current) => {
        return current ? [feed, ...current] : [feed];
      });
      void queryClient.invalidateQueries({ queryKey: qk.icalFeeds() });
      void queryClient.invalidateQueries({ queryKey: qk.stays() });
      setIcalNotice({
        tone: "success",
        text: feed.enabled
          ? `Feed added. ${providerLabel(feed.provider)} parsed successfully and is enabled.`
          : `Feed added but not enabled yet. Last check: ${feed.last_error ?? "provider did not return a parseable calendar"}.`,
      });
    },
    onError: (error) => {
      setIcalNotice({
        tone: "error",
        text: problemMessage(error, "The iCal feed could not be added. Check the setup and try again."),
      });
    },
  });

  if (
    dataQ.isPending ||
    propsQ.isPending ||
    empsQ.isPending ||
    meQ.isPending ||
    wsQ.isPending ||
    feedsQ.isPending ||
    metadataPending
  ) {
    return <DeskPage title="Stays"><Loading /></DeskPage>;
  }
  if (
    !dataQ.data ||
    !propsQ.data ||
    !empsQ.data ||
    !meQ.data ||
    !wsQ.data ||
    !feedsQ.data ||
    unitQs.some((query) => !query.data) ||
    membershipQs.some((query) => !query.data)
  ) {
    return <DeskPage title="Stays">Failed to load.</DeskPage>;
  }

  const data = normalizeStaysPayload(dataQ.data);
  const { stays } = data;
  const properties = propsQ.data;
  const units = unitQs.flatMap((query) => query.data ?? []);
  const memberships = membershipQs.flatMap((query) => query.data ?? []);
  const propsById = new Map(properties.map((p) => [p.id, p]));
  const unitsByProperty = new Map<string, UnitPayload[]>();
  for (const unit of units) {
    const existing = unitsByProperty.get(unit.property_id) ?? [];
    existing.push(unit);
    unitsByProperty.set(unit.property_id, existing);
  }
  const activeWorkspaceId = meQ.data.current_workspace_id
    ?? (workspaceId ? wsQ.data.find((workspace) => workspace.slug === workspaceId)?.workspace_id : null)
    ?? null;
  function canSeeGuestIdentity(propertyId: string): boolean {
    const membership = memberships.find((entry) => {
      return entry.property_id === propertyId && entry.workspace_id === activeWorkspaceId;
    });
    return Boolean(
      membership && (
        membership.membership_role === "owner_workspace" ||
        membership.share_guest_identity
      ),
    );
  }
  function guestNameForStay(stay: PageStay): string {
    return canSeeGuestIdentity(stay.property_id) ? stay.guest_name : "Hidden guest";
  }
  const canShareManualGuestName = manualForm ? canSeeGuestIdentity(manualForm.propertyId) : false;
  const manualOverlap = manualForm ? overlapWarning(manualForm, stays, guestNameForStay) : null;
  const selectedManualUnits = manualForm ? unitsByProperty.get(manualForm.propertyId) ?? [] : [];
  const selectedIcalUnits = icalForm ? unitsByProperty.get(icalForm.propertyId) ?? [] : [];
  const icalDuplicate = icalForm ? duplicateFeedNotice(icalForm, feedsQ.data) : null;

  function openManualDialog(): void {
    const next = initialManualForm(properties, units);
    setManualForm(next);
    setManualNotice(null);
    createStay.reset();
    manualDialogRef.current?.showModal();
  }

  function openIcalDialog(): void {
    const next = initialIcalForm(properties, units);
    setIcalForm(next);
    setIcalNotice(null);
    createIcalFeed.reset();
    icalDialogRef.current?.showModal();
  }

  function updateManualProperty(propertyId: string): void {
    const propertyUnits = unitsByProperty.get(propertyId) ?? [];
    setManualForm((current) => current ? { ...current, propertyId, unitId: propertyUnits[0]?.id ?? "" } : current);
  }

  function updateIcalProperty(propertyId: string): void {
    // code-health: ignore[ccn nloc] Tiny setter is over-counted because lizard extends the TSX function range through the dialog JSX.
    const propertyUnits = unitsByProperty.get(propertyId) ?? [];
    setIcalForm((current) => current ? { ...current, propertyId, unitId: propertyUnits[0]?.id ?? "" } : current);
  }

  function submitManualStay(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!manualForm) return;
    const validation = validateManualForm(manualForm, canShareManualGuestName);
    if (validation) {
      setManualNotice({ tone: "error", text: validation });
      return;
    }
    const guestName = canShareManualGuestName ? manualForm.guestName.trim() : "";
    createStay.mutate({
      property_id: manualForm.propertyId,
      unit_id: manualForm.unitId,
      check_in_at: `${manualForm.checkIn}T16:00:00Z`,
      check_out_at: `${manualForm.checkOut}T10:00:00Z`,
      guest_name: guestName || null,
      guest_count: Number.parseInt(manualForm.guestCount, 10),
      guest_kind: "guest",
      status: manualForm.status,
      source: "manual",
    });
  }

  function submitIcalFeed(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!icalForm) return;
    const validation = validateIcalForm(icalForm);
    if (validation) {
      setIcalNotice({ tone: "error", text: validation });
      return;
    }
    createIcalFeed.mutate({
      property_id: icalForm.propertyId,
      unit_id: icalForm.unitId,
      url: icalForm.url.trim(),
      provider_override: icalForm.provider,
    });
  }

  return (
    <DeskPage
      title="Stays"
      sub="Imported from Airbnb, VRBO, and direct bookings. Four layers: stays, turnover bundles, closures, employee leave."
      actions={
        <button type="button" className="btn btn--moss" onClick={openIcalDialog}>
          Import iCal
        </button>
      }
      overflow={[
        {
          label: "Add stay",
          onSelect: openManualDialog,
        },
      ]}
    >
      <dialog className="modal modal--sheet sheet-form-dialog" ref={manualDialogRef} aria-label="Add stay">
        {manualForm ? (
          <form
            className="stay-create-form sheet-form"
            onSubmit={submitManualStay}
            aria-describedby={describedBy(
              !canShareManualGuestName && manualPrivacyId,
              selectedManualUnits.length === 0 && manualUnitErrorId,
              manualOverlap && manualOverlapId,
              manualNotice && manualNoticeId,
            )}
            noValidate
          >
            <header className="stay-create-form__head sheet-form__head">
              <div>
                <p className="stay-create-form__eyebrow sheet-form__eyebrow">Reservations</p>
                <h3 className="stay-create-form__title sheet-form__title">Add stay</h3>
                <p className="stay-create-form__sub sheet-form__sub">
                  Manual stays save to the reservation API and use the selected unit for conflict checks.
                </p>
              </div>
              <button
                type="button"
                className="stay-create-form__close sheet-form__close"
                onClick={() => manualDialogRef.current?.close()}
                aria-label="Close"
              >
                ×
              </button>
            </header>

            <div className="stay-create-form__body sheet-form__body">
            <div className="stay-create-form__grid sheet-form__grid">
              <FormField label="Property" requirement="required" className="stay-create-form__field sheet-form__field">
                <select
                  value={manualForm.propertyId}
                  onChange={(event) => updateManualProperty(event.target.value)}
                >
                  {properties.map((property) => (
                    <option key={property.id} value={property.id}>{property.name}</option>
                  ))}
                </select>
              </FormField>

              <FormField label="Unit" requirement="required" className="stay-create-form__field sheet-form__field">
                <select
                  value={manualForm.unitId}
                  onChange={(event) => setManualForm({ ...manualForm, unitId: event.target.value })}
                  aria-invalid={selectedManualUnits.length === 0}
                  aria-describedby={selectedManualUnits.length === 0 ? manualUnitErrorId : undefined}
                >
                  {selectedManualUnits.map((unit) => (
                    <option key={unit.id} value={unit.id}>{unit.name}</option>
                  ))}
                </select>
              </FormField>

              <FormField label="Check-in" requirement="required" className="stay-create-form__field sheet-form__field">
                <input
                  type="date"
                  value={manualForm.checkIn}
                  onChange={(event) => setManualForm({ ...manualForm, checkIn: event.target.value })}
                />
              </FormField>

              <FormField label="Check-out" requirement="required" className="stay-create-form__field sheet-form__field">
                <input
                  type="date"
                  value={manualForm.checkOut}
                  onChange={(event) => setManualForm({ ...manualForm, checkOut: event.target.value })}
                />
              </FormField>

              <FormField label="Guest name" requirement="optional" className="stay-create-form__field sheet-form__field">
                <input
                  type="text"
                  value={canShareManualGuestName ? manualForm.guestName : ""}
                  disabled={!canShareManualGuestName}
                  placeholder={canShareManualGuestName ? "Ada Guest" : "Hidden by sharing settings"}
                  aria-describedby={!canShareManualGuestName ? manualPrivacyId : undefined}
                  onChange={(event) => setManualForm({ ...manualForm, guestName: event.target.value })}
                />
              </FormField>

              <FormField label="Guests" requirement="required" className="stay-create-form__field sheet-form__field">
                <input
                  type="number"
                  min="1"
                  value={manualForm.guestCount}
                  onChange={(event) => setManualForm({ ...manualForm, guestCount: event.target.value })}
                />
              </FormField>

              <FormField label="Status" requirement="required" className="stay-create-form__field sheet-form__field">
                <select
                  value={manualForm.status}
                  onChange={(event) => setManualForm({ ...manualForm, status: event.target.value as StayStatus })}
                >
                  <option value="tentative">Tentative</option>
                  <option value="confirmed">Confirmed</option>
                </select>
              </FormField>
            </div>

            {!canShareManualGuestName ? (
              <p id={manualPrivacyId} className="stays-form-note">
                Guest identity is hidden for this shared property. This stay will be saved without a guest name.
              </p>
            ) : null}
            {selectedManualUnits.length === 0 ? (
              <p id={manualUnitErrorId} className="form-error" role="alert">No units are available for this property.</p>
            ) : null}
            {manualOverlap ? <p id={manualOverlapId} className="stays-form-note stays-form-note--warn">{manualOverlap}</p> : null}
            {manualNotice ? (
              <p id={manualNoticeId} className={`form-notice form-notice--${manualNotice.tone}`} role="alert">
                {manualNotice.text}
              </p>
            ) : null}
            </div>

            <footer className="stay-create-form__footer sheet-form__footer">
              <button type="button" className="btn btn--ghost" onClick={() => manualDialogRef.current?.close()}>
                Cancel
              </button>
              <button type="submit" className="btn btn--moss" disabled={createStay.isPending}>
                {createStay.isPending ? "Creating..." : "Create stay"}
              </button>
            </footer>
          </form>
        ) : null}
      </dialog>

      <dialog className="modal modal--sheet sheet-form-dialog" ref={icalDialogRef} aria-label="Import iCal">
        {icalForm ? (
          <form
            className="ical-feed-form sheet-form"
            onSubmit={submitIcalFeed}
            aria-describedby={describedBy(
              selectedIcalUnits.length === 0 && icalUnitErrorId,
              icalDuplicate && icalDuplicateId,
              icalNotice && icalNoticeId,
            )}
            noValidate
          >
            <header className="ical-feed-form__head sheet-form__head">
              <div>
                <p className="ical-feed-form__eyebrow sheet-form__eyebrow">Calendar feed</p>
                <h3 className="ical-feed-form__title sheet-form__title">Import iCal</h3>
                <p className="ical-feed-form__sub sheet-form__sub">
                  Add a provider export URL, map it to a unit, and crew.day will probe it before enabling the feed.
                </p>
              </div>
              <button
                type="button"
                className="ical-feed-form__close sheet-form__close"
                onClick={() => icalDialogRef.current?.close()}
                aria-label="Close"
              >
                ×
              </button>
            </header>

            <div className="ical-feed-form__body sheet-form__body">
            <div className="ical-feed-form__grid sheet-form__grid">
              <FormField label="Property" requirement="required" className="ical-feed-form__field sheet-form__field">
                <select
                  value={icalForm.propertyId}
                  onChange={(event) => updateIcalProperty(event.target.value)}
                >
                  {properties.map((property) => (
                    <option key={property.id} value={property.id}>{property.name}</option>
                  ))}
                </select>
              </FormField>

              <FormField label="Unit" requirement="required" className="ical-feed-form__field sheet-form__field">
                <select
                  value={icalForm.unitId}
                  onChange={(event) => setIcalForm({ ...icalForm, unitId: event.target.value })}
                  aria-invalid={selectedIcalUnits.length === 0}
                  aria-describedby={selectedIcalUnits.length === 0 ? icalUnitErrorId : undefined}
                >
                  {selectedIcalUnits.map((unit) => (
                    <option key={unit.id} value={unit.id}>{unit.name}</option>
                  ))}
                </select>
              </FormField>

              <FormField label="Provider" requirement="required" className="ical-feed-form__field sheet-form__field">
                <select
                  value={icalForm.provider}
                  onChange={(event) => setIcalForm({ ...icalForm, provider: event.target.value as IcalProvider })}
                >
                  {PROVIDERS.map((provider) => (
                    <option key={provider.value} value={provider.value}>{provider.label}</option>
                  ))}
                </select>
              </FormField>

              <FormField label="Feed URL" requirement="required" className="ical-feed-form__field sheet-form__field">
                <input
                  type="url"
                  inputMode="url"
                  placeholder="https://calendar.provider.example/export.ics"
                  value={icalForm.url}
                  aria-invalid={icalNotice?.tone === "error"}
                  aria-describedby={describedBy(icalDuplicate && icalDuplicateId, icalNotice && icalNoticeId)}
                  onChange={(event) => setIcalForm({ ...icalForm, url: event.target.value })}
                />
              </FormField>
            </div>

            {selectedIcalUnits.length === 0 ? (
              <p id={icalUnitErrorId} className="form-error" role="alert">No units are available for this property.</p>
            ) : null}
            {icalDuplicate ? <p id={icalDuplicateId} className="stays-form-note stays-form-note--warn">{icalDuplicate}</p> : null}
            {icalNotice ? (
              <p id={icalNoticeId} className={`form-notice form-notice--${icalNotice.tone}`} role="alert">
                {icalNotice.text}
              </p>
            ) : null}
            </div>

            <footer className="ical-feed-form__footer sheet-form__footer">
              <button type="button" className="btn btn--ghost" onClick={() => icalDialogRef.current?.close()}>
                Close
              </button>
              <button type="submit" className="btn btn--moss" disabled={createIcalFeed.isPending}>
                {createIcalFeed.isPending ? "Testing..." : "Add feed"}
              </button>
            </footer>
          </form>
        ) : null}
      </dialog>

      <div className="panel">
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Guest</th>
              <th>Property</th>
              <th>Source</th>
              <th>Check-in</th>
              <th>Check-out</th>
              <th>Guests</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {stays.map((s) => {
              const p = propsById.get(s.property_id);
              return (
                <tr key={s.id}>
                  <td><strong>{guestNameForStay(s)}</strong></td>
                  <td>{p && <Chip tone={p.color} size="sm">{p.name}</Chip>}</td>
                  <td><Chip tone="ghost" size="sm">{s.source}</Chip></td>
                  <td className="mono">{fmtAbbrevDate(s.check_in)}</td>
                  <td className="mono">{fmtAbbrevDate(s.check_out)}</td>
                  <td>{s.guests}</td>
                  <td><Chip tone={STAY_TONE[s.status]} size="sm">{s.status.replace("_", " ")}</Chip></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head">
          <h2>iCal feeds</h2>
          <span className="muted">{feedsQ.data.length} connected</span>
        </header>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Provider</th>
              <th>Property</th>
              <th>Unit</th>
              <th>Status</th>
              <th>Last poll</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {feedsQ.data.map((feed) => {
              const property = propsById.get(feed.property_id);
              const unit = units.find((entry) => entry.id === feed.unit_id);
              return (
                <tr key={feed.id}>
                  <td><strong>{providerLabel(feed.provider)}</strong></td>
                  <td>{property ? <Chip tone={property.color} size="sm">{property.name}</Chip> : "—"}</td>
                  <td>{unit?.name ?? "All units"}</td>
                  <td>
                    <Chip tone={feed.enabled ? "moss" : "sand"} size="sm">
                      {feed.enabled ? "enabled" : "needs review"}
                    </Chip>
                  </td>
                  <td className="mono">{feed.last_polled_at ? fmtAbbrevDate(feed.last_polled_at) : "—"}</td>
                  <td className="mono">{feed.url_preview}</td>
                </tr>
              );
            })}
            {feedsQ.data.length === 0 ? (
              <tr>
                <td colSpan={6} className="muted">No iCal feeds connected yet.</td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <InfiniteStaysAgenda
        variant={isPhone ? "phone" : "desktop"}
        today={today}
        todayIso={todayIso}
        properties={properties}
        employees={empsQ.data}
        payload={data}
        guestNameForStay={guestNameForStay}
      />
    </DeskPage>
  );
}
