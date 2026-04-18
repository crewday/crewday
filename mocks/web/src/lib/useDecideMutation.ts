import { useMutation, useQueryClient, type QueryKey } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";

// Approve / reject / reimburse mutations across manager desks share the
// same optimistic flow: cancel in-flight queries, snapshot the cache,
// apply a local edit, POST, restore on error, invalidate on settle.
// Only the local edit differs (remove, update-status, split-list). The
// caller passes that as `applyOptimistic`; the rest is factored here so
// changes to invalidation or error handling ripple to all desks.

export function useDecideMutation<TQueryData, TDecision extends string>({
  queryKey,
  endpoint,
  applyOptimistic,
  alsoInvalidate = [qk.dashboard()],
}: {
  queryKey: QueryKey;
  endpoint: (id: string, decision: TDecision) => string;
  applyOptimistic: (prev: TQueryData, id: string, decision: TDecision) => TQueryData;
  alsoInvalidate?: QueryKey[];
}) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: TDecision }) =>
      fetchJson(endpoint(id, decision), { method: "POST" }),
    onMutate: async ({ id, decision }) => {
      await qc.cancelQueries({ queryKey });
      const prev = qc.getQueryData<TQueryData>(queryKey);
      if (prev !== undefined) {
        qc.setQueryData<TQueryData>(queryKey, applyOptimistic(prev, id, decision));
      }
      return { prev };
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev !== undefined) qc.setQueryData(queryKey, ctx.prev);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey });
      for (const k of alsoInvalidate) qc.invalidateQueries({ queryKey: k });
    },
  });
}
