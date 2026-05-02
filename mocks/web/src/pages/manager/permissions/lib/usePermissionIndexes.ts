import { useQuery } from "@tanstack/react-query";
import { fetchJson, ApiError } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { User } from "@/types/api";

/**
 * One row of `GET /api/v1/me/workspaces` (spec §12 "Auth"). Slim
 * subset — the page only needs id + name for the workspace selector.
 * Mirrors `app.api.v1.auth.me.WorkspaceSwitcherEntry`.
 */
export interface WorkspaceSwitcherEntry {
  workspace_id: string;
  slug: string;
  name: string;
}

export function useWorkspaces() {
  return useQuery({
    queryKey: qk.meWorkspaces(),
    queryFn: () =>
      fetchJson<WorkspaceSwitcherEntry[]>("/api/v1/me/workspaces"),
  });
}

/**
 * Workspace user index keyed by user id. v1 has no `/api/v1/users`
 * listing yet — the index degrades to an empty map so the page renders
 * without a user roster. Drop the 404 catch once cd-8y5aa lands the
 * listing endpoint.
 */
export function useUsersIndex() {
  return useQuery({
    queryKey: qk.users(),
    queryFn: async () => {
      try {
        return await fetchJson<User[]>("/api/v1/users");
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          return [] as User[];
        }
        throw err;
      }
    },
    select: (rows) =>
      Object.fromEntries(rows.map((u) => [u.id, u])) as Record<string, User>,
  });
}
