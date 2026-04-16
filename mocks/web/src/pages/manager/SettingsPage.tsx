import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type { HouseholdSettings } from "@/types/api";

export default function SettingsPage() {
  const q = useQuery({
    queryKey: qk.settings(),
    queryFn: () => fetchJson<HouseholdSettings>("/api/v1/settings"),
  });

  const sub = "Household-wide configuration. Sensitive actions live on the host CLI only — see the danger zone.";

  if (q.isPending) return <DeskPage title="Settings" sub={sub}><Loading /></DeskPage>;
  if (!q.data) return <DeskPage title="Settings" sub={sub}>Failed to load.</DeskPage>;

  const s = q.data;

  return (
    <DeskPage title="Settings" sub={sub}>
      <section className="grid grid--split">
        <div className="panel">
          <header className="panel__head"><h2>Household</h2></header>
          <dl className="settings-kv">
            <dt>Name</dt><dd>{s.name}</dd>
            <dt>Timezone</dt><dd className="mono">{s.timezone}</dd>
            <dt>Currency</dt><dd className="mono">{s.currency}</dd>
            <dt>Week starts on</dt><dd>{s.week_start}</dd>
            <dt>Pay frequency</dt><dd>{s.pay_frequency}</dd>
          </dl>
        </div>

        <div className="panel">
          <header className="panel__head"><h2>Defaults</h2></header>
          <dl className="settings-kv">
            <dt>Photo evidence default</dt>
            <dd><Chip tone="sky" size="sm">{s.default_photo_evidence}</Chip></dd>
            <dt>Geofence radius</dt><dd className="mono">{s.geofence_radius_m} m</dd>
            <dt>LLM call retention</dt>
            <dd className="mono">{s.retention_days.llm_calls} days</dd>
            <dt>Audit retention</dt>
            <dd className="mono">{s.retention_days.audit} days</dd>
            <dt>Task-photo retention</dt>
            <dd className="mono">{s.retention_days.task_photos} days</dd>
          </dl>
        </div>
      </section>

      <div className="panel">
        <header className="panel__head"><h2>Agent approvals</h2></header>
        <p className="muted">Actions that require your manual approval before an agent can execute them.</p>

        <h3 className="section-title section-title--sm">Always gated (cannot be disabled)</h3>
        <ul className="settings-list">
          {s.approvals.always_gated.map((a) => (
            <li key={a}><code className="inline-code">{a}</code></li>
          ))}
        </ul>

        <h3 className="section-title section-title--sm">Configurable</h3>
        <ul className="settings-list">
          {s.approvals.configurable.map((a) => (
            <li key={a}>
              <code className="inline-code">{a}</code>{" "}
              <Chip tone="moss" size="sm">gated</Chip>
            </li>
          ))}
        </ul>
      </div>

      <div className="panel panel--danger">
        <header className="panel__head"><h2>Danger zone</h2></header>
        <p className="muted">Host-CLI-only. No HTTP surface, no agent path (§15).</p>
        <ul className="danger-list">
          {s.danger_zone.map((d) => (
            <li key={d}>{d}</li>
          ))}
        </ul>
      </div>
    </DeskPage>
  );
}
