import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { Me } from "@/types/api";

// Phone-frame layout. Header (greet + clock button) + body (<Outlet />)
// + fixed bottom tab bar. The chat page opts into a special
// `.phone--chat` modifier via pathname, so the whole column becomes a
// flex container and the composer can pin below the tabs.
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

  const firstName = data?.employee.name.split(" ")[0] ?? "…";
  const todayStr = data?.today
    ? new Date(data.today).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "short" })
    : "";
  const clockedIn = data?.employee.clocked_in_at;
  const clockedAt = clockedIn
    ? new Date(clockedIn).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : null;

  return (
    <main className={"phone" + (isChat ? " phone--chat" : "")}>
      <header className="phone__header">
        <div className="phone__greet">
          <span className="phone__hello">Hi, {firstName}</span>
          <span className="phone__date">{todayStr}</span>
        </div>
        <div className="phone__clock">
          <button
            type="button"
            className={"chip chip--lg " + (clockedIn ? "chip--moss" : "chip--ghost")}
            onClick={() => toggleShift.mutate()}
            disabled={toggleShift.isPending}
          >
            {clockedIn ? `● On shift · ${clockedAt}` : "Clock in"}
          </button>
        </div>
      </header>

      <Outlet />

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
