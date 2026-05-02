import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { User, Workspace } from "@/types/api";

export function useWorkspaces() {
  return useQuery({
    queryKey: qk.workspaces(),
    queryFn: () => fetchJson<Workspace[]>("/api/v1/workspaces"),
  });
}

export function useUsersIndex() {
  return useQuery({
    queryKey: qk.users(),
    queryFn: () => fetchJson<User[]>("/api/v1/users"),
    select: (rows) =>
      Object.fromEntries(rows.map((u) => [u.id, u])) as Record<string, User>,
  });
}
