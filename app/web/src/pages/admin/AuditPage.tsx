import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk, type AdminAuditFilter } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, FilterChipGroup, Loading } from "@/components/common";
import { displayAuditRow } from "@/pages/admin/auditRows";
import type { AdminAuditListResponse, AuditEntry } from "@/types/api";

const ACTOR_TONE: Record<AuditEntry["actor_kind"], "moss" | "sky" | "ghost"> = {
  user: "moss",
  agent: "sky",
  system: "ghost",
};

const ACTOR_KIND_OPTIONS: {
  value: NonNullable<AdminAuditFilter["actor_kind"]>;
  label: string;
  tone: "moss" | "sky" | "ghost";
}[] = [
  { value: "user", label: "User", tone: "moss" },
  { value: "agent", label: "Agent", tone: "sky" },
  { value: "system", label: "System", tone: "ghost" },
];

// Build a query string from the server-honoured slice of the filter
// (`actor_kind` is applied client-side; the backend does not yet
// expose it as a query param — see `app.api.admin.audit.list_audit`).
function buildAuditQuery(filter: AdminAuditFilter): string {
  const params = new URLSearchParams();
  if (filter.actor_id) params.set("actor_id", filter.actor_id);
  if (filter.action) params.set("action", filter.action);
  if (filter.since) params.set("since", filter.since);
  if (filter.until) params.set("until", filter.until);
  const qs = params.toString();
  return qs ? "?" + qs : "";
}

// `<input type="date">` returns `YYYY-MM-DD`; the backend wants
// ISO-8601. We pin since/until to the start/end of the local day so
// the filter feels intuitive ("everything from the 18th") rather than
// a midnight-UTC literal that drops the morning of the picker date.
//
// Critical: emit the boundary in **UTC** (`.toISOString()`) after
// constructing it in local time. The backend's `_parse_iso` treats
// naive ISO strings as UTC, so a bare `"2026-05-03T00:00:00"` would
// shift the boundary by the user's TZ offset (e.g. drop the first
// two hours of May 3rd for a Paris user, or include early May 4th
// for the until edge). Building the `Date` from local components
// then serialising via `.toISOString()` keeps the boundary anchored
// to the picker's local day on every consumer.
function dayBoundaryIso(value: string, edge: "start" | "end"): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return value;
  const [, y, m, d] = match;
  const year = Number(y);
  const month = Number(m) - 1;
  const day = Number(d);
  const dt =
    edge === "start"
      ? new Date(year, month, day, 0, 0, 0, 0)
      : new Date(year, month, day, 23, 59, 59, 999);
  return dt.toISOString();
}

function readFilter(params: URLSearchParams): AdminAuditFilter {
  const filter: AdminAuditFilter = {};
  const kind = params.get("actor_kind");
  if (kind === "user" || kind === "agent" || kind === "system") {
    filter.actor_kind = kind;
  }
  const actorId = params.get("actor_id");
  if (actorId) filter.actor_id = actorId;
  const action = params.get("action");
  if (action) filter.action = action;
  const since = params.get("since");
  if (since) filter.since = since;
  const until = params.get("until");
  if (until) filter.until = until;
  return filter;
}

function setParam(
  current: URLSearchParams,
  key: string,
  value: string,
): URLSearchParams {
  const next = new URLSearchParams(current);
  if (value) next.set(key, value);
  else next.delete(key);
  return next;
}

function isFilterActive(filter: AdminAuditFilter): boolean {
  return Boolean(
    filter.actor_kind ||
      filter.actor_id ||
      filter.action ||
      filter.since ||
      filter.until,
  );
}

