import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Chip } from "@/components/common";
import {
  InlineNumberField,
  InlineSelectField,
  InlineTableForm,
  type InlineTableColumn,
  type InlineTableRow,
} from "@/components/InlineTableForm";
import { ApiError, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { EntitySettingsPayload, Property, SettingDefinition } from "@/types/api";
import { formatValue } from "./lib/propertyFormatters";
import type { PropertyDetail } from "./types";
import {
  invalidIntegerSettingDraft,
  parseSettingDraft,
  settingDraftFromValue,
  settingEnumOptionLabel,
  settingOverrideScopes,
  settingScopeLabel,
} from "../settingsEditor";

interface PropertySettingDraft {
  value: string;
}

interface PropertySettingMutation {
  key: string;
  value: unknown;
}

const INHERITED_SETTING_DRAFT_VALUE = "__crewday_inherited_setting__";

function hasPropertyScope(def: SettingDefinition): boolean {
  return settingOverrideScopes(def.override_scope).includes("P");
}

function hasPropertyOverride(overrides: Record<string, unknown>, key: string): boolean {
  return key in overrides && overrides[key] !== null;
}

function settingErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.userMessage ?? error.detail ?? error.message;
  }
  if (error instanceof Error && error.message) return error.message;
  return "Setting could not be saved.";
}

function patchOverride(overrides: Record<string, unknown>, key: string, value: unknown): Record<string, unknown> {
  const next = { ...overrides };
  if (value === null) {
    delete next[key];
  } else {
    next[key] = value;
  }
  return next;
}

function updatePropertyOverrides<T extends { settings_override: Record<string, unknown> }>(
  item: T,
  key: string,
  value: unknown,
): T {
  return {
    ...item,
    settings_override: patchOverride(item.settings_override, key, value),
  };
}

function propertySettingDraftValue(
  key: string,
  def: SettingDefinition,
  overrides: Record<string, unknown>,
  resolved: Record<string, { value: unknown; source: string }>,
): string {
  if (!hasPropertyOverride(overrides, key)) return INHERITED_SETTING_DRAFT_VALUE;
  return settingDraftFromValue(resolved[key]?.value ?? def.catalog_default);
}

function parsePropertySettingDraft(def: SettingDefinition, draft: string): unknown {
  if (draft === INHERITED_SETTING_DRAFT_VALUE) return null;
  return parseSettingDraft(def, draft);
}

function SettingsOverrideValueEditor({
  def,
  value,
  resolvedValue,
  update,
  disabled,
}: {
  def: SettingDefinition;
  value: string;
  resolvedValue: unknown;
  update: (patch: Partial<PropertySettingDraft>) => void;
  disabled: boolean;
}) {
  if (def.type === "bool") {
    return (
      <InlineSelectField
        value={value}
        options={[
          { value: INHERITED_SETTING_DRAFT_VALUE, label: "Inherited" },
          { value: "true", label: "Yes" },
          { value: "false", label: "No" },
        ]}
        onChange={(nextValue) => update({ value: nextValue })}
        disabled={disabled}
        ariaLabel={def.label}
      />
    );
  }
  if (def.type === "enum") {
    return (
      <InlineSelectField
        value={value}
        options={[
          { value: INHERITED_SETTING_DRAFT_VALUE, label: "Inherited" },
          ...(def.enum_values ?? []).map((option) => ({
            value: option,
            label: settingEnumOptionLabel(def, option),
          })),
        ]}
        onChange={(nextValue) => update({ value: nextValue })}
        disabled={disabled}
        ariaLabel={def.label}
      />
    );
  }
  if (value === INHERITED_SETTING_DRAFT_VALUE) {
    return (
      <InlineSelectField
        value={value}
        options={[
          { value: INHERITED_SETTING_DRAFT_VALUE, label: "Inherited" },
          {
            value: settingDraftFromValue(resolvedValue ?? def.catalog_default),
            label: "Set value",
          },
        ]}
        onChange={(nextValue) => update({ value: nextValue })}
        disabled={disabled}
        ariaLabel={def.label}
      />
    );
  }
  return (
    <span className="settings-override-source">
      <InlineNumberField
        value={value}
        onChange={(nextValue) => update({ value: nextValue })}
        disabled={disabled}
        ariaLabel={def.label}
        ariaInvalid={invalidIntegerSettingDraft(value)}
      />
      <button
        type="button"
        className="btn btn--ghost btn--sm"
        disabled={disabled}
        onClick={() => update({ value: INHERITED_SETTING_DRAFT_VALUE })}
      >
        Inherited
      </button>
    </span>
  );
}

