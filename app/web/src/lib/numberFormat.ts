export type NumericValue = number | null | undefined;

export type NumberFormatOptions = {
  locale?: string;
  fallback?: string;
};

export type DecimalFormatOptions = NumberFormatOptions & {
  minimumFractionDigits?: number;
  maximumFractionDigits?: number;
};

export type CompactNumberFormatOptions = NumberFormatOptions & {
  minimumFractionDigits?: number;
  maximumFractionDigits?: number;
};

export type PercentFormatOptions = DecimalFormatOptions & {
  input?: "ratio" | "percent";
};

const DEFAULT_LOCALE = "en-US";
const DEFAULT_FALLBACK = "";
const numberFormatCache = new Map<string, Intl.NumberFormat>();

function numberFormat(locale: string, options: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = JSON.stringify([locale, options]);
  const cached = numberFormatCache.get(key);
  if (cached) return cached;
  // react-doctor-disable-next-line react-doctor/js-hoist-intl -- Numeric helper formatters are cached by locale/options key.
  const formatter = new Intl.NumberFormat(locale, options);
  numberFormatCache.set(key, formatter);
  return formatter;
}

function resolveFractionDigits(
  minimumFractionDigits: number | undefined,
  maximumFractionDigits: number | undefined,
  defaultMaximumFractionDigits: number,
): Pick<
  Intl.NumberFormatOptions,
  "minimumFractionDigits" | "maximumFractionDigits"
> {
  const minimum = minimumFractionDigits ?? 0;
  const maximum = maximumFractionDigits ?? defaultMaximumFractionDigits;
  return {
    minimumFractionDigits: minimum,
    maximumFractionDigits: Math.max(maximum, minimum),
  };
}

function valueOrFallback(
  value: NumericValue,
  fallback: string | undefined,
): number | string {
  return value ?? fallback ?? DEFAULT_FALLBACK;
}

export function formatInteger(
  value: NumericValue,
  options: NumberFormatOptions = {},
): string {
  const resolved = valueOrFallback(value, options.fallback);
  if (typeof resolved === "string") {
    return resolved;
  }
  return numberFormat(options.locale ?? DEFAULT_LOCALE, {
    maximumFractionDigits: 0,
  }).format(resolved);
}

export function formatDecimal(
  value: NumericValue,
  options: DecimalFormatOptions = {},
): string {
  const resolved = valueOrFallback(value, options.fallback);
  if (typeof resolved === "string") {
    return resolved;
  }
  return numberFormat(options.locale ?? DEFAULT_LOCALE, {
    ...resolveFractionDigits(
      options.minimumFractionDigits,
      options.maximumFractionDigits,
      2,
    ),
  }).format(resolved);
}

export function formatCompactNumber(
  value: NumericValue,
  options: CompactNumberFormatOptions = {},
): string {
  const resolved = valueOrFallback(value, options.fallback);
  if (typeof resolved === "string") {
    return resolved;
  }
  return numberFormat(options.locale ?? DEFAULT_LOCALE, {
    notation: "compact",
    compactDisplay: "short",
    ...resolveFractionDigits(
      options.minimumFractionDigits,
      options.maximumFractionDigits,
      1,
    ),
  }).format(resolved);
}

export function formatPercent(
  value: NumericValue,
  options: PercentFormatOptions = {},
): string {
  const resolved = valueOrFallback(value, options.fallback);
  if (typeof resolved === "string") {
    return resolved;
  }
  const ratioValue = options.input === "percent" ? resolved / 100 : resolved;
  return numberFormat(options.locale ?? DEFAULT_LOCALE, {
    style: "percent",
    ...resolveFractionDigits(
      options.minimumFractionDigits,
      options.maximumFractionDigits,
      1,
    ),
  }).format(ratioValue);
}

export function formatContextWindow(
  value: NumericValue,
  options: CompactNumberFormatOptions = {},
): string {
  const resolved = valueOrFallback(value, options.fallback);
  if (typeof resolved === "string") {
    return resolved;
  }
  return `${formatCompactNumber(resolved, options)} ctx`;
}
