import { Link } from "react-router-dom";
import { ArrowRight, Building2, Check } from "lucide-react";
import { workspaceSlug } from "@/auth/roleLanding";
import type { AvailableWorkspace } from "@/types/auth";

const ROLE_LABELS: Record<string, string> = {
  manager: "Manager",
  worker: "Worker",
  client: "Client",
  guest: "Guest",
  admin: "Admin",
};

interface WorkspacePickListProps {
  workspaces: AvailableWorkspace[];
  activeWorkspaceSlug?: string | null;
  className?: string;
  label: string;
  onPick: (workspace: AvailableWorkspace) => void;
  setFirstAction?: (node: HTMLElement | null) => void;
  toForWorkspace?: (workspace: AvailableWorkspace) => string;
}

export default function WorkspacePickList({
  workspaces,
  activeWorkspaceSlug = null,
  className,
  label,
  onPick,
  setFirstAction,
  toForWorkspace,
}: WorkspacePickListProps) {
  return (
    <ul className={["workspace-pick-list", className].filter(Boolean).join(" ")} aria-label={label}>
      {workspaces.map((workspace, idx) => {
        const slug = workspaceSlug(workspace);
        const role = labelForRole(workspace.grant_role);
        const selected = slug === activeWorkspaceSlug;
        const content = (
          <>
            <span className="workspace-pick-list__icon" aria-hidden="true">
              <Building2 size={18} strokeWidth={1.6} />
            </span>
            <span className="workspace-pick-list__body">
              <span className="workspace-pick-list__name">{workspace.workspace.name}</span>
              <span className="workspace-pick-list__meta">
                <span className="workspace-pick-list__slug">{slug}</span>
                {role ? <span className="workspace-pick-list__role">{role}</span> : null}
              </span>
            </span>
            <span className="workspace-pick-list__state" aria-hidden="true">
              {selected ? <Check size={16} strokeWidth={1.8} /> : <ArrowRight size={16} strokeWidth={1.6} />}
            </span>
          </>
        );
        const ariaLabel = [
          `Switch to ${workspace.workspace.name}`,
          `slug ${slug}`,
          role ? `role ${role}` : null,
          selected ? "current workspace" : null,
        ].filter(Boolean).join(", ");

        return (
          <li key={workspace.workspace_id ?? slug} className="workspace-pick-list__item">
            {toForWorkspace ? (
              <Link
                ref={idx === 0 ? setFirstAction : undefined}
                to={toForWorkspace(workspace)}
                className={"workspace-pick-list__row" + (selected ? " workspace-pick-list__row--active" : "")}
                aria-label={ariaLabel}
                aria-current={selected ? "true" : undefined}
                onClick={() => onPick(workspace)}
              >
                {content}
              </Link>
            ) : (
              <button
                ref={idx === 0 ? setFirstAction : undefined}
                type="button"
                className={"workspace-pick-list__row" + (selected ? " workspace-pick-list__row--active" : "")}
                aria-label={ariaLabel}
                aria-current={selected ? "true" : undefined}
                onClick={() => onPick(workspace)}
              >
                {content}
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function labelForRole(role: string | null): string | null {
  if (!role) return null;
  return ROLE_LABELS[role] ?? role;
}
