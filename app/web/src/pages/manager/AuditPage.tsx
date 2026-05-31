import { useInfiniteQuery } from "@tanstack/react-query";
import { FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { fetchJson, openApiDownload } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Loading } from "@/components/common";
import { AdminAuditRow } from "@/pages/admin/AdminAuditRow";
import type { AuditEntry, AuditListResponse } from "@/types/api";

const FILTER_KEYS = ["actor", "action", "entity", "since", "until"] as const;
type FilterKey = (typeof FILTER_KEYS)[number];

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

function auditExportPath(filters: Record<FilterKey, string>): string {
  const params = new URLSearchParams();
  for (const key of FILTER_KEYS) {
    if (filters[key]) params.set(key, filters[key]);
  }
  params.set("limit", "500");
  const qs = params.toString();
  return `/api/v1/audit/tail?${qs}`;
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
  const overflow = [
    {
      label: "Export JSONL",
      onSelect: () => openApiDownload(auditExportPath(filters)),
    },
  ];

  if (q.isPending) return <DeskPage title="Audit log" sub={sub} overflow={overflow}><Loading /></DeskPage>;
  if (!q.data) return <DeskPage title="Audit log" sub={sub} overflow={overflow}>Failed to load.</DeskPage>;

  const entries = q.data.pages.flatMap((page) => page.data);
  const countBy = (kind: AuditEntry["actor_kind"]): number =>
    entries.filter((e) => e.actor_kind === kind).length;
  const countByGrant = (role: NonNullable<AuditEntry["actor_grant_role"]>): number =>
    entries.filter((e) => e.actor_grant_role === role).length;
  const governanceCount = entries.filter((e) => e.actor_was_owner_member).length;
  const filtersActive = FILTER_KEYS.some((key) => filters[key]);

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
        <form key={filterSig} className="audit-filters" role="search" onSubmit={applyFilters}>
          <label className="field">
            <span>Actor</span>
            <input
              name="actor"
              aria-label="Actor"
              placeholder="usr_1"
              defaultValue={filters.actor}
            />
          </label>
          <label className="field field--grow">
            <span>Action</span>
            <input
              name="action"
              aria-label="Action"
              placeholder="asset.updated"
              defaultValue={filters.action}
            />
          </label>
          <label className="field field--grow">
            <span>Entity</span>
            <input
              name="entity"
              aria-label="Entity"
              placeholder="asset:asset_1"
              defaultValue={filters.entity}
            />
          </label>
          <label className="field">
            <span>Since</span>
            <input
              name="since"
              aria-label="Since"
              placeholder="2026-04-01T00:00:00Z"
              defaultValue={filters.since}
            />
          </label>
          <label className="field">
            <span>Until</span>
            <input
              name="until"
              aria-label="Until"
              placeholder="2026-04-30T23:59:59Z"
              defaultValue={filters.until}
            />
          </label>
          <button className="btn btn--ghost" type="submit">Filter</button>
          {filtersActive ? (
            <button
              type="button"
              className="link audit-filters__clear"
              onClick={() => setSearchParams(new URLSearchParams())}
            >
              Clear filters
            </button>
          ) : null}
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
        <table className="table table--roomy admin-audit-table">
          <thead>
            <tr>
              <th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Via</th><th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e, idx) => (
              <AdminAuditRow
                key={e.correlation_id ? `${e.correlation_id}:${idx}` : idx}
                row={e}
                showVia
              />
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
