import { useCallback, useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Archive,
  BedDouble,
  Boxes,
  Building2,
  CalendarCheck,
  CalendarClock,
  CalendarDays,
  ClipboardCheck,
  Euro,
  FileText,
  Files,
  Home,
  KeyRound,
  LayoutDashboard,
  ListChecks,
  Palmtree,
  ScrollText,
  Settings,
  Shield,
  ShieldCheck,
  Sunrise,
  UserCircle,
  Users,
  Wallet,
  Webhook,
  Wrench,
} from "lucide-react";
import AgentSidebar from "@/components/AgentSidebar";
import BottomTabs from "@/components/BottomTabs";
import SideNav, { type SideNavItem } from "@/components/SideNav";
import { ShellNavProvider } from "@/context/ShellNavContext";
import { useAuth } from "@/auth";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import {
  initialAgentCollapsed,
  initialNavCollapsed,
  persistNavCollapsed,
} from "@/lib/preferences";
import type { Me } from "@/types/api";
import type { ResolvedPermission } from "@/types/auth";

// ManagerLayout mounts AgentSidebar as a SIBLING of <Outlet />.
// React Router remounts only the outlet subtree on navigation, so the
// sidebar's chat log scroll position, composer draft, and cached log
// survive route changes. Do NOT wrap the outlet in the sidebar's
// parent (that would couple them), and do NOT put a `key` prop on the
// layout route (that would force a full remount).
//
// At phone widths the same shared <BottomTabs /> the worker shell uses
// hosts the worker-facing routes (Today/Schedule/Chat/Expenses/Me);
// the hamburger drawer holds the rest. MY WORK items are tagged
// `phoneHidden` so they don't duplicate the bottom bar.

const ICON_SIZE = 16;
const ICON_STROKE = 1.75;
const NAV_ICON = (Icon: typeof LayoutDashboard) => (
  <Icon size={ICON_SIZE} strokeWidth={ICON_STROKE} />
);

const BASE_NAV_ITEMS: SideNavItem[] = [
  { type: "section", label: "MY WORK", phoneHidden: true },
  { type: "link", to: "/today", matchPrefix: ["/today", "/task/"], label: "My Day", phoneHidden: true, icon: NAV_ICON(Sunrise) },
  { type: "link", to: "/schedule", label: "My Schedule", phoneHidden: true, icon: NAV_ICON(CalendarClock) },
  { type: "link", to: "/my/expenses", matchPrefix: "/my/expenses", label: "My Expenses", phoneHidden: true, icon: NAV_ICON(Euro) },
  { type: "link", to: "/me", matchPrefix: ["/me", "/history"], label: "My profile", phoneHidden: true, icon: NAV_ICON(UserCircle) },
  { type: "section", label: "OPERATE" },
  { type: "link", to: "/dashboard", label: "Dashboard", icon: NAV_ICON(LayoutDashboard) },
  { type: "link", to: "/properties", matchPrefix: "/propert", label: "Properties", icon: NAV_ICON(Home) },
  { type: "link", to: "/stays", label: "Stays", icon: NAV_ICON(BedDouble) },
  { type: "link", to: "/employees", matchPrefix: "/employee", label: "Employees", icon: NAV_ICON(Users) },
  { type: "link", to: "/templates", label: "Templates", icon: NAV_ICON(FileText) },
  { type: "link", to: "/schedules", label: "Schedules", icon: NAV_ICON(CalendarCheck) },
  { type: "link", to: "/scheduler", label: "Scheduler", icon: NAV_ICON(CalendarDays) },
  { type: "link", to: "/instructions", matchPrefix: "/instructions", label: "Instructions", icon: NAV_ICON(ListChecks) },
  { type: "link", to: "/inventory", label: "Inventory", icon: NAV_ICON(Boxes) },
  { type: "section", label: "ASSETS" },
  { type: "link", to: "/assets", matchPrefix: ["/assets", "/asset/"], label: "Assets", icon: NAV_ICON(Wrench) },
  { type: "link", to: "/asset_types", label: "Catalog", icon: NAV_ICON(Archive) },
  { type: "link", to: "/documents", label: "Documents", icon: NAV_ICON(Files) },
  { type: "section", label: "DECIDE" },
  { type: "link", to: "/approvals", label: "Approvals", icon: NAV_ICON(ClipboardCheck) },
  { type: "link", to: "/leaves", label: "Leaves", icon: NAV_ICON(Palmtree) },
  { type: "link", to: "/expenses", label: "Expenses", icon: NAV_ICON(Euro) },
  { type: "link", to: "/pay", label: "Pay", icon: NAV_ICON(Wallet) },
  { type: "section", label: "ADMIN" },
  { type: "link", to: "/organizations", matchPrefix: "/organization", label: "Organizations", icon: NAV_ICON(Building2) },
  { type: "link", to: "/permissions", label: "Permissions", icon: NAV_ICON(ShieldCheck) },
  { type: "link", to: "/audit", label: "Audit log", icon: NAV_ICON(ScrollText) },
  { type: "link", to: "/webhooks", label: "Webhooks", icon: NAV_ICON(Webhook) },
  { type: "link", to: "/tokens", label: "API tokens", icon: NAV_ICON(KeyRound) },
  { type: "link", to: "/settings", label: "Workspace settings", icon: NAV_ICON(Settings) },
];

