/**
 * Locale-aware money formatting for API minor-unit values.
 *
 * Minor-unit counts come from the ISO-4217 static table below; the
 * formatter never hardcodes "divide by 100".
 */

const MINOR_UNITS: Record<string, number> = {
  JPY: 0,
  KRW: 0,
  VND: 0,
  BHD: 3,
  KWD: 3,
  OMR: 3,
};
const moneyFormatCache = new Map<string, Intl.NumberFormat>();

function moneyFormat(locale: string, currency: string, digits: number): Intl.NumberFormat {
  const key = `${locale}:${currency}:${digits}`;
  const cached = moneyFormatCache.get(key);
  if (cached) return cached;
  // react-doctor-disable-next-line react-doctor/js-hoist-intl -- Currency formatters are cached by locale/currency/minor-unit key.
  const formatter = new Intl.NumberFormat(locale, {
    style: "currency",
    currency,
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  moneyFormatCache.set(key, formatter);
  return formatter;
}

export function formatMoney(
  minorAmount: number,
  currency: string,
  locale = "en-US",
): string {
  const digits = MINOR_UNITS[currency] ?? 2;
  const value = minorAmount / 10 ** digits;
  return moneyFormat(locale, currency, digits).format(value);
}
