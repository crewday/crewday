import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Check, Plus } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useWorkspace } from "@/context/WorkspaceContext";
import { workspaceRelativePathname, workspaceRoute, workspaceSlugFromRoutePath } from "@/lib/workspaceRoutes";
import type { AvailableWorkspace, Me } from "@/types/api";

// §02, workspace switcher rendered under the brand row in SideNav.
// Lists every workspace the current user has a grant on (from /me's
// `available_workspaces`); selecting one keeps `/w/<slug>/...` routes
// canonical while still updating the cookie fallback.

const ROLE_LABEL: Record<string, string> = {
  manager: "Manager",
  worker: "Worker",
  client: "Client",
  guest: "Guest",
};

export default function WorkspaceSwitcher() {
  // code-health: ignore[ccn] Workspace switcher keeps tenant visibility, outside-click handling, and selected-workspace menu state together.
  const { workspaceId, setWorkspaceId } = useWorkspace();
  const navigate = useNavigate();
  const location = useLocation();
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!meQ.data) return null;
  const available = meQ.data.available_workspaces ?? [];
  if (available.length === 0) return null;

  const activeId = workspaceId ?? meQ.data.current_workspace_id;
  const active = available.find((a) => a.workspace.id === activeId) ?? available[0];
  if (!active) return null;

  const pick = (next: AvailableWorkspace) => {
    setOpen(false);
    if (next.workspace.id === activeId) return;
    if (workspaceSlugFromRoutePath(location.pathname)) {
      navigate(workspaceRoute(next.workspace.id, workspaceRelativePathname(location.pathname), {
        search: location.search,
        hash: location.hash,
      }));
      return;
    }
    setWorkspaceId(next.workspace.id);
  };

  const createWorkspace = () => {
    setOpen(false);
    navigate("/workspaces/new");
  };

  return (
    <div className="ws-switcher" ref={ref}>
      <button
        type="button"
        className="ws-switcher__trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => { setOpen((v) => !v); }}
      >
        <span className="ws-switcher__name truncate">{active.workspace.name}</span>
        {active.grant_role && (
          <span className="ws-switcher__role">{ROLE_LABEL[active.grant_role] ?? active.grant_role}</span>
        )}
        <ChevronDown size={14} aria-hidden="true" className="ws-switcher__chev" />
      </button>
      {open && (
        <ul className="ws-switcher__menu" role="menu" aria-label="Workspace menu">
          {available.map((w) => {
            const selected = w.workspace.id === activeId;
            return (
              <li key={w.workspace.id} role="none">
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={selected}
                  className={"ws-switcher__opt" + (selected ? " ws-switcher__opt--active" : "")}
                  onClick={() => pick(w)}
                >
                  <span className="ws-switcher__opt-name truncate">{w.workspace.name}</span>
                  {w.grant_role && (
                    <span className="ws-switcher__opt-role">{ROLE_LABEL[w.grant_role] ?? w.grant_role}</span>
                  )}
                  {selected && <Check size={14} aria-hidden="true" className="ws-switcher__opt-check" />}
                </button>
              </li>
            );
          })}
          <li className="ws-switcher__separator" aria-hidden="true" />
          <li role="none">
            <button
              type="button"
              role="menuitem"
              className="ws-switcher__opt ws-switcher__opt--create"
              onClick={createWorkspace}
            >
              <Plus size={14} aria-hidden="true" className="ws-switcher__opt-check" />
              <span className="ws-switcher__opt-name truncate">New workspace</span>
            </button>
          </li>
        </ul>
      )}
    </div>
  );
}