// §14 "Administration link" — rendered only when the caller holds any
// active (scope_kind='deployment') role_grants row. LLM provider +
// capability config lives on /admin/llm (§11), not on the workspace.
const ADMINISTRATION_LINK: SideNavItem = {
  type: "link",
  to: "/admin",
  matchPrefix: "/admin",
  label: "Administration",
  icon: NAV_ICON(Shield),
};

const NAV_ACTIONS = new Map<string, string>([
  ["/dashboard", "employees.read"],
  ["/properties", "properties.read"],
  ["/stays", "stays.read"],
  ["/employees", "employees.read"],
  ["/templates", "tasks.create"],
  ["/schedules", "availability_overrides.view_others"],
  ["/scheduler", "scope.view"],
  ["/instructions", "instructions.edit"],
  ["/inventory", "scope.view"],
  ["/assets", "scope.view"],
  ["/asset_types", "scope.view"],
  ["/documents", "assets.manage_documents"],
  ["/approvals", "approvals.read"],
  ["/leaves", "leaves.view_others"],
  ["/expenses", "expenses.approve"],
  ["/pay", "payroll.view_other"],
  ["/organizations", "scope.view"],
  ["/permissions", "permissions.edit_rules"],
  ["/audit", "audit_log.view"],
  ["/webhooks", "scope.edit_settings"],
  ["/tokens", "api_tokens.manage"],
  ["/settings", "scope.edit_settings"],
]);

// Drawer-bar visibility: only render the hamburger + mobile top bar
// when there's at least one non-`phoneHidden` link to put inside the
// drawer. Today's RBAC is implicit (workers have no manager-only
// items), so the worker shell never shows it; once permissions filter
// NAV_ITEMS this rule lets workers gain a hamburger when they earn
// access to anything beyond MY WORK.
function hasDrawerItems(items: SideNavItem[]): boolean {
  return items.some((it) => it.type === "link" && !it.phoneHidden);
}

function actionKeysFor(items: SideNavItem[]): string[] {
  return Array.from(
    new Set(
      items.flatMap((item) => {
        if (item.type !== "link") return [];
        const actionKey = NAV_ACTIONS.get(item.to);
        return actionKey ? [actionKey] : [];
      }),
    ),
  );
}

