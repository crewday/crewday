import { useEffect } from "react";

export function useBannerHeightVar(refreshKey: unknown = null): void {
  useEffect(() => {
    const sync = (): void => {
      const banners = document.querySelectorAll(".demo-banner, .shell-controls");
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
