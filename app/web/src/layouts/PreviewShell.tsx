import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Monitor, Moon, Sun } from "lucide-react";
import { useRole } from "@/context/RoleContext";
import { useTheme } from "@/context/ThemeContext";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useBannerHeightVar } from "@/lib/useBannerHeightVar";

const STYLEGUIDE_ENABLED =
  import.meta.env.DEV ||
  import.meta.env.VITE_CREWDAY_STAGING === "1" ||
  import.meta.env.VITE_CREWDAY_STAGING === "true";

interface RuntimeInfo {
  runtime: {
    demo_mode: boolean;
  };
}

function isGuestPath(pathname: string): boolean {
  return pathname.startsWith("/guest/") || /^\/w\/[^/]+\/guest\//.test(pathname);
}

// PreviewShell is the outermost layout: grain, sticky preview banner,
// then the routed layout inside <Outlet />. Grain is mounted once at
// tree root (not per-page) so navigation doesn't flicker.
export default function PreviewShell() {
  const { role, setRole } = useRole();
  const { theme, resolved, toggle } = useTheme();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const runtimeQ = useQuery({
    queryKey: qk.runtimeInfo(),
    queryFn: () => fetchJson<RuntimeInfo>("/api/v1/runtime/info"),
    retry: false,
    staleTime: Infinity,
  });
  useBannerHeightVar(runtimeQ.data?.runtime.demo_mode ?? false);

  // Pages that don't render role-specific content: pill clicks should
  // still navigate (so they have a visible effect), but neither pill
  // should display as active while the user is here. Public auth flows
  // are role-agnostic AND should keep the user in place.
  const roleNeutral =
    (STYLEGUIDE_ENABLED && pathname === "/styleguide") ||
    pathname === "/login" ||
    pathname === "/recover" ||
    pathname.startsWith("/accept/") ||
    isGuestPath(pathname);
  const stayOnRoleSwitch =
    pathname === "/login" ||
    pathname === "/recover" ||
    pathname.startsWith("/accept/") ||
    isGuestPath(pathname);

  const switchRole = (r: typeof role) => {
    setRole(r);
    if (!stayOnRoleSwitch) {
      const next =
        r === "employee" ? "/today"
        : r === "client" ? "/portfolio"
        : "/dashboard";
      navigate(next);
    }
  };

  return (
    <div className="surface" data-role={role} data-theme={resolved}>
      <img src="/grain.svg" alt="" aria-hidden="true" className="grain" />

      {runtimeQ.data?.runtime.demo_mode ? (
        <div className="demo-banner" role="note">
          Demo data - resets on inactivity
        </div>
      ) : null}

      <div className="preview-banner">
        <span className="preview-banner__badge">PREVIEW</span>
        <span className="preview-banner__note">Interactive mocks · no real data</span>
        <nav className="preview-banner__switch" aria-label="Preview controls">
          <button
            type="button"
            className={"pill" + (!roleNeutral && role === "employee" ? " pill--active" : "")}
            onClick={() => switchRole("employee")}
          >
            Employee
          </button>
          <button
            type="button"
            className={"pill" + (!roleNeutral && role === "manager" ? " pill--active" : "")}
            onClick={() => switchRole("manager")}
          >
            Manager
          </button>
          <button
            type="button"
            className={"pill" + (!roleNeutral && role === "client" ? " pill--active" : "")}
            onClick={() => switchRole("client")}
          >
            Client
          </button>
          <button
            type="button"
            className="pill pill--ghost preview-banner__theme"
            aria-label={"Theme: " + theme + " (click to cycle)"}
            title={"Theme: " + theme}
            onClick={toggle}
          >
            {theme === "light" ? (
              <Sun size={14} aria-hidden="true" />
            ) : theme === "dark" ? (
              <Moon size={14} aria-hidden="true" />
            ) : (
              <Monitor size={14} aria-hidden="true" />
            )}
          </button>
          {STYLEGUIDE_ENABLED ? (
            <Link to="/styleguide" className="pill pill--ghost">§ styleguide</Link>
          ) : null}
        </nav>
      </div>

      <Outlet />
    </div>
  );
}
