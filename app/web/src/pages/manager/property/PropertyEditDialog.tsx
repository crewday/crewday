import { useEffect, useRef, useState, type FormEvent } from "react";
import type { Property } from "@/types/api";
import type { PropertyAddress, PropertyRecord } from "./types";

const PROPERTY_KINDS: readonly Property["kind"][] = ["residence", "vacation", "str", "mixed"];

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

export default function PropertyEditDialog({
  open,
  property,
  initialDraft,
  mode = "edit",
  saving,
  error,
  onSubmit,
  onClose,
}: PropertyEditDialogProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const [draft, setDraft] = useState<PropertyEditDraft>(() =>
    property ? draftFromProperty(property) : blankPropertyDraft(initialDraft)
  );

  useEffect(() => {
    if (open) setDraft(property ? draftFromProperty(property) : blankPropertyDraft(initialDraft));
  }, [initialDraft, open, property]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) {
      if (typeof dialog.showModal === "function") {
        if (!dialog.open) dialog.showModal();
      } else if (!dialog.open) {
        dialog.setAttribute("open", "");
      }
    } else if (dialog.open && typeof dialog.close === "function") {
      dialog.close();
    } else if (!open) {
      dialog.removeAttribute("open");
    }
  }, [open]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit(draft);
  }

  return (
    <dialog
      ref={dialogRef}
      className="modal modal--sheet"
      aria-labelledby="property-edit-dialog-title"
      onCancel={(event) => {
        if (saving) event.preventDefault();
      }}
      onClose={onClose}
    >
      <form className="modal__body form" onSubmit={submit}>
        <h3 id="property-edit-dialog-title" className="modal__title">
          {mode === "create" ? "Add property" : "Edit property"}
        </h3>
        <div className="form-grid form-grid--two">
          <label className="field">
            <span>Name</span>
            <input
              value={draft.name}
              onChange={(event) => setDraft({ ...draft, name: event.currentTarget.value })}
              required
            />
          </label>
          <label className="field">
            <span>Kind</span>
            <select
              value={draft.kind}
              onChange={(event) =>
                setDraft({ ...draft, kind: event.currentTarget.value as Property["kind"] })
              }
            >
              {PROPERTY_KINDS.map((kind) => (
                <option key={kind} value={kind}>{kind}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Address line 1</span>
            <input
              value={draft.line1}
              onChange={(event) => setDraft({ ...draft, line1: event.currentTarget.value })}
            />
          </label>
          <label className="field">
            <span>Address line 2</span>
            <input
              value={draft.line2}
              onChange={(event) => setDraft({ ...draft, line2: event.currentTarget.value })}
            />
          </label>
          <label className="field">
            <span>City</span>
            <input
              value={draft.city}
              onChange={(event) => setDraft({ ...draft, city: event.currentTarget.value })}
            />
          </label>
          <label className="field">
            <span>State / province</span>
            <input
              value={draft.state_province}
              onChange={(event) => setDraft({ ...draft, state_province: event.currentTarget.value })}
            />
          </label>
          <label className="field">
            <span>Postal code</span>
            <input
              value={draft.postal_code}
              onChange={(event) => setDraft({ ...draft, postal_code: event.currentTarget.value })}
            />
          </label>
          <label className="field">
            <span>Country</span>
            <input
              value={draft.country}
              onChange={(event) => setDraft({ ...draft, country: event.currentTarget.value })}
              maxLength={2}
              required
            />
          </label>
          <label className="field">
            <span>Timezone</span>
            <input
              value={draft.timezone}
              onChange={(event) => setDraft({ ...draft, timezone: event.currentTarget.value })}
              required
            />
          </label>
          <label className="field">
            <span>Locale</span>
            <input
              value={draft.locale}
              onChange={(event) => setDraft({ ...draft, locale: event.currentTarget.value })}
            />
          </label>
          <label className="field">
            <span>Default currency</span>
            <input
              value={draft.default_currency}
              onChange={(event) => setDraft({ ...draft, default_currency: event.currentTarget.value })}
              maxLength={3}
            />
          </label>
        </div>
        <label className="field">
          <span>Notes</span>
          <textarea
            value={draft.property_notes_md}
            onChange={(event) => setDraft({ ...draft, property_notes_md: event.currentTarget.value })}
            rows={5}
          />
        </label>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="modal__actions">
          <button
            type="button"
            className="btn btn--ghost"
            disabled={saving}
            onClick={() => dialogRef.current?.close()}
          >
            Cancel
          </button>
          <button type="submit" className="btn btn--moss" disabled={saving}>
            {saving ? "Saving..." : mode === "create" ? "Create property" : "Save property"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
