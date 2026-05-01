import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type {
  AdminSignupSignal,
  AdminSignupSignalKind,
  AdminSignupsResponse,
} from "@/types/api";

const KIND_LABEL: Record<AdminSignupSignalKind, string> = {
  burst_rate: "Burst rate",
  distinct_emails_one_ip: "One IP, many emails",
  repeat_email: "Repeat email",
  quota_near_breach: "Quota near breach",
};

const KIND_TONE: Record<
  AdminSignupSignalKind,
  "moss" | "sky" | "sand" | "ghost" | "rust"
> = {
  burst_rate: "rust",
  distinct_emails_one_ip: "sand",
  repeat_email: "sky",
  quota_near_breach: "moss",
};

function detailCount(signal: AdminSignupSignal): number | null {
  const detail = signal.detail;
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return null;
  const count = detail.count;
  return typeof count === "number" ? count : null;
}

function shortHash(value: string | null): string {
  return value ? value.slice(0, 12) : "-";
}

export default function AdminSignupsPage() {
  const q = useQuery({
    queryKey: qk.adminSignups(),
    queryFn: () => fetchJson<AdminSignupsResponse>("/admin/api/v1/signups"),
  });
  const sub =
    "Deployment-scope signup-abuse signals from the audit log. Hashes are peppered; raw email and IP never appear here.";

  if (q.isPending) return <DeskPage title="Signup signals" sub={sub}><Loading /></DeskPage>;
  if (!q.data) return <DeskPage title="Signup signals" sub={sub}>Failed to load.</DeskPage>;

  return (
    <DeskPage title="Signup signals" sub={sub}>
      <div className="panel">
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>When</th>
              <th>Signal</th>
              <th>Email hash</th>
              <th>IP hash</th>
              <th>Count</th>
            </tr>
          </thead>
          <tbody>
            {q.data.data.map((signal) => {
              const count = detailCount(signal);
              return (
                <tr key={signal.event_id}>
                  <td className="mono">{new Date(signal.occurred_at).toLocaleString()}</td>
                  <td>
                    <Chip tone={KIND_TONE[signal.kind]} size="sm">
                      {KIND_LABEL[signal.kind]}
                    </Chip>
                  </td>
                  <td className="mono">{shortHash(signal.email_hash)}</td>
                  <td className="mono">{shortHash(signal.ip_hash)}</td>
                  <td className="muted">{count ?? "-"}</td>
                </tr>
              );
            })}
            {q.data.data.length === 0 ? (
              <tr>
                <td colSpan={5} className="muted">
                  No suspicious signup activity has been recorded.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
