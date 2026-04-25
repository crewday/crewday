import { describe, expect, it } from "vitest";
import {
  deriveConfidences,
  deriveValues,
  minConfidence,
  todayDateInput,
} from "./scanDerivation";
import type { ExpenseScanResult } from "@/types/api";

// `scanDerivation` projects a `ReceiptScanPanel` autofill result into
// the worker form's controlled state. Field names match the server's
// `ExpenseClaimCreate` shape (`vendor`, `note_md`, `purchased_on` →
// `purchased_at`) so the mapping into
// `buildExpenseClaimCreatePayload` is a 1:1 spread.

function highConfidenceScan(): ExpenseScanResult {
  return {
    vendor: { value: "Carrefour", confidence: 0.95 },
    purchased_at: { value: "2026-04-15T09:30:00Z", confidence: 0.92 },
    currency: { value: "EUR", confidence: 0.98 },
    total_amount_cents: { value: 1234, confidence: 0.91 },
    category: { value: "supplies", confidence: 0.95 },
    note_md: { value: "Cleaning supplies", confidence: 0.7 },
    agent_question: null,
  };
}

describe("todayDateInput", () => {
  it("formats a Date as YYYY-MM-DD using the local calendar", () => {
    // `new Date(2026, 3, 5)` is April 5 2026 local — the
    // single-digit month/day exercises the zero-pad branch.
    expect(todayDateInput(new Date(2026, 3, 5))).toBe("2026-04-05");
  });

  it("uses local-calendar getters so a midnight-UTC stamp doesn't roll back a day", () => {
    // `new Date(2026, 3, 5, 0, 30)` is April 5 00:30 in any
    // negative-offset zone — would `toISOString().slice(0, 10)`
    // back-shift this to April 4? The helper uses local getters so
    // it must not.
    expect(todayDateInput(new Date(2026, 3, 5, 0, 30))).toBe("2026-04-05");
  });
});

describe("deriveValues", () => {
  it("seeds every field from a fully-confident scan", () => {
    const out = deriveValues(highConfidenceScan());
    expect(out.vendor).toBe("Carrefour");
    expect(out.amount).toBe("12.34");
    expect(out.currency).toBe("EUR");
    expect(out.category).toBe("supplies");
    expect(out.note_md).toBe("Cleaning supplies");
    expect(out.purchased_on).toBe("2026-04-15");
    // Property pin is never seeded from a scan — the worker picks.
    expect(out.property_id).toBe("");
  });

  it("falls back to today when the scan's purchased_at is below the confidence threshold", () => {
    const scan = highConfidenceScan();
    scan.purchased_at = { value: "2026-04-15T00:00:00Z", confidence: 0.2 };
    const out = deriveValues(scan);
    expect(out.purchased_on).toBe(todayDateInput());
  });

  it("returns a fully-blank form (with today's date and EUR default) when no scan", () => {
    const out = deriveValues(null);
    expect(out.vendor).toBe("");
    expect(out.amount).toBe("");
    expect(out.currency).toBe("EUR");
    expect(out.category).toBe("other");
    expect(out.note_md).toBe("");
    expect(out.purchased_on).toBe(todayDateInput());
    expect(out.property_id).toBe("");
  });

  it("blanks low-confidence fields rather than seeding them with the LLM's guess", () => {
    const scan = highConfidenceScan();
    scan.vendor = { value: "Carrefour", confidence: 0.4 };
    scan.total_amount_cents = { value: 1234, confidence: 0.5 };
    const out = deriveValues(scan);
    expect(out.vendor).toBe("");
    expect(out.amount).toBe("");
  });
});

describe("deriveConfidences", () => {
  it("mirrors every per-field confidence onto the same key the form uses", () => {
    const out = deriveConfidences(highConfidenceScan());
    expect(out.vendor).toBeCloseTo(0.95);
    expect(out.purchased_on).toBeCloseTo(0.92);
    expect(out.currency).toBeCloseTo(0.98);
    expect(out.amount).toBeCloseTo(0.91);
    expect(out.category).toBeCloseTo(0.95);
    expect(out.note_md).toBeCloseTo(0.7);
  });

  it("returns nulls across the board for a manual-entry form", () => {
    const out = deriveConfidences(null);
    for (const v of Object.values(out)) {
      expect(v).toBeNull();
    }
  });
});

describe("minConfidence", () => {
  it("returns the weakest non-null confidence across the form fields", () => {
    const c = deriveConfidences(highConfidenceScan());
    // The note's 0.7 is the lowest in the fixture above.
    expect(minConfidence(c)).toBeCloseTo(0.7);
  });

  it("returns null when no field carries a confidence value", () => {
    expect(minConfidence(deriveConfidences(null))).toBeNull();
  });
});
