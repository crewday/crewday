import { useEffect } from "react";

// Mirrors the legacy chat.js syncBannerHeight helper — set the
// --banner-h CSS custom property so .phone--chat can size correctly
// (100dvh - banner). Called once from PreviewShell.
export function useBannerHeightVar(): void {
  useEffect(() => {
    const sync = (): void => {
      const banner = document.querySelector(".preview-banner");
      if (!banner) return;
      const h = banner.getBoundingClientRect().height;
      document.documentElement.style.setProperty("--banner-h", h + "px");
    };
    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);
}
