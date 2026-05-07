import { Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useActiveAppRole } from "@/auth";
import { useTheme } from "@/context/ThemeContext";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useBannerHeightVar } from "@/lib/useBannerHeightVar";

interface RuntimeInfo {
  runtime: {
    demo_mode: boolean;
  };
}

// PreviewShell is the outermost layout: grain, optional demo banner,
// then the routed layout inside <Outlet />. Grain is mounted once at
// tree root (not per-page) so navigation doesn't flicker.
export default function PreviewShell() {
  const { resolved } = useTheme();
  const role = useActiveAppRole();
  const runtimeQ = useQuery({
    queryKey: qk.runtimeInfo(),
    queryFn: () => fetchJson<RuntimeInfo>("/api/v1/runtime/info"),
    retry: false,
    staleTime: Infinity,
  });
  useBannerHeightVar(runtimeQ.data?.runtime.demo_mode ?? false);

  return (
    <div className="surface" data-role={role} data-theme={resolved}>
      <img src="/grain.svg" alt="" aria-hidden="true" className="grain" />

      {runtimeQ.data?.runtime.demo_mode ? (
        <div className="demo-banner" role="note">
          Demo data - resets on inactivity
        </div>
      ) : null}

      <Outlet />
    </div>
  );
}
