import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Checkbox, Chip, Loading } from "@/components/common";
import type {
  AdminDeploymentSetting,
  AdminMe,
  AdminSignupSettings,
} from "@/types/api";

export default function AdminSettingsPage() {
  const qc = useQueryClient();
  const me = useQuery({
    queryKey: qk.adminMe(),
    queryFn: () => fetchJson<AdminMe>("/admin/api/v1/me"),
  });
  const q = useQuery({
    queryKey: qk.adminSettings(),
    queryFn: () => fetchJson<AdminDeploymentSetting[]>("/admin/api/v1/settings"),
  });
  const update = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) =>
      fetchJson<AdminDeploymentSetting>(`/admin/api/v1/settings/${key}`, {
        method: "PUT",
        body: { value },
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.adminSettings() }),
  });

  const signupQ = useQuery({
    queryKey: qk.adminSignup(),
    queryFn: () => fetchJson<AdminSignupSettings>("/admin/api/v1/signup/settings"),
  });
  const signupUpdate = useMutation({
    mutationFn: (patch: Partial<AdminSignupSettings>) =>
      fetchJson<AdminSignupSettings>("/admin/api/v1/signup/settings", {
        method: "PUT",
        body: patch,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.adminSignup() }),
  });

  const [drafts, setDrafts] = useState<Record<string, unknown>>({});
  const [signupDraft, setSignupDraft] = useState<Partial<AdminSignupSettings>>({});

  const sub =
    "Deployment-scope settings: self-serve signup policy, the capability registry read-out, and the raw key/value store. Root-only keys require deployment owner rights.";

  if (q.isPending || me.isPending || signupQ.isPending) {
    return <DeskPage title="Settings" sub={sub}><Loading /></DeskPage>;
  }
  if (!q.data || !signupQ.data) return <DeskPage title="Settings" sub={sub}>Failed to load.</DeskPage>;

  const isOwner = me.data?.is_owner ?? false;
  const rows = q.data;
  const setDraft = (k: string, v: unknown) =>
    setDrafts((d) => ({ ...d, [k]: v }));
  const getDraft = (r: AdminDeploymentSetting): unknown =>
    drafts[r.key] !== undefined ? drafts[r.key] : r.value;

  const dirtyKeys = rows
    .filter((r) => {
      const locked = r.root_only && !isOwner;
      return !locked && drafts[r.key] !== undefined && drafts[r.key] !== r.value;
    })
    .map((r) => r.key);
  const dirtyCount = dirtyKeys.length;

  const saveAll = async () => {
    if (dirtyCount === 0) return;
    const byKey = new Map(rows.map((r) => [r.key, r] as const));
    await Promise.all(
      dirtyKeys.map((k) => {
        const r = byKey.get(k);
        return r ? update.mutateAsync({ key: k, value: getDraft(r) }) : null;
      }),
    );
    setDrafts({});
  };

  const resetAll = () => setDrafts({});

  const s = signupQ.data;
  const sv = <K extends keyof AdminSignupSettings>(k: K): AdminSignupSettings[K] =>
    (k in signupDraft ? (signupDraft as any)[k] : s[k]) as AdminSignupSettings[K];
  const setSignup = <K extends keyof AdminSignupSettings>(k: K, v: AdminSignupSettings[K]) =>
    setSignupDraft((d) => ({ ...d, [k]: v }));
  const signupDirty = Object.keys(signupDraft).length > 0;

  return (
    <DeskPage title="Settings" sub={sub}>
      {/* §03 self-serve signup — moved here from /admin/signup so all
          deployment-policy knobs live on one page. The SaaS deployment
          at crew.day keeps enabled=true; self-hosts flip it off. */}
      <div className="panel">
        <header className="panel__head">
          <h2>Visitor signup</h2>
          <Chip tone={s.enabled ? "moss" : "ghost"} size="sm">
            {s.enabled ? "enabled" : "closed"}
          </Chip>
        </header>
        <p className="muted">
          Self-serve signup controls for this deployment (§03). Flip enabled, tighten
          throttles, point at the disposable-domain blocklist.
        </p>
        <div className="form-grid form-grid--two">
          <div className="form-row">
            <span className="form-label">Enabled</span>
            <Checkbox
              checked={!!sv("enabled")}
              onChange={(e) => setSignup("enabled", e.target.checked)}
              label={
                sv("enabled")
                  ? "Anyone can create a workspace via /signup."
                  : "/signup/start returns 404; /signup renders a 'closed' page."
              }
            />
          </div>
          <label className="form-row">
            <span className="form-label">Disposable domains</span>
            <span className="mono">{s.disposable_domains_count.toLocaleString()} blocked</span>
          </label>
          <label className="form-row">
            <span className="form-label">Per-IP /hour</span>
            <input
              type="number"
              min="1"
              max="50"
              className="input input--inline"
              value={sv("throttle_per_ip_hour")}
              onChange={(e) => setSignup("throttle_per_ip_hour", Number(e.target.value))}
            />
          </label>
          <label className="form-row">
            <span className="form-label">Per-email lifetime cap</span>
            <input
              type="number"
              min="1"
              max="10"
              className="input input--inline"
              value={sv("throttle_per_email_lifetime")}
              onChange={(e) => setSignup("throttle_per_email_lifetime", Number(e.target.value))}
            />
          </label>
          <label className="form-row">
            <span className="form-label">Pre-verified upload cap (MB)</span>
            <input
              type="number"
              min="5"
              max="500"
              className="input input--inline"
              value={sv("pre_verified_upload_mb_cap")}
              onChange={(e) => setSignup("pre_verified_upload_mb_cap", Number(e.target.value))}
            />
          </label>
          <label className="form-row">
            <span className="form-label">Pre-verified LLM cap (% of free tier)</span>
            <input
              type="number"
              min="1"
              max="100"
              className="input input--inline"
              value={sv("pre_verified_llm_percent_cap")}
              onChange={(e) => setSignup("pre_verified_llm_percent_cap", Number(e.target.value))}
            />
          </label>
        </div>
        <footer className="panel__foot">
          <span className="muted">
            Last edited {new Date(s.updated_at).toLocaleString()} by {s.updated_by}.
          </span>
          <div className="inline-actions">
            <button
              type="button"
              className="btn btn--ghost"
              disabled={!signupDirty || signupUpdate.isPending}
              onClick={() => setSignupDraft({})}
            >
              Discard
            </button>
            <button
              type="button"
              className="btn btn--moss"
              disabled={!signupDirty || signupUpdate.isPending}
              onClick={() => signupUpdate.mutate(signupDraft)}
            >
              Save
            </button>
          </div>
        </footer>
      </div>

      <div className="panel">
        <header className="panel__head"><h2>Deployment settings</h2></header>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Key</th>
              <th>Value</th>
              <th>Last edit</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const locked = r.root_only && !isOwner;
              return (
                <tr key={r.key}>
                  <td>
                    <code className="inline-code">{r.key}</code>
                    {r.root_only && (
                      <>
                        {" "}
                        <Chip tone="rust" size="sm">owners-only</Chip>
                      </>
                    )}
                    <div className="table__sub muted">{r.description}</div>
                  </td>
                  <td>
                    {r.kind === "bool" ? (
                      <Checkbox
                        checked={!!getDraft(r)}
                        disabled={locked}
                        onChange={(e) => setDraft(r.key, e.target.checked)}
                      />
                    ) : r.kind === "int" ? (
                      <input
                        type="number"
                        className="input input--inline"
                        value={String(getDraft(r) as number)}
                        disabled={locked}
                        onChange={(e) => setDraft(r.key, Number(e.target.value))}
                      />
                    ) : (
                      <input
                        type="text"
                        className="input input--inline"
                        value={String(getDraft(r) as string)}
                        disabled={locked}
                        onChange={(e) => setDraft(r.key, e.target.value)}
                      />
                    )}
                  </td>
                  <td className="mono muted">
                    {new Date(r.updated_at).toLocaleString()}
                    <div className="table__sub">{r.updated_by}</div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <footer className="panel__foot">
          <span className="muted">
            {dirtyCount === 0
              ? "No pending changes."
              : `${dirtyCount} pending change${dirtyCount === 1 ? "" : "s"}.`}
          </span>
          <div className="inline-actions">
            <button
              type="button"
              className="btn btn--ghost"
              disabled={dirtyCount === 0 || update.isPending}
              onClick={resetAll}
            >
              Discard
            </button>
            <button
              type="button"
              className="btn btn--moss"
              disabled={dirtyCount === 0 || update.isPending}
              onClick={saveAll}
            >
              {update.isPending ? "Saving…" : "Save changes"}
            </button>
          </div>
        </footer>
      </div>
    </DeskPage>
  );
}
