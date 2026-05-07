import type { SiteLink } from "@/content/en/site";

const DEFAULT_APP_ORIGIN = "https://app.crew.day";

function normalizeOrigin(value: string | undefined): string {
  if (!value) {
    return DEFAULT_APP_ORIGIN;
  }

  return value.replace(/\/+$/u, "") || DEFAULT_APP_ORIGIN;
}

const appOrigin = normalizeOrigin(import.meta.env.PUBLIC_CREWDAY_APP_ORIGIN);

export const appLinks = {
  signup: {
    label: "Sign up",
    href: `${appOrigin}/signup/start`,
  },
  login: {
    label: "Log in",
    href: `${appOrigin}/login`,
  },
} as const satisfies Record<string, SiteLink>;
