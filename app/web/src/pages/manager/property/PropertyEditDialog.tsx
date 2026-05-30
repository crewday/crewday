import { useEffect, useState, type FormEvent } from "react";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import CountrySelect from "@/components/CountrySelect";
import FormField from "@/components/FormField";
import FormModal, { FormModalGrid } from "@/components/FormModal";
import TimezoneSelect from "@/components/TimezoneSelect";
import type { Property } from "@/types/api";
import type { PropertyAddress, PropertyRecord } from "./types";

const PROPERTY_KIND_OPTIONS: readonly {
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

interface PropertyEditDialogProps {
  open: boolean;
  property: PropertyRecord | null;
  initialDraft?: PropertyEditDraft;
  mode?: "create" | "edit";
  saving: boolean;
  error: string | null;
  onSubmit: (draft: PropertyEditDraft) => void;
  onClose: () => void;
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

export default function PropertyEditDialog(props: PropertyEditDialogProps) {
  // code-health: ignore[nloc] Property edit dialog is one promoted form surface; field order mirrors the property API shape.
  const {
    open,
    property,
    initialDraft,
    mode = "edit",
    saving,
    error,
    onSubmit,
    onClose,
  } = props;
  const [draft, setDraft] = useState<PropertyEditDraft>(() =>
    property ? draftFromProperty(property) : blankPropertyDraft(initialDraft)
  );

  useEffect(() => {
    if (open) setDraft(property ? draftFromProperty(property) : blankPropertyDraft(initialDraft));
  }, [initialDraft, open, property]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit(draft);
  }

  return (
    <FormModal
      open={open}
      title={mode === "create" ? "Add property" : "Edit property"}
      titleId="property-edit-dialog-title"
      eyebrow="Property"
      formClassName="property-edit-dialog"
      onCancel={(event) => {
        if (saving) event.preventDefault();
      }}
      onClose={onClose}
      onSubmit={submit}
      closeDisabled={saving}
      actions={
        <>
          <button
            type="button"
            className="btn btn--ghost"
            disabled={saving}
            onClick={onClose}
          >
            Cancel
          </button>
          <button type="submit" className="btn btn--moss" disabled={saving}>
            {saving ? "Saving..." : mode === "create" ? "Create property" : "Save property"}
          </button>
        </>
      }
    >
        <FormModalGrid className="property-edit-dialog__grid">
          <FormField
            label="Name"
            requirement="required"
            className="property-edit-dialog__identity-field property-edit-dialog__field sheet-form__field"
          >
            <input
              value={draft.name}
              onChange={(event) => setDraft({ ...draft, name: event.currentTarget.value })}
              required
             aria-label="Name"/>
          </FormField>
          <FormField
            label="Kind"
            requirement="optional"
            className="property-edit-dialog__identity-field property-edit-dialog__field sheet-form__field"
          >
            <select
              value={draft.kind}
              onChange={(event) =>
                setDraft({ ...draft, kind: event.currentTarget.value as Property["kind"] })
              } aria-label="Kind"
            >
              {PROPERTY_KIND_OPTIONS.map((kind) => (
                <option key={kind.value} value={kind.value}>{kind.label}</option>
              ))}
            </select>
          </FormField>
          <FormField label="Address line 1" requirement="optional" className="property-edit-dialog__field sheet-form__field">
            <input
              value={draft.line1}
              onChange={(event) => setDraft({ ...draft, line1: event.currentTarget.value })}
             aria-label="Address line 1"/>
          </FormField>
          <FormField label="Address line 2" requirement="optional" className="property-edit-dialog__field sheet-form__field">
            <input
              value={draft.line2}
              onChange={(event) => setDraft({ ...draft, line2: event.currentTarget.value })}
             aria-label="Address line 2"/>
          </FormField>
          <FormField label="City" requirement="optional" className="property-edit-dialog__field sheet-form__field">
            <input
              value={draft.city}
              onChange={(event) => setDraft({ ...draft, city: event.currentTarget.value })}
             aria-label="City"/>
          </FormField>
          <FormField label="State / province" requirement="optional" className="property-edit-dialog__field sheet-form__field">
            <input
              value={draft.state_province}
              onChange={(event) => setDraft({ ...draft, state_province: event.currentTarget.value })}
             aria-label="State / province"/>
          </FormField>
          <FormField label="Postal code" requirement="optional" className="property-edit-dialog__field sheet-form__field">
            <input
              value={draft.postal_code}
              onChange={(event) => setDraft({ ...draft, postal_code: event.currentTarget.value })}
             aria-label="Postal code"/>
          </FormField>
          <CountrySelect
            value={draft.country}
            onChange={(country) => setDraft({ ...draft, country })}
            required
            requirement="required"
          />
          <TimezoneSelect
            value={draft.timezone}
            onChange={(timezone) => setDraft({ ...draft, timezone })}
            required
            requirement="required"
          />
          <FormField label="Locale" requirement="optional" className="property-edit-dialog__field sheet-form__field">
            <input
              value={draft.locale}
              onChange={(event) => setDraft({ ...draft, locale: event.currentTarget.value })}
             aria-label="Locale"/>
          </FormField>
          <FormField label="Default currency" requirement="optional" className="property-edit-dialog__field sheet-form__field">
            <input
              value={draft.default_currency}
              onChange={(event) => setDraft({ ...draft, default_currency: event.currentTarget.value })}
              maxLength={3}
             aria-label="Default currency"/>
          </FormField>
        </FormModalGrid>
        <FormField label="Notes" requirement="optional" className="property-edit-dialog__field sheet-form__field">
          <AutoGrowTextarea
            value={draft.property_notes_md}
            onChange={(event) => setDraft({ ...draft, property_notes_md: event.currentTarget.value })}
            rows={5}
          />
        </FormField>
        {error && <p className="form-error" role="alert">{error}</p>}
    </FormModal>
  );
}
