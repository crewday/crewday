import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import type { ListEnvelope } from "@/lib/listResponse";
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

export type UserIndexRow = Pick<User, "id" | "display_name" | "email">;

async function fetchUsersIndexRows(): Promise<UserIndexRow[]> {
  const rows: UserIndexRow[] = [];
  let cursor: string | null = null;
  for (;;) {
    const path: string =
      cursor === null
        ? "/api/v1/users?limit=500"
        : `/api/v1/users?limit=500&cursor=${encodeURIComponent(cursor)}`;
    const page: ListEnvelope<UserIndexRow> = await fetchJson(path);
    rows.push(...page.data);
    if (!page.has_more || page.next_cursor === null) return rows;
    cursor = page.next_cursor;
  }
}

/** Workspace user index keyed by user id. */
export function useUsersIndex() {
  return useQuery({
    queryKey: qk.users(),
    queryFn: fetchUsersIndexRows,
    select: (page) =>
      Object.fromEntries(page.map((u) => [u.id, u])) as Record<string, UserIndexRow>,
  });
}
