import { useInfiniteQuery } from "@tanstack/react-query";
import { FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { ACTOR_KIND_TONE, GRANT_ROLE_TONE } from "@/lib/tones";
import type { AuditEntry, AuditListResponse } from "@/types/api";

const FILTER_KEYS = ["actor", "action", "entity", "since", "until"] as const;
type FilterKey = (typeof FILTER_KEYS)[number];

function hms(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}
function dayMon(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

function filtersFromSearch(searchParams: URLSearchParams): Record<FilterKey, string> {
  return {
    actor: searchParams.get("actor") ?? "",
    action: searchParams.get("action") ?? "",
    entity: searchParams.get("entity") ?? "",
    since: searchParams.get("since") ?? "",
    until: searchParams.get("until") ?? "",
  };
}

function auditPath(filters: Record<FilterKey, string>, cursor: string | null): string {
  const params = new URLSearchParams();
  for (const key of FILTER_KEYS) {
    if (filters[key]) params.set(key, filters[key]);
  }
  params.set("limit", "50");
  if (cursor) params.set("cursor", cursor);
  const qs = params.toString();
  return qs ? `/api/v1/audit?${qs}` : "/api/v1/audit";
}

export default function AuditPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = filtersFromSearch(searchParams);
  const filterSig = JSON.stringify(filters);
  const q = useInfiniteQuery({
    queryKey: [...qk.audit(), filterSig],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      fetchJson<AuditListResponse>(auditPath(filters, pageParam)),
    getNextPageParam: (lastPage) => lastPage.next_cursor,
  });

  const sub = "Append-only. Every mutation by a user (on the manager/worker/client surface), an agent, or the system. Actions taken by a member of the owners permission group carry a governance badge.";
  const overflow = [{ label: "Export JSONL", onSelect: () => undefined }];

  if (q.isPending) return <DeskPage title="Audit log" sub={sub} overflow={overflow}><Loading /></DeskPage>;
  if (!q.data) return <DeskPage title="Audit log" sub={sub} overflow={overflow}>Failed to load.</DeskPage>;

  const entries = q.data.pages.flatMap((page) => page.data);
  const countBy = (kind: AuditEntry["actor_kind"]): number =>
    entries.filter((e) => e.actor_kind === kind).length;
  const countByGrant = (role: NonNullable<AuditEntry["actor_grant_role"]>): number =>
    entries.filter((e) => e.actor_grant_role === role).length;
  const governanceCount = entries.filter((e) => e.actor_was_owner_member).length;

  function applyFilters(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const next = new URLSearchParams();
    for (const key of FILTER_KEYS) {
      const value = String(form.get(key) ?? "").trim();
      if (value) next.set(key, value);
    }
    setSearchParams(next);
  }

  return (
    <DeskPage title="Audit log" sub={sub} overflow={overflow}>
      <section className="panel">
        <form key={filterSig} className="desk-filters" role="search" onSubmit={applyFilters}>
          <input name="actor" aria-label="Actor" placeholder="Actor" defaultValue={filters.actor} />
          <input name="action" aria-label="Action" placeholder="Action" defaultValue={filters.action} />
          <input name="entity" aria-label="Entity" placeholder="Entity" defaultValue={filters.entity} />
          <input name="since" aria-label="Since" placeholder="Since" defaultValue={filters.since} />
          <input name="until" aria-label="Until" placeholder="Until" defaultValue={filters.until} />
          <button className="btn btn--ghost" type="submit">Filter</button>
        </form>
        <div className="desk-filters">
          <span className="chip chip--ghost chip--sm chip--active">All</span>
          <span className="chip chip--ghost chip--sm">User · {countBy("user")}</span>
          <span className="chip chip--ghost chip--sm">Agent · {countBy("agent")}</span>
          <span className="chip chip--ghost chip--sm">System · {countBy("system")}</span>
          <span className="chip chip--ghost chip--sm">Manager · {countByGrant("manager")}</span>
          <span className="chip chip--ghost chip--sm">Worker · {countByGrant("worker")}</span>
          <span className="chip chip--ghost chip--sm">Client · {countByGrant("client")}</span>
          <span className="chip chip--ghost chip--sm">Governance · {governanceCount}</span>
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Via</th><th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e, idx) => (
              <tr key={e.correlation_id ? `${e.correlation_id}:${idx}` : idx}>
                <td className="mono">
                  {hms(e.at)}
                  <div className="table__sub">{dayMon(e.at)}</div>
                </td>
                <td>
                  <Chip tone={ACTOR_KIND_TONE[e.actor_kind]} size="sm">{e.actor_kind}</Chip>{" "}
                  {e.actor_grant_role ? (
                    <>
                      <Chip tone={GRANT_ROLE_TONE[e.actor_grant_role]} size="sm">{e.actor_grant_role}</Chip>{" "}
                    </>
                  ) : null}
                  {e.actor_was_owner_member ? (
                    <>
                      <Chip tone="moss" size="sm">owners</Chip>{" "}
                    </>
                  ) : null}
                  {e.actor}
                </td>
                <td className="mono">{e.action}</td>
                <td className="mono muted">{e.target}</td>
                <td><Chip tone="ghost" size="sm">{e.via}</Chip></td>
                <td className="table__sub">{e.reason ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {q.hasNextPage ? (
          <div className="desk-filters">
            <button
              className="btn btn--ghost"
              type="button"
              disabled={q.isFetchingNextPage}
              onClick={() => void q.fetchNextPage()}
            >
              {q.isFetchingNextPage ? "Loading..." : "Load more"}
            </button>
          </div>
        ) : null}
      </section>
    </DeskPage>
  );
}
