import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ShieldAlert } from "lucide-react";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import {
  Chip,
  EmptyState,
  FilterChipGroup,
  Loading,
  Panel,
} from "@/components/common";
import type {
  SignupAuditEntry,
  SignupAuditKind,
  SignupsListResponse,
} from "@/types/api";

// Deployment-scoped admin surface — signup abuse signals (§15
// "Self-serve abuse mitigations"). Mounts at /admin/signups and reads
// /admin/api/v1/signups; workspace managers do not get a separate
// signup-abuse feed.
//
// The backing router is a placeholder (cd-g1ay) returning
// {data: [], has_more: false}. cd-ovt4 wires the real query
// against the audit log. The UI renders the filter chrome and
// empty-state copy today so the page anchors the nav entry and
// reviewers see exactly what the live feature will look like.

const KIND_TONE: Record<SignupAuditKind, "moss" | "sand" | "rust" | "sky"> = {
  burst_rate: "rust",
  distinct_emails_one_ip: "rust",
  repeat_email: "sand",
  quota_near_breach: "sky",
};

const KIND_LABEL: Record<SignupAuditKind, string> = {
  burst_rate: "Burst-rate trip",
  distinct_emails_one_ip: "Many emails, one IP",
  repeat_email: "Repeat email",
  quota_near_breach: "Quota near breach",
};

type KindFilter = SignupAuditKind;

const KIND_OPTIONS: { value: KindFilter; label: string; tone: "rust" | "sand" | "sky" }[] = [
  { value: "burst_rate", label: KIND_LABEL.burst_rate, tone: "rust" },
  { value: "distinct_emails_one_ip", label: KIND_LABEL.distinct_emails_one_ip, tone: "rust" },
  { value: "repeat_email", label: KIND_LABEL.repeat_email, tone: "sand" },
  { value: "quota_near_breach", label: KIND_LABEL.quota_near_breach, tone: "sky" },
];

function hms(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function dayMon(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
  });
}

function shortHash(value: string | null): string {
  if (!value) return "—";
  // Hashes arrive as opaque strings from §15's redaction layer;
  // render the first eight chars for visual distinctness without
  // implying more precision than we have.
  return value.length > 10 ? `${value.slice(0, 8)}…` : value;
}

function kindLabel(kind: SignupAuditEntry["kind"]): string {
  return (KIND_LABEL as Record<string, string>)[kind] ?? kind;
}

function kindTone(
  kind: SignupAuditEntry["kind"],
): "moss" | "sand" | "rust" | "sky" | "ghost" {
  return (KIND_TONE as Record<string, "moss" | "sand" | "rust" | "sky">)[kind] ?? "ghost";
}

export default function SignupsPage() {
  const [filter, setFilter] = useState<KindFilter | "">("");

  const q = useQuery({
    queryKey: qk.adminSignups(),
    queryFn: () =>
      fetchJson<SignupsListResponse>("/admin/api/v1/signups"),
  });

  const sub =
    "Abuse signals from the signup surface — burst-rate trips, one IP across many emails, repeat provisioning, and quota near-breach events. Live rows appear here as the deployment catches them; §15 sets the thresholds.";

  const rows: SignupAuditEntry[] = useMemo(
    () => q.data?.data ?? [],
    [q.data],
  );
  const filtered = useMemo(
    () => (filter === "" ? rows : rows.filter((r) => r.kind === filter)),
    [rows, filter],
  );

  if (q.isPending) {
    return (
      <DeskPage title="Signups" sub={sub}>
        <Loading />
      </DeskPage>
    );
  }
  if (!q.data) {
    return (
      <DeskPage title="Signups" sub={sub}>
        Failed to load.
      </DeskPage>
    );
  }

  const countBy = (kind: KindFilter): number =>
    rows.filter((r) => r.kind === kind).length;

  return (
    <DeskPage title="Signups" sub={sub}>
      <Panel
        title="Abuse signals"
        right={
          <span className="muted">
            {rows.length === 0
              ? "No signals yet"
              : `${rows.length} event${rows.length === 1 ? "" : "s"}`}
          </span>
        }
      >
        <FilterChipGroup<KindFilter>
          value={filter}
          onChange={setFilter}
          allLabel={`All · ${rows.length}`}
          options={KIND_OPTIONS.map((opt) => ({
            value: opt.value,
            label: `${opt.label} · ${countBy(opt.value)}`,
            tone: opt.tone,
          }))}
        />


        {rows.length === 0 ? (
          <EmptyState
            variant="quiet"
            glyph={<ShieldAlert size={22} strokeWidth={1.5} aria-hidden="true" />}
          >
            <p>
              Nothing to triage. When a signup burst or repeat-email
              pattern trips, it lands here with the redacted IP and
              email hashes your audit log already carries.
            </p>
          </EmptyState>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>When</th>
                <th>Signal</th>
                <th>IP hash</th>
                <th>Email hash</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => (
                <tr key={row.event_id}>
                  <td className="mono">
                    {hms(row.occurred_at)}
                    <div className="table__sub">{dayMon(row.occurred_at)}</div>
                  </td>
                  <td>
                    <Chip tone={kindTone(row.kind)} size="sm">
                      {kindLabel(row.kind)}
                    </Chip>
                  </td>
                  <td className="mono muted">{shortHash(row.ip_hash)}</td>
                  <td className="mono muted">{shortHash(row.email_hash)}</td>
                  <td className="table__sub">
                    {Object.keys(row.detail).length === 0
                      ? "—"
                      : Object.entries(row.detail)
                          .map(([k, v]) => `${k}: ${String(v)}`)
                          .join(" · ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </DeskPage>
  );
}
