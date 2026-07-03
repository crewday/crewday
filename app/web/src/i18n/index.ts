export {
  DEFAULT_LOCALE,
  PSEUDO_LOCALE,
  resolveLocale,
  toSupportedLocale,
  type LocaleResolutionInput,
  type SupportedLocale,
} from "@/i18n/locale";
export { createTranslator, t, type TFunction } from "@/i18n/translator";
export { I18nProvider, useI18n, type I18nProviderProps } from "@/i18n/I18nProvider";
export { ConnectedI18nProvider } from "@/i18n/ConnectedI18nProvider";
export type { MessageKey, MessageParamMap } from "@/i18n/catalogs/en-US";
