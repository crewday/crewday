import type { SettingDefinition } from "@/types/api";

const SCOPE_LABELS: Record<string, string> = {
  W: "workspace",
  P: "property",
  U: "unit",
  WE: "work engagement",
  T: "task",
  E: "employee",
  workspace: "workspace",
};

const ENUM_LABELS: Record<string, Record<string, string>> = {
  "evidence.policy": {
    require: "Required",
    optional: "Optional",
    forbid: "Forbidden",
  },
  "bookings.pay_basis": {
    scheduled: "Scheduled time",
    actual: "Actual worked time",
  },
  "pay.frequency": {
    weekly: "Weekly",
    fortnightly: "Fortnightly",
    monthly: "Monthly",
  },
  "pay.week_start": {
    monday: "Monday",
    sunday: "Sunday",
  },
};

export function settingDraftFromValue(value: unknown): string {
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value;
  return "";
}

export function parseSettingDraft(def: SettingDefinition, draft: string): unknown {
  if (def.type === "bool") return draft === "true";
  if (def.type === "int") return Number(draft);
  return draft;
}

export function settingEnumOptionLabel(def: SettingDefinition, option: string): string {
  const label = ENUM_LABELS[def.key]?.[option];
  if (label) return label;
  return option
    .replaceAll("_", " ")
    .split(" ")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function settingScopeLabel(scope: string): string {
  return settingOverrideScopes(scope)
    .map((part) => SCOPE_LABELS[part] ?? part)
    .join(", ");
}

export function settingOverrideScopes(scope: string): string[] {
  return scope
    .split("/")
    .flatMap((part) => {
      const trimmed = part.trim();
      return trimmed ? [trimmed] : [];
    });
}

export function invalidIntegerSettingDraft(draft: string): boolean {
  return !Number.isInteger(Number(draft)) || draft.trim() === "";
}
