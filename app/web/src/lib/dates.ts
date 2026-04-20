// PLACEHOLDER — real impl lands in cd-qdsl. DO NOT USE FOR PRODUCTION
// DECISIONS.
//
// Copied verbatim from `mocks/web/src/lib/dates.ts` so component ports
// compile. The helpers are self-contained and already final-shape; the
// cd-qdsl lib port just moves them into the production module tree.
//
// Locale-parameterized date/time formatting helpers. Default locale is
// "en-GB" (preserving the behaviour of the former per-page functions).

export function fmtDate(
  iso: string,
  locale = "en-GB",
  opts?: Intl.DateTimeFormatOptions,
): string {
  return new Date(iso).toLocaleDateString(
    locale,
    opts ?? { day: "2-digit", month: "short" },
  );
}

export function fmtTime(iso: string, locale = "en-GB"): string {
  return new Date(iso).toLocaleTimeString(locale, {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function fmtDateTime(iso: string, locale = "en-GB"): string {
  return fmtDate(iso, locale) + " \u00b7 " + fmtTime(iso, locale);
}
