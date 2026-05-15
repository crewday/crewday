import { describe, expect, it } from "vitest";
import {
  formatCompactNumber,
  formatContextWindow,
  formatDecimal,
  formatInteger,
  formatPercent,
} from "@/lib/numberFormat";

describe("number formatting helpers", () => {
  it("formats integers with grouping separators", () => {
    expect(formatInteger(1234567)).toBe("1,234,567");
  });

  it("formats decimals with optional fraction digits", () => {
    expect(formatDecimal(1234)).toBe("1,234");
    expect(formatDecimal(1234.5)).toBe("1,234.5");
    expect(formatDecimal(1234.567, { maximumFractionDigits: 2 })).toBe("1,234.57");
    expect(formatDecimal(1234, { minimumFractionDigits: 2 })).toBe("1,234.00");
    expect(formatDecimal(1234, { minimumFractionDigits: 3 })).toBe("1,234.000");
  });

  it("normalizes conflicting fraction digit bounds", () => {
    expect(
      formatDecimal(1234.5, {
        minimumFractionDigits: 3,
        maximumFractionDigits: 1,
      }),
    ).toBe("1,234.500");
  });

  it("formats compact million-scale context windows", () => {
    expect(formatCompactNumber(1048576)).toBe("1M");
    expect(formatContextWindow(1048576)).toBe("1M ctx");
  });

  it("uses a clear fallback for nullish values", () => {
    expect(formatInteger(null)).toBe("");
    expect(formatDecimal(undefined, { fallback: "n/a" })).toBe("n/a");
    expect(formatContextWindow(null, { fallback: "unknown ctx" })).toBe("unknown ctx");
  });

  it("formats ratios and already-scaled percentages", () => {
    expect(formatPercent(0.125, { maximumFractionDigits: 1 })).toBe("12.5%");
    expect(formatPercent(0.125, { minimumFractionDigits: 2 })).toBe("12.50%");
    expect(formatPercent(12.5, { input: "percent", maximumFractionDigits: 1 })).toBe(
      "12.5%",
    );
  });
});
