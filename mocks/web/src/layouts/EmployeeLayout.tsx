import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import SideNav, { type SideNavItem } from "@/components/SideNav";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { Me } from "@/types/api";

function roleLabel(role: string): string {
  return role.charAt(0).toUpperCase() + role.slice(1).replace(/_/g, " ");
}

// Phone-frame layout. Body (<Outlet />) + a bottom dock that hosts the
// clock-in toggle, plus a fixed bottom tab bar. The chat page opts
// into a special `.phone--chat` modifier via pathname, so the whole
// column becomes a flex container and the composer can pin below the
// tabs (the dock is hidden on chat to give the composer the room).
//
// At tablet / desktop widths (>=720px) the phone becomes a two-column
// grid and the shared <SideNav /> takes over from the bottom tab bar,
// so the chrome matches the manager sidebar exactly. The clock-in
// button rides in the sidebar's `action` slot at that width; the
// phone-mode dock + bottom tab bar stay in the DOM and are hidden by
// CSS.

const NAV_ITEMS: SideNavItem[] = [
  { type: "link", to: "/today", label: "Today" },
  { type: "link", to: "/week", label: "Week" },
  { type: "link", to: "/chat", label: "Chat" },
  { type: "link", to: "/my/expenses", label: "Expenses" },
  { type: "link", to: "/me", matchPrefix: "/me", label: "Me" },
];

function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p.charAt(0).toUpperCase()).join("") || "·";
}

export default function EmployeeLayout() {
  const { pathname } = useLocation();
  const isChat = pathname === "/chat";
  const { data } = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const qc = useQueryClient();

  const toggleShift = useMutation({
    mutationFn: () => fetchJson<Me>("/api/v1/shifts/toggle", { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.me() });
      qc.invalidateQueries({ queryKey: qk.today() });
    },
  });

  const clockedIn = data?.employee.clocked_in_at;
  const clockedAt = clockedIn
    ? new Date(clockedIn).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : null;

  const footerName = data?.employee.name ?? "…";
  const footerRole = data?.employee.roles[0] ? roleLabel(data.employee.roles[0]) : "Employee";
  const footerInitials = data?.employee.avatar_initials
    ?? (data ? initialsOf(data.employee.name) : "·");

  const clockButton = (
    <button
      type="button"
      className={"clock-toggle " + (clockedIn ? "clock-toggle--on" : "clock-toggle--off")}
      onClick={() => toggleShift.mutate()}
      disabled={toggleShift.isPending}
    >
      {clockedIn ? `● On shift · ${clockedAt}` : "Clock in"}
    </button>
  );

  return (
    <main className={"phone" + (isChat ? " phone--chat" : "")}>
      <SideNav
        items={NAV_ITEMS}
        action={clockButton}
        footer={{
          initials: footerInitials,
          name: footerName,
          role: footerRole,
        }}
      />

      <div className="phone__body">
        <Outlet />
      </div>

      {!isChat && <div className="phone__dock">{clockButton}</div>}

      <nav className="phone__tabs" aria-label="Bottom navigation">
        <Tab to="/today" glyph="◎" label="Today" />
        <Tab to="/week" glyph="⋮⋮" label="Week" />
        <Tab to="/chat" glyph="✦" label="Chat" />
        <Tab to="/my/expenses" glyph="€" label="Expenses" />
        <MeTab />
      </nav>
    </main>
  );
}

function Tab({ to, glyph, label }: { to: string; glyph: string; label: string }) {
  return (
    <NavLink to={to} className={({ isActive }) => "tab" + (isActive ? " tab--active" : "")}>
      <span className="tab__glyph" aria-hidden="true">{glyph}</span>
      <span>{label}</span>
    </NavLink>
  );
}

function MeTab() {
  const { pathname } = useLocation();
  const active = pathname === "/me" || pathname === "/shifts" || pathname === "/history";
  return (
    <NavLink to="/me" className={"tab" + (active ? " tab--active" : "")}>
      <span className="tab__glyph" aria-hidden="true">◌</span>
      <span>Me</span>
    </NavLink>
  );
}
