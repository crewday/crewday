import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { useLocation, useParams } from "react-router-dom";
import { ApiError, fetchJson } from "@/lib/api";
import { type ListEnvelope } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import FormField from "@/components/FormField";
import FormModal, { FormModalGrid } from "@/components/FormModal";
import {
  InlineDateField,
  InlineNumberField,
  InlineSearchableSelectField,
  InlineSelectField,
  InlineTableForm,
  InlineTextField,
  type InlineTableColumn,
  type InlineTableRow,
} from "@/components/InlineTableForm";
import SearchableSelect, { type SearchableSelectOption } from "@/components/SearchableSelect";
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
import PropertyTabs from "./property/PropertyTabs";
import { InfiniteStaysAgenda, type PageStay, type StaysPlannerDraftRange } from "./stays/InfiniteStaysAgenda";

type IcalProvider = "airbnb" | "vrbo" | "booking" | "gcal" | "generic";
type StaySource = PageStay["source"];
type StayStatus = PageStay["status"];

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

function propertySelectOption(property: Property): SearchableSelectOption {
  return {
    value: property.id,
    label: property.name,
    secondaryText: property.city,
    searchText: `${property.name} ${property.city} ${property.timezone}`,
  };
}

function unitSelectOption(unit: UnitPayload): SearchableSelectOption {
  return {
    value: unit.id,
    label: unit.name,
  };
}

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

