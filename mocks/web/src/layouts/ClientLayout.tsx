import { useCallback, useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  CalendarDays,
  Clock3,
  FileText,
  Home,
  Menu,
  Receipt,
  UserCircle,
} from "lucide-react";
import SideNav, { type SideNavItem } from "@/components/SideNav";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { initialNavCollapsed, persistNavCollapsed } from "@/lib/preferences";
import type { Me, User } from "@/types/api";

// §22 — client portal layout. A read-mostly shell with a narrower
// nav: properties billed to the user, billable hours, quotes &
// invoices, and the shared `/me` profile screen. The agent sidebar
// is intentionally not mounted here — clients don't drive the
// crewday agent in v1; their actions are the accept/reject of
// quotes and the read of billing rollups.

const ICON_SIZE = 16;
const ICON_STROKE = 1.75;
const NAV_ICON = (Icon: typeof Home) => (
  <Icon size={ICON_SIZE} strokeWidth={ICON_STROKE} />
);

const NAV_ITEMS: SideNavItem[] = [
  { type: "section", label: "PORTFOLIO" },
  { type: "link", to: "/portfolio", label: "Properties", icon: NAV_ICON(Home) },
  { type: "link", to: "/scheduler", label: "Scheduler", icon: NAV_ICON(CalendarDays) },
  { type: "link", to: "/billable_hours", label: "Billable hours", icon: NAV_ICON(Clock3) },
  { type: "section", label: "BILLING" },
  { type: "link", to: "/quotes", label: "Quotes", icon: NAV_ICON(FileText) },
  { type: "link", to: "/invoices", label: "Invoices", icon: NAV_ICON(Receipt) },
  { type: "section", label: "ACCOUNT" },
  { type: "link", to: "/me", matchPrefix: "/me", label: "Me", icon: NAV_ICON(UserCircle) },
];

function hasDrawerItems(items: SideNavItem[]): boolean {
  return items.some((it) => it.type === "link" && !it.phoneHidden);
}

function initialsOf(name: string): string {
  return name.trim().split(/\s+/).slice(0, 2).map((p) => p.charAt(0).toUpperCase()).join("") || "·";
}

export default function ClientLayout() {
  const { data } = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  // §22 — the client's own User row is the source of truth for the
  // footer; `me.employee` is a legacy compat projection that only
  // the worker shell consumes. Hand the User in directly so the
  // sidebar shows the right person.
  const userQ = useQuery({
    queryKey: ["user", data?.user_id ?? ""] as const,
    queryFn: () => fetchJson<{ user: User }>("/api/v1/users/" + data!.user_id),
    enabled: !!data?.user_id,
    select: (r) => r.user,
  });
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
  const showMobileBar = hasDrawerItems(NAV_ITEMS);

  useEffect(() => { setNavOpen(false); }, [pathname]);
  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setNavOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen]);

  const displayName = userQ.data?.display_name ?? "Client";
  const initials = initialsOf(displayName);

  return (
    <div
      className="desk"
      data-nav-open={navOpen ? "true" : "false"}
      data-nav-collapsed={navCollapsed ? "true" : "false"}
      data-mobile-bar={showMobileBar ? "true" : "false"}
      data-agent-collapsed="true"
    >
      {showMobileBar && (
        <header className="desk__mobile-bar" aria-label="Mobile controls">
          <button
            type="button"
            className="desk__icon-btn"
            onClick={() => setNavOpen((v) => !v)}
            aria-label={navOpen ? "Close menu" : "Open menu"}
            aria-expanded={navOpen}
          >
            <Menu size={20} strokeWidth={2} aria-hidden="true" />
          </button>
          <div className="desk__brand">
            <span className="desk__logo" aria-hidden="true">◈</span>
            <span className="desk__wordmark">crew.day</span>
          </div>
        </header>
      )}

      {navOpen && (
        <div className="desk__scrim" onClick={() => setNavOpen(false)} role="presentation" aria-hidden="true" />
      )}

      <SideNav
        items={NAV_ITEMS}
        collapsed={navCollapsed}
        onToggleCollapsed={toggleNavCollapsed}
        footer={{
          initials,
          name: displayName,
          role: "Client",
        }}
      />

      <section className="desk__main">
        <Outlet />
      </section>
    </div>
  );
}
