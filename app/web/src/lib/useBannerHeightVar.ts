// PLACEHOLDER — real impl lands with the PreviewShell + AgentSidebar
// follow-ups (cd-k69n). DO NOT USE FOR PRODUCTION DECISIONS.
//
// Sets the `--banner-h` custom property so `.phone--chat` can size
// against `100dvh - banner`. Real impl mirrors
// `mocks/web/src/lib/useBannerHeightVar.ts`.
import { useEffect } from "react";

export function useBannerHeightVar(refreshKey: unknown = null): void {
  useEffect(() => {
    const sync = (): void => {
      const banners = document.querySelectorAll(".demo-banner, .preview-banner");
      const h = Array.from(banners).reduce(
        (total, banner) => total + banner.getBoundingClientRect().height,
        0,
      );
      document.documentElement.style.setProperty("--banner-h", h + "px");
    };
    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, [refreshKey]);
}
