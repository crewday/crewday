// Pure helpers for projecting an `ExpenseScanResult` into the review
// form's controlled state. Lives outside the form module so the
// component file stays small enough to scan in one pass.
//
// The keys here match the server-side `ExpenseClaimCreate` field
// names verbatim (`vendor`, `note_md`, `purchased_on` → `purchased_at`)
// so the form's local state can flow into `buildExpenseClaimCreatePayload`
// without an intermediate rename pass.

import type { ExpenseCategory, ExpenseScanResult } from "@/types/api";

export interface FieldValues {
  vendor: string;
  /** `YYYY-MM-DD` from a native date picker. */
  purchased_on: string;
  amount: string;
  currency: string;
  category: ExpenseCategory;
  note_md: string;
  /** Optional property pin — empty string means "unset". */
  property_id: string;
}

export interface FieldConfidences {
  vendor: number | null;
  purchased_on: number | null;
  amount: number | null;
  currency: number | null;
  category: number | null;
  note_md: number | null;
}

/**
 * Today in the worker's local timezone, formatted as `YYYY-MM-DD` —
 * the shape `<input type="date">` consumes. Built without `toISOString()`
 * because that strips the local-timezone offset and would surface as
 * "yesterday" for any worker east of GMT after 12am UTC.
 */
export function todayDateInput(now: Date = new Date()): string {
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

const EMPTY_CONF: FieldConfidences = {
  vendor: null,
  purchased_on: null,
  amount: null,
  currency: null,
  category: null,
  note_md: null,
};

/**
 * Apply the mock's confidence-gating: fill the field if the OCR is at
 * least `threshold` sure, otherwise leave it blank so the worker types
 * it themselves. Returning `null` rather than the typed default keeps
 * the caller in charge of which empty-state to render (e.g. "EUR" vs.
 * `""` for currency).
 */
function fillIf<T>(
  field: { value: T; confidence: number },
  threshold = 0.6,
): T | null {
  return field.confidence >= threshold ? field.value : null;
}

/**
 * Reduce an ISO-8601 timestamp to its calendar `YYYY-MM-DD` slice.
 * Returns `null` for an unparseable string so a malformed scan
 * doesn't poison the form's date input.
 */
function dateInputFromIso(iso: string): string | null {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return todayDateInput(d);
}

export function deriveValues(scan: ExpenseScanResult | null): FieldValues {
  if (!scan) {
    return {
      vendor: "",
      purchased_on: todayDateInput(),
      amount: "",
      currency: "EUR",
      category: "other",
      note_md: "",
      property_id: "",
    };
  }
  const cents = fillIf(scan.total_amount_cents);
  const purchasedIso = fillIf(scan.purchased_at);
  return {
    vendor: fillIf(scan.vendor) ?? "",
    purchased_on:
      (purchasedIso !== null ? dateInputFromIso(purchasedIso) : null)
        ?? todayDateInput(),
    amount: cents !== null ? (cents / 100).toFixed(2) : "",
    currency: fillIf(scan.currency) ?? "EUR",
    category: fillIf(scan.category) ?? "other",
    note_md: fillIf(scan.note_md) ?? "",
    property_id: "",
  };
}

export function deriveConfidences(
  scan: ExpenseScanResult | null,
): FieldConfidences {
  if (!scan) return { ...EMPTY_CONF };
  return {
    vendor: scan.vendor.confidence,
    purchased_on: scan.purchased_at.confidence,
    amount: scan.total_amount_cents.confidence,
    currency: scan.currency.confidence,
    category: scan.category.confidence,
    note_md: scan.note_md.confidence,
  };
}

/**
 * §09 stores `ocr_confidence` as the *minimum* across fields so an
 * audit row instantly shows the weakest link. Manual entries (no
 * scan) carry `null`.
 */
export function minConfidence(conf: FieldConfidences): number | null {
  // `Object.values` infers `(number | null)[]` directly from
  // `FieldConfidences` under strict TypeScript — no cast required.
  const values = Object.values(conf).filter((c): c is number => c !== null);
  return values.length > 0 ? Math.min(...values) : null;
}
