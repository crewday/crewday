type CrewdayBootstrap = {
  cspNonce?: string;
  publicSiteUrl?: string | null;
};

declare global {
  interface Window {
    __CREWDAY__?: CrewdayBootstrap;
  }
}

export function configuredPublicSiteUrl(): string | null {
  if (window.__CREWDAY__ && "publicSiteUrl" in window.__CREWDAY__) {
    return normalisePublicSiteUrl(window.__CREWDAY__.publicSiteUrl);
  }
  return normalisePublicSiteUrl(import.meta.env.VITE_CREWDAY_PUBLIC_SITE_URL);
}

function normalisePublicSiteUrl(value: string | null | undefined): string | null {
  const raw = value?.trim();
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    return url.href;
  } catch {
    return null;
  }
}
