import { useState, type FormEvent } from "react";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import CountrySelect from "@/components/CountrySelect";
import FormField from "@/components/FormField";
import FormModal, { FormModalGrid } from "@/components/FormModal";
import TimezoneSelect from "@/components/TimezoneSelect";
import type { Property } from "@/types/api";
import {
  blankPropertyDraft,
  draftFromProperty,
  PROPERTY_KIND_OPTIONS,
  type PropertyEditDraft,
} from "./PropertyEditDialog.lib";
import type { PropertyRecord } from "./types";

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

export default function PropertyEditDialog(props: PropertyEditDialogProps) {
  if (!props.open) return null;
  const key = props.property?.id ?? props.initialDraft?.name ?? "new-property";
  return <PropertyEditDialogForm key={key} {...props} />;
}

function PropertyEditDialogForm(props: PropertyEditDialogProps) {
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
