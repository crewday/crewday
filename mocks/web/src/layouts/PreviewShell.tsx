import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useRole } from "@/context/RoleContext";
import { useTheme } from "@/context/ThemeContext";
import { useBannerHeightVar } from "@/lib/useBannerHeightVar";

// PreviewShell is the outermost layout: grain, sticky preview banner,
// then the routed layout inside <Outlet />. Grain is mounted once at
// tree root (not per-page) so navigation doesn't flicker.
export default function PreviewShell() {
  const { role, setRole } = useRole();
  const { theme, toggle } = useTheme();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  useBannerHeightVar();

  const switchRole = (r: typeof role) => {
    setRole(r);
    const stay =
      pathname === "/styleguide" ||
      pathname === "/login" ||
      pathname === "/recover" ||
      pathname.startsWith("/enroll/") ||
      pathname.startsWith("/guest/");
    if (!stay) {
      navigate(r === "employee" ? "/today" : "/dashboard");
    }
  };

  return (
    <div className="surface" data-role={role} data-theme={theme}>
      <img src="/grain.svg" alt="" aria-hidden="true" className="grain" />

      <div className="preview-banner">
        <span className="preview-banner__badge">PREVIEW</span>
        <span className="preview-banner__note">Interactive mocks · no real data</span>
        <nav className="preview-banner__switch" aria-label="Preview controls">
          <button
            type="button"
            className={"pill" + (role === "employee" ? " pill--active" : "")}
            onClick={() => switchRole("employee")}
          >
            Employee
          </button>
          <button
            type="button"
            className={"pill" + (role === "manager" ? " pill--active" : "")}
            onClick={() => switchRole("manager")}
          >
            Manager
          </button>
          <button
            type="button"
            className="pill pill--ghost"
            aria-label="Toggle theme"
            onClick={toggle}
          >
            {theme === "dark" ? "☀" : "☾"}
          </button>
          <Link to="/styleguide" className="pill pill--ghost">§ styleguide</Link>
        </nav>
      </div>

      <Outlet />
    </div>
  );
}