function pruneEmptySections(items: SideNavItem[]): SideNavItem[] {
  const result: SideNavItem[] = [];
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i];
    if (!item) continue;
    if (item.type === "link") {
      result.push(item);
      continue;
    }
    const hasLinkBeforeNextSection = items
      .slice(i + 1)
      .some((next) => next.type === "section" ? false : true);
    if (hasLinkBeforeNextSection) result.push(item);
  }
  return result.filter((item, index, all) => {
    if (item.type === "link") return true;
    const next = all[index + 1];
    return next?.type === "link";
  });
}

function filterNavItems(items: SideNavItem[], allowedActions: Set<string> | null): SideNavItem[] {
  if (!allowedActions) {
    return items.filter((item) => item.type !== "link" || !NAV_ACTIONS.has(item.to) || item.phoneHidden);
  }
  const visible = items.filter((item) => {
    if (item.type !== "link") return true;
    const actionKey = NAV_ACTIONS.get(item.to);
    return !actionKey || allowedActions.has(actionKey);
  });
  return pruneEmptySections(visible);
}

function delayPermissionProbe(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const id = window.setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(id);
        reject(signal.reason);
      },
      { once: true },
    );
  });
}

async function resolveAllowedNavActions(
  actionKeys: string[],
  scopeId: string,
  signal: AbortSignal,
): Promise<Set<string>> {
  const allowed = new Set<string>();
  for (const [index, actionKey] of actionKeys.entries()) {
    if (signal.aborted) break;
    if (index > 0) await delayPermissionProbe(75, signal);
    const params = new URLSearchParams({
      action_key: actionKey,
      scope_kind: "workspace",
      scope_id: scopeId,
    });
    try {
      const permission = await fetchJson<ResolvedPermission>(
        `/api/v1/permissions/resolved/self?${params}`,
        { signal },
      );
      if (permission.effect === "allow") allowed.add(actionKey);
    } catch {
      if (signal.aborted) break;
      // A failed nav hint must not widen access. Direct routes still
      // enforce the same action through RequirePermission.
    }
  }
  return allowed;
}

export default function ManagerLayout() {
  const { user } = useAuth();
  const { data } = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
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
  const navItems: SideNavItem[] = data?.is_deployment_admin
    ? [...BASE_NAV_ITEMS, ADMINISTRATION_LINK]
    : BASE_NAV_ITEMS;
  const permissionScopeId = data?.current_workspace_id ?? null;
  const permissionUserId = data?.user_id ?? user?.user_id ?? null;
  const actionKeys = actionKeysFor(navItems);
  const permissionQ = useQuery({
    queryKey: permissionUserId && permissionScopeId
      ? [...qk.permissionResolvedPrefix(), "nav", permissionUserId, permissionScopeId, actionKeys.join("|")]
      : ["permission", "unresolved", "nav", "workspace"],
    enabled: Boolean(permissionUserId && permissionScopeId),
    queryFn: ({ signal }) => resolveAllowedNavActions(actionKeys, permissionScopeId ?? "", signal),
    retry: false,
  });
  const allowedActions = permissionQ.isPending ? null : permissionQ.data ?? new Set<string>();
  const filteredNavItems = filterNavItems(navItems, allowedActions);
  const hasDrawer = hasDrawerItems(filteredNavItems);
  const toggleNav = useCallback(() => setNavOpen((v) => !v), []);

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

  return (
    <ShellNavProvider hasDrawer={hasDrawer} isOpen={navOpen} toggle={toggleNav}>
      <div
        className={"desk" + (pathname === "/chat" ? " desk--chat" : "")}
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
          items={filteredNavItems}
          collapsed={navCollapsed}
          onToggleCollapsed={toggleNavCollapsed}
          footer={{
            initials: data?.employee.avatar_initials ?? "EB",
            name: data?.manager_name ?? "Élodie Bernard",
            role: "Manager",
          }}
        />

        <section className="desk__main">
          <Outlet />
        </section>

        {/* Sibling of <Outlet />. Do not nest. */}
        <AgentSidebar role="manager" />

        <BottomTabs />
      </div>
    </ShellNavProvider>
  );
}
