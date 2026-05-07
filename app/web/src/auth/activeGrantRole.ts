import type { Role } from "@/types/api";
import { useWorkspace } from "@/context/WorkspaceContext";
import { useAuth } from "./useAuth";
import type { AuthMe } from "./types";

export function activeWorkspaceGrantRole(
  user: AuthMe | null,
  workspaceId: string | null,
): string | null {
  const activeId = workspaceId ?? user?.current_workspace_id ?? null;
  if (!user || !activeId) return null;
  const workspace = user.available_workspaces.find(
    (w) => w.workspace.id === activeId || w.workspace_id === activeId,
  );
  return workspace?.grant_role ?? null;
}

export function appRoleForGrantRole(grantRole: string | null): Role {
  if (grantRole === "manager" || grantRole === "admin") return "manager";
  if (grantRole === "client") return "client";
  return "employee";
}

export function useActiveAppRole(): Role {
  const { user } = useAuth();
  const { workspaceId } = useWorkspace();
  return appRoleForGrantRole(activeWorkspaceGrantRole(user, workspaceId));
}
