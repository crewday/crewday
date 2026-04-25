// Pure helpers for projecting an `ExpenseScanResult` into the review
// form's controlled state. Lives outside the form module so the
// component file stays small enough to scan in one pass.

import type { ExpenseCategory, ExpenseScanResult } from "@/types/api";

export interface FieldValues {
  merchant: string;
  amount: string;
  currency: string;
  category: ExpenseCategory;
  note: string;
}

export interface FieldConfidences {
  merchant: number | null;
  amount: number | null;
  currency: number | null;
  category: number | null;
  note: number | null;
}

const EMPTY_VALUES: FieldValues = {
  merchant: "",
  amount: "",
  currency: "EUR",
  category: "other",
  note: "",
};

const EMPTY_CONF: FieldConfidences = {
  merchant: null,
  amount: null,
  currency: null,
  category: null,
  note: null,
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

export function deriveValues(scan: ExpenseScanResult | null): FieldValues {
  if (!scan) return { ...EMPTY_VALUES };
  const cents = fillIf(scan.total_amount_cents);
  return {
    merchant: fillIf(scan.vendor) ?? "",
    amount: cents !== null ? (cents / 100).toFixed(2) : "",
    currency: fillIf(scan.currency) ?? "EUR",
    category: fillIf(scan.category) ?? "other",
    note: fillIf(scan.note_md) ?? "",
  };
}

export function deriveConfidences(
  scan: ExpenseScanResult | null,
): FieldConfidences {
  if (!scan) return { ...EMPTY_CONF };
  return {
    merchant: scan.vendor.confidence,
    amount: scan.total_amount_cents.confidence,
    currency: scan.currency.confidence,
    category: scan.category.confidence,
    note: scan.note_md.confidence,
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
