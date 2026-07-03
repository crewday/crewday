import { describe, expect, it } from "vitest";
import { DEFAULT_LOCALE, PSEUDO_LOCALE, resolveLocale, toSupportedLocale } from "@/i18n";

describe("locale negotiation", () => {
  it("normalizes supported locale tags", () => {
    expect(toSupportedLocale("en")).toBe(DEFAULT_LOCALE);
    expect(toSupportedLocale("en_GB")).toBe(DEFAULT_LOCALE);
    expect(toSupportedLocale("qps-ploc")).toBe(PSEUDO_LOCALE);
    expect(toSupportedLocale("fr-FR")).toBe("fr");
    expect(toSupportedLocale("es-MX")).toBe("es");
  });

  it.each([
    {
      name: "query pseudo-locale",
      input: {
        search: "?locale=qps-ploc",
        preferredLocale: "en-US",
        navigatorLanguages: ["de-DE"],
        workspaceDefaultLocale: "en-US",
      },
      expected: PSEUDO_LOCALE,
    },
    {
      name: "preferred locale",
      input: {
        search: "",
        preferredLocale: "en-US",
        navigatorLanguages: ["de-DE"],
        workspaceDefaultLocale: "en-US",
      },
      expected: DEFAULT_LOCALE,
    },
    {
      name: "navigator languages",
      input: {
        search: "",
        preferredLocale: null,
        navigatorLanguages: ["fr-FR", "en-GB"],
        workspaceDefaultLocale: "en-US",
      },
      expected: "fr",
    },
    {
      name: "workspace default",
      input: {
        search: "",
        preferredLocale: null,
        navigatorLanguages: ["de-DE"],
        workspaceDefaultLocale: "en-US",
      },
      expected: DEFAULT_LOCALE,
    },
    {
      name: "final fallback",
      input: {
        search: "",
        preferredLocale: null,
        navigatorLanguages: ["de-DE"],
        workspaceDefaultLocale: "de-DE",
      },
      expected: DEFAULT_LOCALE,
    },
  ])("uses the $name precedence step", ({ input, expected }) => {
    expect(resolveLocale(input)).toBe(expected);
  });

  describe("placeholder-locale gating (§18 staged i18n)", () => {
    // In a production build fr/es ship English placeholder catalogs, so
    // automatic negotiation must never land on them — a French browser
    // would otherwise render the English placeholder as if translated.
    it("drops a placeholder preferred_locale in production and falls through", () => {
      expect(
        resolveLocale({
          search: "",
          preferredLocale: "fr-FR",
          navigatorLanguages: ["de-DE"],
          workspaceDefaultLocale: null,
          allowPlaceholderLocales: false,
        }),
      ).toBe(DEFAULT_LOCALE);
    });

    it("drops a placeholder navigator language in production", () => {
      expect(
        resolveLocale({
          search: "",
          preferredLocale: null,
          navigatorLanguages: ["es-MX", "en-GB"],
          workspaceDefaultLocale: null,
          allowPlaceholderLocales: false,
        }),
      ).toBe(DEFAULT_LOCALE);
    });

    it("drops a placeholder workspace default in production", () => {
      expect(
        resolveLocale({
          search: "",
          preferredLocale: null,
          navigatorLanguages: ["de-DE"],
          workspaceDefaultLocale: "fr",
          allowPlaceholderLocales: false,
        }),
      ).toBe(DEFAULT_LOCALE);
    });

    it("still resolves a placeholder preferred_locale when placeholders are allowed (dev)", () => {
      expect(
        resolveLocale({
          search: "",
          preferredLocale: "fr-FR",
          navigatorLanguages: ["de-DE"],
          workspaceDefaultLocale: null,
          allowPlaceholderLocales: true,
        }),
      ).toBe("fr");
    });

    it("honours an explicit ?locale= override for a placeholder even in production", () => {
      // The dev / QA affordance ignores the production gate so a
      // placeholder (or the pseudo-locale) can still be forced.
      expect(
        resolveLocale({
          search: "?locale=fr",
          preferredLocale: null,
          navigatorLanguages: ["en-US"],
          workspaceDefaultLocale: null,
          allowPlaceholderLocales: false,
        }),
      ).toBe("fr");
    });

    it("never auto-negotiates the pseudo-locale, even in dev", () => {
      expect(
        resolveLocale({
          search: "",
          preferredLocale: PSEUDO_LOCALE,
          navigatorLanguages: [PSEUDO_LOCALE],
          workspaceDefaultLocale: PSEUDO_LOCALE,
          allowPlaceholderLocales: true,
        }),
      ).toBe(DEFAULT_LOCALE);
    });
  });
});
