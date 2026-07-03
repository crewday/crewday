export const DEFAULT_LOCALE = "en-US";
export const PSEUDO_LOCALE = "qps-ploc";

const SUPPORTED_LOCALES = [DEFAULT_LOCALE, "fr", "es", PSEUDO_LOCALE] as const;

export type SupportedLocale = (typeof SUPPORTED_LOCALES)[number];

// fr/es ship English placeholder catalogs (§18 "staged i18n"). They are
// known tags — reachable via the explicit `?locale=` dev override — but
// must NOT be picked by automatic negotiation (preferred → navigator →
// workspace default) in a production build, or a French browser would
// silently render the English placeholder as if it were a real
// translation. The pseudo-locale is likewise never auto-negotiated.
const PLACEHOLDER_LOCALES = new Set<SupportedLocale>(["fr", "es"]);

export interface LocaleResolutionInput {
  search?: string;
  preferredLocale?: string | null;
  navigatorLanguages?: readonly string[];
  workspaceDefaultLocale?: string | null;
  /**
   * Whether placeholder locales (fr/es) may be selected by automatic
   * negotiation. Defaults to dev builds only (`import.meta.env.DEV`);
   * production auto-negotiation resolves to `en-US` until real
   * translations land. The explicit `?locale=` override ignores this
   * gate so QA can still exercise a placeholder or the pseudo-locale.
   */
  allowPlaceholderLocales?: boolean;
}

function isNegotiable(locale: SupportedLocale, allowPlaceholders: boolean): boolean {
  if (locale === PSEUDO_LOCALE) return false;
  if (PLACEHOLDER_LOCALES.has(locale)) return allowPlaceholders;
  return true;
}

function currentSearch(): string {
  return typeof window === "undefined" ? "" : window.location.search;
}

function currentNavigatorLanguages(): readonly string[] {
  if (typeof navigator === "undefined") return [];
  return navigator.languages.length > 0 ? navigator.languages : [navigator.language];
}

export function toSupportedLocale(value: string | null | undefined): SupportedLocale | null {
  if (!value) return null;
  const normalized = value.trim().replaceAll("_", "-").toLowerCase();
  if (normalized === PSEUDO_LOCALE) return PSEUDO_LOCALE;
  if (normalized === "en" || normalized.startsWith("en-")) return DEFAULT_LOCALE;
  if (normalized === "fr" || normalized.startsWith("fr-")) return "fr";
  if (normalized === "es" || normalized.startsWith("es-")) return "es";
  return null;
}

export function resolveLocale(input: LocaleResolutionInput = {}): SupportedLocale {
  // Explicit override wins and honours every known tag — the dev /
  // pseudo-locale affordance (`?locale=qps-ploc`, `?locale=fr`).
  const params = new URLSearchParams(input.search ?? currentSearch());
  const queryLocale = toSupportedLocale(params.get("locale"));
  if (queryLocale) return queryLocale;

  const allowPlaceholders = input.allowPlaceholderLocales ?? import.meta.env.DEV;
  const negotiate = (value: string | null | undefined): SupportedLocale | null => {
    const locale = toSupportedLocale(value);
    return locale && isNegotiable(locale, allowPlaceholders) ? locale : null;
  };

  const preferredLocale = negotiate(input.preferredLocale);
  if (preferredLocale) return preferredLocale;

  const languages = input.navigatorLanguages ?? currentNavigatorLanguages();
  for (const language of languages) {
    const locale = negotiate(language);
    if (locale) return locale;
  }

  return negotiate(input.workspaceDefaultLocale) ?? DEFAULT_LOCALE;
}
