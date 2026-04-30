import { type MessageKey, type MessageParamMap } from "@/i18n/catalogs/en-US";
import enUSBundle from "@/i18n/bundles/en-US.json";
import esBundle from "@/i18n/bundles/es.json";
import frBundle from "@/i18n/bundles/fr.json";
import pseudoBundle from "@/i18n/bundles/qps-ploc.json";
import { DEFAULT_LOCALE, PSEUDO_LOCALE, type SupportedLocale } from "@/i18n/locale";

type Catalog = Record<MessageKey, string>;
type MissingKeyMode = "throw" | "return-key";
type MessageParamValue = string | number;
type MessageArgs<K extends MessageKey> = K extends keyof MessageParamMap
  ? [params: MessageParamMap[K]]
  : [];

export interface TFunction {
  <K extends MessageKey>(key: K, ...args: MessageArgs<K>): string;
}

interface TranslatorOptions {
  missingKeyMode?: MissingKeyMode;
}

function catalogFor(locale: SupportedLocale): Catalog {
  if (locale === "fr") return frBundle as Catalog;
  if (locale === "es") return esBundle as Catalog;
  if (locale === PSEUDO_LOCALE) return pseudoBundle as Catalog;
  return enUSBundle as Catalog;
}

function missingKeyMode(): MissingKeyMode {
  return import.meta.env.PROD ? "return-key" : "throw";
}

function formatMessage(template: string, params: Record<string, MessageParamValue> = {}): string {
  return template.replace(/\{([A-Za-z0-9_]+)\}/g, (match, name: string) => {
    const value = params[name];
    return value === undefined ? match : String(value);
  });
}

export function createTranslator(
  locale: SupportedLocale = DEFAULT_LOCALE,
  options: TranslatorOptions = {},
): TFunction {
  const catalog = catalogFor(locale);
  const onMissing = options.missingKeyMode ?? missingKeyMode();

  return ((key: MessageKey, ...args: [Record<string, MessageParamValue>?]) => {
    const template = catalog[key];
    if (template === undefined) {
      if (onMissing === "throw") throw new Error(`Missing i18n key: ${key}`);
      return key;
    }
    return formatMessage(template, args[0]);
  }) as TFunction;
}

export const t = createTranslator(DEFAULT_LOCALE);
