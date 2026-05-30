import type { Property } from "@/types/api";
import type { PropertyAddress, PropertyRecord } from "./types";

export const PROPERTY_KIND_OPTIONS: readonly {
  value: Property["kind"];
  label: string;
}[] = [
  {
    value: "residence",
    label: "Primary residence - no automatic area or stay lifecycle setup",
  },
  {
    value: "vacation",
    label: "Vacation home - seed turnover areas and checkout workflow",
  },
  {
    value: "str",
    label: "Short-term rental - seed turnover areas and checkout workflow",
  },
  {
    value: "mixed",
    label: "Mixed use - seed turnover setup for guest, staff, and other stays",
  },
];

export interface PropertyEditDraft {
  name: string;
  kind: Property["kind"];
  line1: string;
  line2: string;
  city: string;
  state_province: string;
  postal_code: string;
  country: string;
  timezone: string;
  locale: string;
  default_currency: string;
  property_notes_md: string;
}

export interface PropertyPatchBody {
  name: string;
  kind: Property["kind"];
  address_json: PropertyAddress;
  country: string;
  locale: string | null;
  default_currency: string | null;
  timezone: string;
  lat: number | null;
  lon: number | null;
  client_org_id: string | null;
  owner_user_id: string | null;
  tags_json: string[];
  welcome_defaults_json: Record<string, unknown>;
  property_notes_md: string;
}

export interface PropertyCreateBody {
  name: string;
  kind: Property["kind"];
  address_json: PropertyAddress;
  country: string;
  locale: string | null;
  default_currency: string | null;
  timezone: string;
  lat: null;
  lon: null;
  client_org_id: null;
  owner_user_id: null;
  tags_json: string[];
  welcome_defaults_json: Record<string, unknown>;
  property_notes_md: string;
}

const BLANK_PROPERTY_DRAFT: PropertyEditDraft = {
  name: "",
  kind: "residence",
  line1: "",
  line2: "",
  city: "",
  state_province: "",
  postal_code: "",
  country: "",
  timezone: "UTC",
  locale: "",
  default_currency: "",
  property_notes_md: "",
};

export function draftFromProperty(property: PropertyRecord): PropertyEditDraft {
  return {
    name: property.name,
    kind: property.kind,
    line1: property.address_json.line1,
    line2: property.address_json.line2,
    city: property.address_json.city,
    state_province: property.address_json.state_province,
    postal_code: property.address_json.postal_code,
    country: property.address_json.country || property.country,
    timezone: property.timezone,
    locale: property.locale ?? "",
    default_currency: property.default_currency ?? "",
    property_notes_md: property.property_notes_md,
  };
}

export function blankPropertyDraft(defaults?: Partial<PropertyEditDraft>): PropertyEditDraft {
  return { ...BLANK_PROPERTY_DRAFT, ...defaults };
}

export function buildPropertyPatchBody(
  property: PropertyRecord,
  draft: PropertyEditDraft,
): PropertyPatchBody {
  const country = draft.country.trim().toUpperCase();
  const currency = draft.default_currency.trim().toUpperCase();
  return {
    name: draft.name.trim(),
    kind: draft.kind,
    address_json: {
      ...property.address_json,
      line1: draft.line1.trim(),
      line2: draft.line2.trim(),
      city: draft.city.trim(),
      state_province: draft.state_province.trim(),
      postal_code: draft.postal_code.trim(),
      country,
    },
    country,
    locale: draft.locale.trim() || null,
    default_currency: currency || null,
    timezone: draft.timezone.trim(),
    lat: property.lat,
    lon: property.lon,
    client_org_id: property.client_org_id,
    owner_user_id: property.owner_user_id,
    tags_json: property.tags_json,
    welcome_defaults_json: property.welcome_defaults_json,
    property_notes_md: draft.property_notes_md.trim(),
  };
}

export function buildPropertyCreateBody(draft: PropertyEditDraft): PropertyCreateBody {
  const country = draft.country.trim().toUpperCase();
  const currency = draft.default_currency.trim().toUpperCase();
  return {
    name: draft.name.trim(),
    kind: draft.kind,
    address_json: {
      line1: draft.line1.trim(),
      line2: draft.line2.trim(),
      city: draft.city.trim(),
      state_province: draft.state_province.trim(),
      postal_code: draft.postal_code.trim(),
      country,
    },
    country,
    locale: draft.locale.trim() || null,
    default_currency: currency || null,
    timezone: draft.timezone.trim(),
    lat: null,
    lon: null,
    client_org_id: null,
    owner_user_id: null,
    tags_json: [],
    welcome_defaults_json: {},
    property_notes_md: draft.property_notes_md.trim(),
  };
}