export default function SettingsOverridePanel({
  propertyId,
  overrides,
  resolved,
  catalog,
}: {
  propertyId: string;
  overrides: Record<string, unknown>;
  resolved: Record<string, { value: unknown; source: string }>;
  catalog: SettingDefinition[];
}) {
  const queryClient = useQueryClient();
  const [editedDrafts, setEditedDrafts] = useState<Map<string, PropertySettingDraft>>(new Map());
  const [rowErrors, setRowErrors] = useState<Map<string, string>>(new Map());
  const propertyScoped = useMemo(() => catalog.filter(hasPropertyScope), [catalog]);
  const definitionsByKey = useMemo(
    () => new Map(propertyScoped.map((def) => [def.key, def])),
    [propertyScoped],
  );

  const saveSetting = useMutation({
    mutationFn: ({ key, value }: PropertySettingMutation) =>
      fetchJson<EntitySettingsPayload>("/api/v1/properties/" + propertyId + "/settings", {
        method: "PATCH",
        body: { [key]: value },
      }),
    onSuccess: async (next, variables) => {
      queryClient.setQueryData(qk.propertySettings(propertyId), next);
      queryClient.setQueryData<PropertyDetail | undefined>(qk.property(propertyId), (current) => (
        current
          ? { ...current, property: updatePropertyOverrides(current.property, variables.key, variables.value) }
          : current
      ));
      queryClient.setQueryData<Property[] | undefined>(qk.properties(), (current) => (
        current?.map((property) =>
          property.id === propertyId ? updatePropertyOverrides(property, variables.key, variables.value) : property,
        )
      ));
      setEditedDrafts((current) => {
        const nextDrafts = new Map(current);
        nextDrafts.delete(variables.key);
        return nextDrafts;
      });
      setRowErrors((current) => {
        const nextErrors = new Map(current);
        nextErrors.delete(variables.key);
        return nextErrors;
      });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: qk.propertySettings(propertyId) }),
        queryClient.invalidateQueries({ queryKey: qk.property(propertyId) }),
        queryClient.invalidateQueries({ queryKey: qk.properties() }),
      ]);
    },
    onError: (error, variables) => {
      setRowErrors((current) => {
        const nextErrors = new Map(current);
        nextErrors.set(variables.key, settingErrorMessage(error));
        return nextErrors;
      });
    },
  });

  const rows = useMemo(
    (): InlineTableRow<PropertySettingDraft>[] => propertyScoped.map((def) => {
      const res = resolved[def.key];
      const editedDraft = editedDrafts.get(def.key);
      const committedValue = propertySettingDraftValue(def.key, def, overrides, resolved);
      const savingThisRow = saveSetting.isPending && saveSetting.variables?.key === def.key;
      const source = res?.source ?? "catalog";
      return {
        id: def.key,
        label: def.label,
        draft: editedDraft ?? { value: committedValue },
        committedDraft: { value: committedValue },
        editing: editedDraft !== undefined,
        dirty: editedDraft !== undefined && editedDraft.value !== committedValue,
        saving: savingThisRow,
        disabled: saveSetting.isPending && !savingThisRow,
        error: rowErrors.get(def.key) ? <span role="alert">{rowErrors.get(def.key)}</span> : undefined,
        validation: editedDraft && editedDraft.value !== INHERITED_SETTING_DRAFT_VALUE
          && def.type === "int" && invalidIntegerSettingDraft(editedDraft.value)
          ? "Enter a whole number."
          : undefined,
        meta: def.description,
        isNew: source !== "property" && !hasPropertyOverride(overrides, def.key),
      };
    }),
    [editedDrafts, overrides, propertyScoped, resolved, rowErrors, saveSetting.isPending, saveSetting.variables],
  );

  const columns = useMemo(
    (): InlineTableColumn<PropertySettingDraft>[] => [
      {
        key: "setting",
        header: "Setting",
        width: { flex: 1.35, min: 190 },
        renderRead: ({ row }) => {
          const def = definitionsByKey.get(row.id);
          return (
            <span title={def?.description}>
              <code className="inline-code">{row.id}</code>
              <span className="muted setting-label">{def?.label ?? row.label}</span>
            </span>
          );
        },
        renderEdit: ({ row }) => {
          const def = definitionsByKey.get(row.id);
          return (
            <span title={def?.description}>
              <code className="inline-code">{row.id}</code>
              <span className="muted setting-label">{def?.label ?? row.label}</span>
            </span>
          );
        },
      },
      {
        key: "value",
        header: "Override value",
        width: { flex: 1, min: 160 },
        renderRead: ({ row }) => {
          const hasOverride = hasPropertyOverride(overrides, row.id);
          const res = resolved[row.id];
          return hasOverride ? (
            <strong>{formatValue(res?.value)}</strong>
          ) : (
            <span className="muted">Inherited</span>
          );
        },
        renderEdit: ({ row, update, disabled }) => {
          const def = definitionsByKey.get(row.id);
          if (!def) return null;
          return (
            <SettingsOverrideValueEditor
              def={def}
              value={row.draft.value}
              resolvedValue={resolved[row.id]?.value}
              update={update}
              disabled={disabled}
            />
          );
        },
      },
      {
        key: "effective",
        header: "Effective value",
        width: { flex: 0.9, min: 140 },
        renderRead: ({ row }) => <span>{formatValue(resolved[row.id]?.value)}</span>,
        renderEdit: ({ row }) => <span>{formatValue(resolved[row.id]?.value)}</span>,
      },
      {
        key: "source",
        header: "Source",
        width: { flex: 1.05, min: 190 },
        renderRead: ({ row }) => {
          const res = resolved[row.id];
          if (res?.source === "property" || hasPropertyOverride(overrides, row.id)) {
            return (
              <span className="settings-override-source">
                <Chip tone="moss" size="sm">property override</Chip>
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  disabled={saveSetting.isPending}
                  onClick={() => saveSetting.mutate({ key: row.id, value: null })}
                >
                  Clear
                </button>
              </span>
            );
          }
          return <span className="muted">inherited ({res?.source ?? "catalog"})</span>;
        },
        renderEdit: ({ row }) => {
          const def = definitionsByKey.get(row.id);
          return (
            <span className="muted">
              {hasPropertyOverride(overrides, row.id)
                ? "Editing property override"
                : "Will create property override"}
              {def ? <> · {settingScopeLabel(def.override_scope)}</> : null}
            </span>
          );
        },
      },
    ],
    [definitionsByKey, overrides, resolved, saveSetting],
  );

  return (
    <div className="panel">
      <header className="panel__head"><h2>Settings overrides</h2></header>
      <p className="muted">
        Property-scoped settings. Overridden values take precedence over workspace defaults.
      </p>
      <InlineTableForm
        ariaLabel="Property settings overrides"
        columns={columns}
        rows={rows}
        saveMode="explicit"
        onDraftChange={(rowId, patch) => {
          const def = definitionsByKey.get(rowId);
          if (!def) return;
          setEditedDrafts((current) => {
            const currentDraft = current.get(rowId) ?? {
              value: propertySettingDraftValue(rowId, def, overrides, resolved),
            };
            const nextDrafts = new Map(current);
            nextDrafts.set(rowId, { ...currentDraft, ...patch });
            return nextDrafts;
          });
          setRowErrors((current) => {
            const nextErrors = new Map(current);
            nextErrors.delete(rowId);
            return nextErrors;
          });
        }}
        onEdit={(rowId) => {
          const def = definitionsByKey.get(rowId);
          if (!def) return;
          setEditedDrafts((current) => {
            const nextDrafts = new Map(current);
            nextDrafts.set(rowId, {
              value: propertySettingDraftValue(rowId, def, overrides, resolved),
            });
            return nextDrafts;
          });
          setRowErrors((current) => {
            const nextErrors = new Map(current);
            nextErrors.delete(rowId);
            return nextErrors;
          });
        }}
        onSave={(rowId) => {
          const def = definitionsByKey.get(rowId);
          const draft = editedDrafts.get(rowId);
          if (
            !def
            || !draft
            || (
              draft.value !== INHERITED_SETTING_DRAFT_VALUE
              && def.type === "int"
              && invalidIntegerSettingDraft(draft.value)
            )
          ) return;
          const committedValue = propertySettingDraftValue(rowId, def, overrides, resolved);
          if (draft.value === committedValue) {
            setEditedDrafts((current) => {
              const nextDrafts = new Map(current);
              nextDrafts.delete(rowId);
              return nextDrafts;
            });
            setRowErrors((current) => {
              const nextErrors = new Map(current);
              nextErrors.delete(rowId);
              return nextErrors;
            });
            return;
          }
          saveSetting.mutate({ key: rowId, value: parsePropertySettingDraft(def, draft.value) });
        }}
        onCancel={(rowId) => {
          setEditedDrafts((current) => {
            const nextDrafts = new Map(current);
            nextDrafts.delete(rowId);
            return nextDrafts;
          });
          setRowErrors((current) => {
            const nextErrors = new Map(current);
            nextErrors.delete(rowId);
            return nextErrors;
          });
        }}
        getRowLabel={(row) => row.label ?? row.id}
        emptyState={<p className="muted">No property-scoped settings are available.</p>}
      />
    </div>
  );
}