async function fetchStaysPayload(propertyId?: string | null): Promise<StaysPayload> {
  const reservationsPath = propertyId
    ? "/api/v1/stays/reservations?property_id=" + encodeURIComponent(propertyId) + "&limit=500"
    : "/api/v1/stays/reservations?limit=500";
  const [reservations, leaves] = await Promise.all([
    fetchJson<ListEnvelope<ReservationPayload>>(reservationsPath),
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

async function fetchIcalFeeds(propertyId?: string | null): Promise<IcalFeedPayload[]> {
  const path = propertyId
    ? "/api/v1/stays/ical-feeds?property_id=" + encodeURIComponent(propertyId)
    : "/api/v1/stays/ical-feeds";
  return fetchJson<IcalFeedPayload[]>(path);
}

function initialManualForm(properties: Property[], units: UnitPayload[], preferredPropertyId?: string | null): ManualStayForm {
  // code-health: ignore[ccn] Initial form factory has two defaulting branches; lizard over-counts the surrounding TSX module.
  const propertyId = preferredPropertyId && properties.some((property) => property.id === preferredPropertyId)
    ? preferredPropertyId
    : properties[0]?.id ?? "";
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

function emptyManualForm(): ManualStayForm {
  return {
    propertyId: "",
    unitId: "",
    guestName: "",
    guestCount: "1",
    checkIn: "",
    checkOut: "",
    status: "confirmed",
  };
}

function makeStayCreateRow(form: ManualStayForm): InlineTableRow<ManualStayForm> {
  return {
    id: "stay-create",
    isNew: true,
    editing: true,
    dirty: false,
    draft: form,
    committedDraft: form,
    label: "New stay",
  };
}

function initialIcalForm(properties: Property[], units: UnitPayload[], preferredPropertyId?: string | null): IcalForm {
  const propertyId = preferredPropertyId && properties.some((property) => property.id === preferredPropertyId)
    ? preferredPropertyId
    : properties[0]?.id ?? "";
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
  const detail = error.userMessage ?? error.detail ?? error.title ?? error.message;
  const lowerDetail = detail.toLowerCase();
  const problem = error.problem;
  const rawError = typeof problem?.error === "string" ? problem.error : "";
  const fieldErrors = error.fieldErrors.flatMap((fieldError) =>
    fieldError.msg ? [fieldError.msg] : [],
  ).join(" ");
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

function nextIsoDate(iso: string): string {
  const date = new Date(iso + "T00:00:00Z");
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString().slice(0, 10);
}

// react-doctor-disable-next-line react-doctor/no-giant-component -- Existing promoted surface is intentionally deferred until a focused component split preserves behavior.
export default function StaysPage() {
  // code-health: ignore[nloc] Stays page is declarative reservation/closure composition over promoted route data.
  const { pid } = useParams<{ pid?: string }>();
  const routePropertyId = pid ?? null;
  const { pathname } = useLocation();
  const { workspaceId } = useWorkspace();
  const isPhone = useIsPhone();
  const queryClient = useQueryClient();
  const today = useMemo(() => new Date(), []);
  const todayIso = useMemo(() => isoDate(today), [today]);
  const staysQueryKey = routePropertyId
    ? ([...qk.stays(), "property", routePropertyId] as const)
    : qk.stays();
  const feedsQueryKey = routePropertyId
    ? ([...qk.icalFeeds(), "property", routePropertyId] as const)
    : qk.icalFeeds();
  const [createStayRow, setCreateStayRow] = useState<InlineTableRow<ManualStayForm>>(
    () => makeStayCreateRow(emptyManualForm()),
  );
  const [icalForm, setIcalForm] = useState<IcalForm | null>(null);
  const [icalNotice, setIcalNotice] = useState<FormNotice | null>(null);

  const dataQ = useQuery({
    queryKey: staysQueryKey,
    queryFn: () => fetchStaysPayload(routePropertyId),
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
    queryKey: feedsQueryKey,
    queryFn: () => fetchIcalFeeds(routePropertyId),
  });
  const propertyIds = propsQ.data?.map((property) => property.id) ?? [];
  const metadataPropertyIds = routePropertyId ? [routePropertyId] : propertyIds;
  const unitQs = useQueries({
    queries: metadataPropertyIds.map((propertyId) => ({
      queryKey: qk.propertyUnits(propertyId),
      queryFn: () => fetchPropertyUnits(propertyId),
    })),
  });
  const membershipQs = useQueries({
    queries: metadataPropertyIds.map((propertyId) => ({
      queryKey: qk.propertyWorkspaces(propertyId),
      queryFn: () => fetchPropertyMemberships(propertyId),
    })),
  });
  const metadataPending = propsQ.isPending || (
    propsQ.data
      ? unitQs.some((query) => query.isPending) || membershipQs.some((query) => query.isPending)
      : false
  );
  const loadedUnits = useMemo(() => unitQs.flatMap((query) => query.data ?? []), [unitQs]);
  const loadedProperties = useMemo(() => propsQ.data ?? [], [propsQ.data]);
  const loadedPageProperties = useMemo(
    () => routePropertyId
      ? loadedProperties.filter((property) => property.id === routePropertyId)
      : loadedProperties,
    [loadedProperties, routePropertyId],
  );
  const defaultManualFormSignature = useMemo(() => {
    return JSON.stringify({
      properties: loadedPageProperties.map((property) => property.id),
      units: loadedUnits.map((unit) => [unit.id, unit.property_id]),
      routePropertyId,
    });
  }, [loadedPageProperties, loadedUnits, routePropertyId]);

  useEffect(() => {
    if (!propsQ.data || metadataPending) return;
    const nextForm = initialManualForm(loadedPageProperties, loadedUnits, routePropertyId);
    setCreateStayRow((row) => {
      if (row.dirty) return row;
      return {
        ...row,
        draft: nextForm,
        committedDraft: nextForm,
      };
    });
  // react-doctor-disable-next-line react-doctor/exhaustive-deps -- The signature tracks loaded property/unit contents; the useQueries result arrays are unstable by identity.
  }, [defaultManualFormSignature, metadataPending, propsQ.data, routePropertyId]);

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
      queryClient.setQueryData<StaysPayload>(staysQueryKey, (current) => {
        if (!current) return current;
        return { ...current, stays: [mapReservation(reservation), ...current.stays] };
      });
      void queryClient.invalidateQueries({ queryKey: qk.stays() });
      const nextForm = initialManualForm(loadedPageProperties, loadedUnits, routePropertyId);
      setCreateStayRow(makeStayCreateRow(nextForm));
    },
    onError: (error) => {
      setCreateStayRow((row) => ({
        ...row,
        error: problemMessage(error, "The stay could not be created. Check the fields and try again."),
      }));
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
      queryClient.setQueryData<IcalFeedPayload[]>(feedsQueryKey, (current) => {
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

  const loadedData = normalizeStaysPayload(dataQ.data);
  const properties = propsQ.data;
  const units = unitQs.flatMap((query) => query.data ?? []);
  const memberships = membershipQs.flatMap((query) => query.data ?? []);
  const pageProperties = routePropertyId
    ? properties.filter((property) => property.id === routePropertyId)
    : properties;
  const pageFeeds = routePropertyId
    ? feedsQ.data.filter((feed) => feed.property_id === routePropertyId)
    : feedsQ.data;
  const data = routePropertyId
    ? {
        ...loadedData,
        stays: loadedData.stays.filter((stay) => stay.property_id === routePropertyId),
        leaves: [],
      }
    : loadedData;
  const { stays } = data;
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
  const canShareCreateGuestName = createStayRow.draft.propertyId
    ? canSeeGuestIdentity(createStayRow.draft.propertyId)
    : false;
  const createStayOverlap = overlapWarning(createStayRow.draft, stays, guestNameForStay);
  const selectedCreateUnits = unitsByProperty.get(createStayRow.draft.propertyId) ?? [];
  const selectedIcalUnits = icalForm ? unitsByProperty.get(icalForm.propertyId) ?? [] : [];
  const icalDuplicate = icalForm ? duplicateFeedNotice(icalForm, pageFeeds) : null;

  function openIcalDialog(): void {
    const next = initialIcalForm(pageProperties, units, routePropertyId);
    setIcalForm(next);
    setIcalNotice(null);
    createIcalFeed.reset();
  }

  function updateManualProperty(propertyId: string): void {
    const propertyUnits = unitsByProperty.get(propertyId) ?? [];
    patchCreateStayRow({
      propertyId,
      unitId: propertyUnits[0]?.id ?? "",
      guestName: canSeeGuestIdentity(propertyId) ? createStayRow.draft.guestName : "",
    });
  }

  function updateIcalProperty(propertyId: string): void {
    // code-health: ignore[ccn nloc] Tiny setter is over-counted because lizard extends the TSX function range through the dialog JSX.
    const propertyUnits = unitsByProperty.get(propertyId) ?? [];
    setIcalForm((current) => current ? { ...current, propertyId, unitId: propertyUnits[0]?.id ?? "" } : current);
  }

  function patchCreateStayRow(patch: Partial<ManualStayForm>): void {
    setCreateStayRow((row) => ({
      ...row,
      draft: { ...row.draft, ...patch },
      dirty: true,
      validation: undefined,
      error: undefined,
    }));
  }

  function saveCreateStayRow(): void {
    const manualForm = createStayRow.draft;
    const validation = validateManualForm(manualForm, canShareCreateGuestName);
    if (validation) {
      setCreateStayRow((row) => ({ ...row, dirty: true, validation, error: undefined }));
      return;
    }
    const guestName = canShareCreateGuestName ? manualForm.guestName.trim() : "";
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

  function cancelCreateStayRow(): void {
    setCreateStayRow(makeStayCreateRow(initialManualForm(pageProperties, units, routePropertyId)));
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

  const stayColumns: InlineTableColumn<ManualStayForm>[] = [
    {
      key: "guest",
      header: "Guest",
      width: { flex: 1.2, min: 180 },
      renderRead: ({ row }) => <strong>{row.draft.guestName || "Hidden guest"}</strong>,
      renderEdit: ({ row, update, disabled }) => {
        const canShareGuestName = row.draft.propertyId ? canSeeGuestIdentity(row.draft.propertyId) : false;
        return (
          <InlineTextField
            value={canShareGuestName ? row.draft.guestName : ""}
            disabled={disabled || !canShareGuestName}
            placeholder={canShareGuestName ? "Ada Guest" : "Hidden by sharing settings"}
            ariaLabel="Guest name"
            ariaDescribedBy={!canShareGuestName ? manualPrivacyId : undefined}
            onChange={(guestName) => update({ guestName })}
          />
        );
      },
    },
    {
      key: "property",
      header: "Property",
      width: { flex: 1.1, min: 180 },
      renderRead: ({ row }) => {
        const property = propsById.get(row.draft.propertyId);
        return property ? <Chip tone={property.color} size="sm">{property.name}</Chip> : ",";
      },
      renderEdit: ({ row, disabled }) => (
        <InlineSearchableSelectField
          value={row.draft.propertyId}
          options={pageProperties.map(propertySelectOption)}
          disabled={disabled || Boolean(routePropertyId)}
          ariaLabel="Property"
          noResultsLabel="No properties available"
          onChange={updateManualProperty}
        />
      ),
    },
    {
      key: "unit",
      header: "Unit",
      width: { flex: 1, min: 160 },
      renderRead: ({ row }) => units.find((unit) => unit.id === row.draft.unitId)?.name ?? ",",
      renderEdit: ({ row, update, disabled }) => {
        const propertyUnits = unitsByProperty.get(row.draft.propertyId) ?? [];
        return (
          <InlineSearchableSelectField
            value={row.draft.unitId}
            options={propertyUnits.map(unitSelectOption)}
            disabled={disabled}
            ariaLabel="Unit"
            noResultsLabel="No units available"
            renderOptionSecondaryText={() => null}
            onChange={(unitId) => update({ unitId })}
          />
        );
      },
    },
    {
      key: "check_in",
      header: "Check-in",
      width: { px: 156 },
      renderRead: ({ row }) => <span className="mono">{row.draft.checkIn ? fmtAbbrevDate(row.draft.checkIn) : ","}</span>,
      renderEdit: ({ row, update, disabled }) => (
        <InlineDateField
          value={row.draft.checkIn}
          disabled={disabled}
          ariaLabel="Check-in"
          onChange={(checkIn) => update({ checkIn })}
        />
      ),
    },
    {
      key: "check_out",
      header: "Check-out",
      width: { px: 156 },
      renderRead: ({ row }) => <span className="mono">{row.draft.checkOut ? fmtAbbrevDate(row.draft.checkOut) : ","}</span>,
      renderEdit: ({ row, update, disabled }) => (
        <InlineDateField
          value={row.draft.checkOut}
          disabled={disabled}
          ariaLabel="Check-out"
          onChange={(checkOut) => update({ checkOut })}
        />
      ),
    },
    {
      key: "guests",
      header: "Guests",
      width: { px: 96 },
      align: "end",
      renderRead: ({ row }) => row.draft.guestCount,
      renderEdit: ({ row, update, disabled }) => (
        <InlineNumberField
          value={row.draft.guestCount}
          min={1}
          disabled={disabled}
          ariaLabel="Guests"
          onChange={(guestCount) => update({ guestCount })}
        />
      ),
    },
    {
      key: "status",
      header: "Status",
      width: { px: 140 },
      renderRead: ({ row }) => <Chip tone={STAY_TONE[row.draft.status]} size="sm">{row.draft.status.replace("_", " ")}</Chip>,
      renderEdit: ({ row, update, disabled }) => (
        <InlineSelectField
          value={row.draft.status}
          disabled={disabled}
          ariaLabel="Status"
          options={[
            { value: "tentative", label: "Tentative" },
            { value: "confirmed", label: "Confirmed" },
          ]}
          onChange={(status) => update({ status: status as StayStatus })}
        />
      ),
    },
    {
      key: "source",
      header: "Source",
      width: { px: 120 },
      renderRead: ({ row }) => <Chip tone="ghost" size="sm">{stays.find((stay) => stay.id === row.id)?.source ?? "manual"}</Chip>,
      renderEdit: () => <Chip tone="ghost" size="sm">manual</Chip>,
    },
  ];
  const stayRows: InlineTableRow<ManualStayForm>[] = stays.map((stay) => ({
    id: stay.id,
    draft: {
      propertyId: stay.property_id,
      unitId: stay.unit_id ?? "",
      guestName: guestNameForStay(stay),
      guestCount: String(stay.guests),
      checkIn: stay.check_in,
      checkOut: stay.check_out,
      status: stay.status,
    },
    label: `${guestNameForStay(stay)} stay from ${fmtAbbrevDate(stay.check_in)} to ${fmtAbbrevDate(stay.check_out)}`,
  }));
  const createStayMeta = createStayRow.draft.propertyId && (
    !canShareCreateGuestName ||
    selectedCreateUnits.length === 0 ||
    createStayOverlap
  ) ? (
    <>
      {!canShareCreateGuestName ? (
        <p id={manualPrivacyId} className="stays-form-note">
          Guest identity is hidden for this shared property. This stay will be saved without a guest name.
        </p>
      ) : null}
      {selectedCreateUnits.length === 0 ? (
        <p id={manualUnitErrorId} className="form-error" role="alert">No units are available for this property.</p>
      ) : null}
      {createStayOverlap ? (
        <p id={manualOverlapId} className="stays-form-note stays-form-note--warn">{createStayOverlap}</p>
      ) : null}
    </>
  ) : undefined;
  const activeCreateStayRow: InlineTableRow<ManualStayForm> = {
    ...createStayRow,
    saving: createStay.isPending,
    meta: createStayMeta,
  };
  const plannerDraftRanges: StaysPlannerDraftRange[] =
    createStayRow.dirty && createStayRow.draft.checkIn && createStayRow.draft.checkOut && createStayRow.draft.checkIn < createStayRow.draft.checkOut
      ? [{
          id: "draft-stay",
          kind: "stay",
          starts_on: createStayRow.draft.checkIn,
          ends_on: createStayRow.draft.checkOut,
          endExclusive: true,
          label: "Draft stay",
          meta: canShareCreateGuestName && createStayRow.draft.guestName.trim()
            ? createStayRow.draft.guestName.trim()
            : "Unsaved manual stay",
          property_id: createStayRow.draft.propertyId,
        }]
      : [];

  return (
    <DeskPage
      title="Stays"
      sub={
        routePropertyId
          ? "Imported from Airbnb, VRBO, and direct bookings. Property view: stays, turnover bundles, and closures."
          : "Imported from Airbnb, VRBO, and direct bookings. Four layers: stays, turnover bundles, closures, employee leave."
      }
      actions={
        <button type="button" className="btn btn--moss" onClick={openIcalDialog}>
          Import iCal
        </button>
      }
    >
      {pid ? (
        <PropertyTabs
          pathname={pathname}
          propertyId={pid}
          activeRelatedPage="stays"
        />
      ) : null}

      <FormModal
        open={icalForm !== null}
        title="Import iCal"
        eyebrow="Calendar feed"
        subtitle="Add a provider export URL, map it to a unit, and crew.day will probe it before enabling the feed."
        formClassName="ical-feed-form"
        onClose={() => setIcalForm(null)}
        onSubmit={submitIcalFeed}
        describedBy={
          icalForm
            ? describedBy(
                selectedIcalUnits.length === 0 && icalUnitErrorId,
                icalDuplicate && icalDuplicateId,
                icalNotice && icalNoticeId,
              )
            : undefined
        }
        noValidate
        actions={
          <>
            <button type="button" className="btn btn--ghost" onClick={() => setIcalForm(null)}>
              Close
            </button>
            <button type="submit" className="btn btn--moss" disabled={createIcalFeed.isPending}>
              {createIcalFeed.isPending ? "Testing..." : "Add feed"}
            </button>
          </>
        }
      >
        {icalForm ? (
          <>
            <FormModalGrid className="ical-feed-form__grid">
              <SearchableSelect
                label="Property"
                className="ical-feed-form__field sheet-form__field"
                value={icalForm.propertyId}
                options={pageProperties.map(propertySelectOption)}
                required
                onChange={updateIcalProperty}
              />

              <SearchableSelect
                label="Unit"
                className="ical-feed-form__field sheet-form__field"
                value={icalForm.unitId}
                options={selectedIcalUnits.map(unitSelectOption)}
                required
                aria-invalid={selectedIcalUnits.length === 0}
                aria-describedby={selectedIcalUnits.length === 0 ? icalUnitErrorId : undefined}
                noResultsLabel="No units available"
                renderOptionSecondaryText={() => null}
                onChange={(value) => setIcalForm({ ...icalForm, unitId: value })}
              />

              <FormField label="Provider" requirement="required" className="ical-feed-form__field sheet-form__field">
                <select
                  value={icalForm.provider}
                  onChange={(event) => setIcalForm({ ...icalForm, provider: event.target.value as IcalProvider })} aria-label="Provider"
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
                 aria-label="Feed URL"/>
              </FormField>
            </FormModalGrid>

            {selectedIcalUnits.length === 0 ? (
              <p id={icalUnitErrorId} className="form-error" role="alert">No units are available for this property.</p>
            ) : null}
            {icalDuplicate ? <p id={icalDuplicateId} className="stays-form-note stays-form-note--warn">{icalDuplicate}</p> : null}
            {icalNotice ? (
              <p id={icalNoticeId} className={`form-notice form-notice--${icalNotice.tone}`} role="alert">
                {icalNotice.text}
              </p>
            ) : null}
          </>
        ) : null}
      </FormModal>

      <div className="panel">
        <InlineTableForm
          ariaLabel="Stays"
          columns={stayColumns}
          rows={stayRows}
          trailingCreateRow={activeCreateStayRow}
          saveMode="explicit"
          onDraftChange={(rowId, patch) => {
            if (rowId === createStayRow.id) patchCreateStayRow(patch);
          }}
          onCancel={(rowId) => {
            if (rowId === createStayRow.id) cancelCreateStayRow();
          }}
          onSave={(rowId) => {
            if (rowId === createStayRow.id) saveCreateStayRow();
          }}
          getRowLabel={(row) => row.label ?? "Stay"}
        />
      </div>

      <div className="panel">
        <header className="panel__head">
          <h2>iCal feeds</h2>
          <span className="muted">{pageFeeds.length} connected</span>
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
            {pageFeeds.map((feed) => {
              const property = propsById.get(feed.property_id);
              const unit = units.find((entry) => entry.id === feed.unit_id);
              return (
                <tr key={feed.id}>
                  <td><strong>{providerLabel(feed.provider)}</strong></td>
                  <td>{property ? <Chip tone={property.color} size="sm">{property.name}</Chip> : ","}</td>
                  <td>{unit?.name ?? "All units"}</td>
                  <td>
                    <Chip tone={feed.enabled ? "moss" : "sand"} size="sm">
                      {feed.enabled ? "enabled" : "needs review"}
                    </Chip>
                  </td>
                  <td className="mono">{feed.last_polled_at ? fmtAbbrevDate(feed.last_polled_at) : ","}</td>
                  <td className="mono">{feed.url_preview}</td>
                </tr>
              );
            })}
            {pageFeeds.length === 0 ? (
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
        properties={pageProperties}
        employees={empsQ.data}
        payload={data}
        guestNameForStay={guestNameForStay}
        showLeaveLayer={!routePropertyId}
        draftRanges={plannerDraftRanges}
        selectionLabel="Select stay dates"
        onDraftRangeSelect={(fromIso, toIso) => {
          patchCreateStayRow({ checkIn: fromIso, checkOut: nextIsoDate(toIso) });
        }}
      />
    </DeskPage>
  );
}
