import { useCallback, useEffect, useState } from "react";
import { Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ActivitySquare,
  BookOpen,
  Building2,
  Gauge,
  ShieldAlert,
  MessageSquareMore,
  ScrollText,
  Settings,
  Sparkles,
  Users,
} from "lucide-react";
import AgentSidebar from "@/components/AgentSidebar";
import SideNav, { type SideNavItem } from "@/components/SideNav";
import { ShellNavProvider } from "@/context/ShellNavContext";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import {
  initialAgentCollapsed,
  initialNavCollapsed,
  persistNavCollapsed,
} from "@/lib/preferences";
import type { AdminMe, Me } from "@/types/api";

// AdminLayout — bare-host /admin/* shell (§14 "Admin shell").
//
// Mirrors ManagerLayout structurally (same .desk grid, same
// AgentSidebar sibling-of-Outlet pattern so chat state survives
// route changes) but swaps the nav for deployment-level entries
// and the agent for the admin-side agent (role="admin", §11).
//
// Access: the caller must pass GET /admin/api/v1/me AND have
// `is_deployment_admin: true` on the bare-host /auth/me payload.
// A non-admin caller is redirected to `RoleHome` (`/`) rather than
// shown a polite-card — the LoginPage filters `?next=/admin/...`
// for non-admins (cd-28s7), so the only paths that reach this
// guard are direct navigation / stale bookmarks. Sending those
// users home is clearer than dropping them on a denied surface
// they didn't ask to see. While the admin probe is still in
// flight we render a quiet "Checking access…" placeholder so
// child queries don't fire before authorisation is known.

const ICON_SIZE = 16;
const ICON_STROKE = 1.75;
const NAV_ICON = (Icon: typeof Gauge) => (
  <Icon size={ICON_SIZE} strokeWidth={ICON_STROKE} />
);

const NAV_ITEMS: SideNavItem[] = [
  { type: "section", label: "OPERATE" },
  { type: "link", to: "/admin/dashboard", label: "Dashboard", icon: NAV_ICON(Gauge) },
  { type: "link", to: "/admin/workspaces", matchPrefix: "/admin/workspaces", label: "Workspaces", icon: NAV_ICON(Building2) },
  { type: "link", to: "/admin/signups", label: "Signup signals", icon: NAV_ICON(ShieldAlert) },
  { type: "section", label: "USAGE" },
  { type: "link", to: "/admin/llm", label: "LLM & agents", icon: NAV_ICON(Sparkles) },
  { type: "link", to: "/admin/agent-docs", label: "Agent docs", icon: NAV_ICON(BookOpen) },
  { type: "link", to: "/admin/chat-gateway", label: "Chat gateway", icon: NAV_ICON(MessageSquareMore) },
  { type: "link", to: "/admin/usage", label: "Usage", icon: NAV_ICON(ActivitySquare) },
  { type: "section", label: "ADMIN" },
  { type: "link", to: "/admin/admins", label: "Admins", icon: NAV_ICON(Users) },
  { type: "link", to: "/admin/settings", label: "Settings", icon: NAV_ICON(Settings) },
  { type: "link", to: "/admin/audit", label: "Audit log", icon: NAV_ICON(ScrollText) },
];

export default function AdminLayout() {
  const navigate = useNavigate();
  const collapsed = initialAgentCollapsed();
  const { pathname } = useLocation();
  const [navOpen, setNavOpen] = useState(false);
  const [navCollapsed, setNavCollapsed] = useState<boolean>(() => initialNavCollapsed());
  const toggleNavCollapsed = useCallback(() => {
    setNavCollapsed((c) => {
      const next = !c;
      persistNavCollapsed(next ? "collapsed" : "open");
      return next;
    });
  }, []);

  const meQ = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });
  const adminMeQ = useQuery({
    queryKey: qk.adminMe(),
    queryFn: () => fetchJson<AdminMe>("/admin/api/v1/me"),
    retry: false,
  });

  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setNavOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen]);
  const toggleNav = useCallback(() => setNavOpen((v) => !v), []);

  const denied = adminMeQ.isError || meQ.data?.is_deployment_admin === false;
  const hasAccess = adminMeQ.isSuccess && meQ.data?.is_deployment_admin === true;

  if (denied) {
    // Bounce non-admins back to RoleHome (§14 "Admin shell"). The
    // root `<RoleHome>` routes by grant role — managers to /dashboard,
    // workers to /today, clients to /portfolio — so the user lands
    // on a surface that matches their identity rather than on the
    // admin chrome they have no business seeing. cd-28s7.
    return <Navigate to="/" replace />;
  }

  if (!hasAccess) {
    // Still resolving identity — render a minimal chrome without
    // mounting the outlet, so child pages don't fire admin queries
    // before we know whether the caller is authorised. Avoids a burst
    // of 404s in the console for visitors who aren't admins.
    return (
      <div className="desk desk--admin">
        <section className="desk__main" aria-busy="true">
          <div className="empty-state empty-state--quiet">Checking access…</div>
        </section>
      </div>
    );
  }

  return (
    <ShellNavProvider hasDrawer={true} isOpen={navOpen} toggle={toggleNav}>
      <div
        className="desk desk--admin"
        data-agent-collapsed={collapsed ? "true" : "false"}
        data-nav-collapsed={navCollapsed ? "true" : "false"}
        data-nav-open={navOpen ? "true" : "false"}
      >
        {navOpen && (
          <div
            className="desk__scrim"
            onClick={() => setNavOpen(false)}
            role="presentation"
            aria-hidden="true"
          />
        )}

        <SideNav
          items={NAV_ITEMS}
          collapsed={navCollapsed}
          onToggleCollapsed={toggleNavCollapsed}
          footer={{
            initials: (adminMeQ.data?.display_name ?? "Admin")
              .split(" ")
              .map((w) => w[0])
              .join("")
              .slice(0, 2)
              .toUpperCase(),
            name: adminMeQ.data?.display_name ?? "Deployment admin",
            role: adminMeQ.data?.is_owner ? "Deployment owner" : "Deployment admin",
          }}
          action={
            <button
              type="button"
              className="btn btn--ghost admin-backlink"
              onClick={() => navigate("/")}
            >
              ← Back to workspaces
            </button>
          }
        />

        <section className="desk__main">
          <Outlet />
        </section>

        {/* Sibling of <Outlet />. Do not nest. */}
        <AgentSidebar role="admin" />
      </div>
    </ShellNavProvider>
  );
}
