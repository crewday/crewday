// Shared helpers for the worker's expense surface (§09).
//
// Mock parity: these constants and the `confidenceClass` helper come
// straight from `mocks/web/src/pages/employee/MyExpensesPage.tsx` —
// keeping them in one place lets `SubmitExpenseForm`, `RecentExpenses`,
// and `ReceiptScanPanel` share the same chip tone and warn-class
// spelling without drift.

import type { ChipTone } from "@/components/common";
import type { ExpenseCategory, ExpenseStatus } from "@/types/api";

/**
 * Status → chip tone for the recent-expenses list. Mirrors the mock
 * verbatim so a Playwright diff against `MyExpensesPage.tsx.png` shows
 * zero pixel delta.
 */
export const STATUS_TONE: Record<ExpenseStatus, ChipTone> = {
  draft: "ghost",
  submitted: "sand",
  approved: "moss",
  rejected: "rust",
  reimbursed: "sky",
};

/**
 * Category radio options in the mock's display order. Worker picks one
 * per claim; the value lands in `expenses.category` server-side.
 */
export const CATEGORIES: ReadonlyArray<{ value: ExpenseCategory; label: string }> = [
  { value: "supplies", label: "Supplies" },
  { value: "fuel", label: "Fuel" },
  { value: "food", label: "Food" },
  { value: "transport", label: "Transport" },
  { value: "maintenance", label: "Maintenance" },
  { value: "other", label: "Other" },
];

/**
 * Confidence-band → field className.
 *
 * The mock paints a soft amber ring around any field whose OCR
 * confidence sits in `[0.6, 0.9)` (the "review me" band). Above 0.9 we
 * trust the autofill; below 0.6 the value is left blank altogether so
 * a `null` confidence and a high confidence both render unstyled.
 */
export function confidenceClass(c: number | null): string {
  if (c === null || c >= 0.9) return "";
  if (c >= 0.6) return "field--warn";
  return "";
}