export default function AdminAuditPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filter = useMemo(() => readFilter(searchParams), [searchParams]);

  // The backend filters server-side on actor_id/action/since/until;
  // actor_kind is left out of the wire query and applied below over
  // the returned page so two actor_kind tabs share one fetch. The
  // cache key tracks only the wire-shaped slice for the same reason.
  const wireFilter: AdminAuditFilter = {
    actor_id: filter.actor_id,
    action: filter.action,
    since: filter.since ? dayBoundaryIso(filter.since, "start") : undefined,
    until: filter.until ? dayBoundaryIso(filter.until, "end") : undefined,
  };

  const q = useQuery({
    queryKey: qk.adminAudit(wireFilter),
    queryFn: () =>
      fetchJson<AdminAuditListResponse>(
        "/admin/api/v1/audit" + buildAuditQuery(wireFilter),
      ),
  });

  const sub =
    "Deployment-scope audit — scope_kind='deployment' rows only. Each action ties back to its admin actor via actor_id.";

  const setFilterParam = (key: string, value: string) => {
    setSearchParams(setParam(searchParams, key, value));
  };
  const clearFilters = () => setSearchParams(new URLSearchParams());

  const filterActive = isFilterActive(filter);

  // Action autocomplete pulls suggestions from the loaded page so the
  // datalist always reflects what the server actually has. A future
  // ``GET /admin/api/v1/audit/actions`` endpoint can replace this
  // with a deployment-wide distinct list; for v1 the in-page set is
  // good enough — the action vocabulary is small (≤ a few dozen).
  const actionSuggestions = useMemo(() => {
    if (!q.data) return [];
    return Array.from(new Set(q.data.data.map((r) => r.action))).sort();
  }, [q.data]);

  if (q.isPending) return <DeskPage title="Audit log" sub={sub}><Loading /></DeskPage>;
  if (!q.data) return <DeskPage title="Audit log" sub={sub}>Failed to load.</DeskPage>;

  const allRows = q.data.data.map(displayAuditRow);
  const rows = filter.actor_kind
    ? allRows.filter((r) => r.actor_kind === filter.actor_kind)
    : allRows;

  return (
    <DeskPage title="Audit log" sub={sub}>
      <div className="panel">
        <FilterChipGroup<NonNullable<AdminAuditFilter["actor_kind"]>>
          value={filter.actor_kind ?? ""}
          onChange={(value) => setFilterParam("actor_kind", value)}
          allLabel="All actors"
          options={ACTOR_KIND_OPTIONS}
        />

        <div className="audit-filters">
          <label className="field field--grow">
            <span>Action</span>
            <input
              list="audit-action-suggestions"
              value={filter.action ?? ""}
              placeholder="deployment.budget.updated"
              onChange={(e) => setFilterParam("action", e.target.value.trim())}
            />
            <datalist id="audit-action-suggestions">
              {actionSuggestions.map((a) => (
                <option key={a} value={a} />
              ))}
            </datalist>
          </label>
          <label className="field">
            <span>Actor ID</span>
            <input
              value={filter.actor_id ?? ""}
              placeholder="u-elodie"
              onChange={(e) => setFilterParam("actor_id", e.target.value.trim())}
            />
          </label>
          <label className="field">
            <span>From</span>
            <input
              type="date"
              value={filter.since ?? ""}
              onChange={(e) => setFilterParam("since", e.target.value)}
            />
          </label>
          <label className="field">
            <span>Until</span>
            <input
              type="date"
              value={filter.until ?? ""}
              onChange={(e) => setFilterParam("until", e.target.value)}
            />
          </label>
          {filterActive && (
            <button
              type="button"
              className="link audit-filters__clear"
              onClick={clearFilters}
            >
              Clear filters
            </button>
          )}
        </div>

        {rows.length === 0 ? (
          <p className="muted audit-filters__empty">
            {filterActive
              ? "No audit rows match this filter."
              : "No deployment-scope audit rows yet."}
          </p>
        ) : (
          <table className="table table--roomy">
            <thead>
              <tr>
                <th>When</th>
                <th>Actor</th>
                <th>Action</th>
                <th>Target</th>
                <th>Via</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, idx) => (
                <tr key={idx}>
                  <td className="mono">{new Date(row.at).toLocaleString()}</td>
                  <td>
                    <Chip tone={ACTOR_TONE[row.actor_kind]} size="sm">{row.actor_kind}</Chip>{" "}
                    {row.actor}
                    {row.actor_was_owner_member ? <span className="muted"> · owner</span> : null}
                  </td>
                  <td>
                    <code className="inline-code">{row.action}</code>
                    {row.actor_action_key && (
                      <div className="table__sub">via {row.actor_action_key}</div>
                    )}
                  </td>
                  <td className="mono">{row.target}</td>
                  <td className="muted">{row.via}</td>
                  <td className="muted">{row.reason ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </DeskPage>
  );
}
