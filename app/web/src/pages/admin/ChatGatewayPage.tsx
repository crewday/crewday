import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Check, Copy, MessageSquare } from "lucide-react";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading, StatCard } from "@/components/common";
import { ApiError, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type {
  AdminChatOverrideRow,
  AdminChatProvider,
  AdminChatProviderTemplate,
} from "@/types/api";

const STATUS_TONE: Record<AdminChatProvider["status"], "moss" | "rust" | "ghost"> = {
  connected: "moss",
  error: "rust",
  not_configured: "ghost",
};

const TEMPLATE_TONE: Record<
  AdminChatProviderTemplate["status"],
  "moss" | "sand" | "rust" | "ghost"
> = {
  approved: "moss",
  pending: "sand",
  rejected: "rust",
  paused: "ghost",
};

interface AdminChatTestInboundRequest {
  channel_kind: AdminChatProvider["channel_kind"];
  external_contact: string;
  body_md: string;
  language_hint: string | null;
}

interface AdminChatTestInboundResult {
  correlation_id: string;
  message_id: string;
  binding_id: string;
  channel_id: string;
  dispatch_status: "enqueued" | "skipped" | "failed";
  agent_invoked: boolean;
  latency_ms: number;
  failure_reason: string | null;
}

async function copyToClipboard(value: string): Promise<void> {
  if (!navigator.clipboard) throw new Error("clipboard_unavailable");
  await navigator.clipboard.writeText(value);
}

function CopyField({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="chat-gateway-panel__webhook">
      <span className="muted">{label}</span>
      <code className="inline-code chat-gateway-panel__url">{value || "—"}</code>
      <button
        type="button"
        className="btn btn--ghost btn--sm"
        disabled={!value}
        onClick={() => {
          void copyToClipboard(value).then(() => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1600);
          });
        }}
      >
        {copied ? <Check size={14} strokeWidth={2} /> : <Copy size={14} strokeWidth={2} />}
        {copied ? " Copied" : " Copy"}
      </button>
    </div>
  );
}

function ProviderPanel({
  p,
  templates,
}: {
  p: AdminChatProvider;
  templates: AdminChatProviderTemplate[];
}) {
  return (
    <div className="panel">
      <header className="panel__head">
        <div className="agent-usage__heading">
          <h2>{p.label}</h2>
          <span className="muted">
            <MessageSquare size={14} strokeWidth={2} aria-hidden="true" /> {p.phone_display}
          </span>
        </div>
        <Chip tone={STATUS_TONE[p.status]} size="sm">{p.status.replace("_", " ")}</Chip>
      </header>

      {p.status === "not_configured" ? (
        <p className="muted">
          Not configured. Paste the provider credentials below to turn this channel on for every
          workspace riding the deployment default.
        </p>
      ) : (
        <p className="muted">
          Deployment-default. Every workspace routes through this Meta account unless it opts into
          its own provider override.
        </p>
      )}

      <h3 className="section-title section-title--sm">Credentials</h3>
      <table className="table chat-gateway-panel__table">
        <thead>
          <tr>
            <th>Field</th><th>Value</th><th>Last edit</th><th></th>
          </tr>
        </thead>
        <tbody>
          {p.credentials.map((c) => (
            <tr key={c.field}>
              <td>{c.label}<div className="table__sub mono">{c.field}</div></td>
              <td className="mono">{c.display_stub}</td>
              <td className="mono muted">
                {c.updated_at ? new Date(c.updated_at).toLocaleString() : "—"}
                <div className="table__sub">{c.updated_by ?? ""}</div>
              </td>
              <td>
                <button type="button" className="btn btn--ghost btn--sm" disabled>
                  {c.set ? "Rotate" : "Set"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {templates.length > 0 && (
        <>
          <h3 className="section-title section-title--sm">Registered templates</h3>
          <table className="table chat-gateway-panel__table">
            <thead>
              <tr>
                <th>Name</th><th>Purpose</th><th>Status</th><th>Last sync</th><th></th>
              </tr>
            </thead>
            <tbody>
              {templates.map((t) => (
                <tr key={t.name}>
                  <td className="mono">{t.name}</td>
                  <td className="muted">{t.purpose}</td>
                  <td>
                    <Chip tone={TEMPLATE_TONE[t.status]} size="sm">{t.status}</Chip>
                    {t.rejection_reason ? (
                      <div className="table__sub">{t.rejection_reason}</div>
                    ) : null}
                  </td>
                  <td className="mono muted">
                    {t.last_sync_at ? new Date(t.last_sync_at).toLocaleString() : "—"}
                  </td>
                  <td>
                    <button type="button" className="btn btn--ghost btn--sm" disabled>Resync</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <div className="chat-gateway-panel__footer">
        <CopyField label="Webhook URL" value={p.webhook_url} />
        <CopyField label="Verify token" value={p.verify_token_stub} />
        <p className="muted chat-gateway-panel__hint">
          Paste these into the Meta Business Manager → WhatsApp → Configuration panel when
          provisioning or rotating. Both values are workspace-agnostic — every riding workspace
          shares them.
        </p>
      </div>
    </div>
  );
}

function TestInboundPanel({ provider }: { provider: AdminChatProvider | undefined }) {
  const [externalContact, setExternalContact] = useState("+15551234567");
  const [bodyMd, setBodyMd] = useState("Can you show me today's tasks?");
  const [languageHint, setLanguageHint] = useState("en");

  const testInbound = useMutation({
    mutationFn: (payload: AdminChatTestInboundRequest) =>
      fetchJson<AdminChatTestInboundResult>("/admin/api/v1/chat/test-inbound", {
        method: "POST",
        body: payload,
      }),
  });

  const result = testInbound.data;
  const error = testInbound.error;
  const errorMessage =
    error instanceof ApiError ? error.message : error instanceof Error ? error.message : null;

  return (
    <div className="panel chat-gateway-test">
      <header className="panel__head">
        <h2>Test inbound</h2>
        <Chip tone="sky" size="sm">dispatcher</Chip>
      </header>
      <p className="muted">
        Send a fake provider webhook through the dispatcher and confirm the agent handoff without
        waiting for Meta to deliver a live message.
      </p>
      <div className="chat-gateway-test__form">
        <label className="field">
          <span>From</span>
          <input
            className="input mono"
            value={externalContact}
            onChange={(e) => setExternalContact(e.target.value)}
          />
        </label>
        <label className="field">
          <span>Language</span>
          <input
            className="input"
            value={languageHint}
            onChange={(e) => setLanguageHint(e.target.value)}
          />
        </label>
        <label className="field chat-gateway-test__message">
          <span>Inbound message</span>
          <textarea
            className="textarea"
            value={bodyMd}
            onChange={(e) => setBodyMd(e.target.value)}
            rows={3}
          />
        </label>
        <button
          type="button"
          className="btn btn--moss"
          disabled={!provider || testInbound.isPending || !externalContact.trim() || !bodyMd.trim()}
          onClick={() => {
            if (!provider) return;
            testInbound.mutate({
              channel_kind: provider.channel_kind,
              external_contact: externalContact.trim(),
              body_md: bodyMd.trim(),
              language_hint: languageHint.trim() || null,
            });
          }}
        >
          {testInbound.isPending ? "Sending…" : "Send test inbound"}
        </button>
      </div>

      {result ? (
        <dl className="settings-kv chat-gateway-test__result" aria-label="Dispatcher result">
          <dt>Correlation ID</dt>
          <dd className="mono">{result.correlation_id}</dd>
          <dt>Agent invoked</dt>
          <dd>{result.agent_invoked ? "yes" : "no"}</dd>
          <dt>Dispatch status</dt>
          <dd><Chip tone={result.dispatch_status === "failed" ? "rust" : "moss"} size="sm">{result.dispatch_status}</Chip></dd>
          <dt>Latency</dt>
          <dd className="mono">{result.latency_ms} ms</dd>
          <dt>Message ID</dt>
          <dd className="mono">{result.message_id}</dd>
          <dt>Binding ID</dt>
          <dd className="mono">{result.binding_id}</dd>
          <dt>Channel ID</dt>
          <dd className="mono">{result.channel_id}</dd>
          {result.failure_reason ? (
            <>
              <dt>Failure</dt>
              <dd>{result.failure_reason}</dd>
            </>
          ) : null}
        </dl>
      ) : null}

      {errorMessage ? (
        <p className="form-error" role="alert">{errorMessage}</p>
      ) : null}
    </div>
  );
}

export default function AdminChatGatewayPage() {
  const providersQ = useQuery({
    queryKey: qk.adminChatProviders(),
    queryFn: () => fetchJson<AdminChatProvider[]>("/admin/api/v1/chat/providers"),
  });
  const templatesQ = useQuery({
    queryKey: qk.adminChatTemplates(),
    queryFn: () => fetchJson<AdminChatProviderTemplate[]>("/admin/api/v1/chat/templates"),
  });
  const overridesQ = useQuery({
    queryKey: qk.adminChatOverrides(),
    queryFn: () => fetchJson<AdminChatOverrideRow[]>("/admin/api/v1/chat/overrides"),
  });

  const sub =
    "Deployment-default chat providers (§23). Every workspace rides this account unless it opts into its own. Workers link their phones on /me; nothing here configures a specific user.";

  if (providersQ.isPending || templatesQ.isPending || overridesQ.isPending) {
    return <DeskPage title="Chat gateway" sub={sub}><Loading /></DeskPage>;
  }
  if (!providersQ.data || !templatesQ.data || !overridesQ.data) {
    return <DeskPage title="Chat gateway" sub={sub}>Failed to load.</DeskPage>;
  }

  const providers = providersQ.data;
  const templates = templatesQ.data;
  const overrides = overridesQ.data;
  const wa = providers.find((p) => p.channel_kind === "offapp_whatsapp");

  return (
    <DeskPage title="Chat gateway" sub={sub}>
      <section className="grid grid--stats">
        <StatCard
          label="Active providers"
          value={providers.filter((p) => p.status === "connected").length}
          sub={`of ${providers.length} configured kinds`}
        />
        <StatCard
          label="24h outbound"
          value={wa ? wa.outbound_24h : 0}
          sub={wa ? `cap ${wa.daily_outbound_cap} / day` : "—"}
        />
        <StatCard
          label="Delivery errors"
          value={wa ? `${wa.delivery_error_rate_pct.toFixed(1)}%` : "—"}
          sub="24h, across all bindings"
        />
        <StatCard
          label="Workspaces on override"
          value={overrides.length}
          sub={overrides.length === 1 ? "bringing their own Meta account" : "bringing their own Meta accounts"}
        />
      </section>

      <div className="panel">
        <header className="panel__head">
          <h2>Agent authority</h2>
          <Chip tone="sky" size="sm">invariant</Chip>
        </header>
        <p className="muted">
          The chat gateway authenticates the <em>transport</em>; the <em>agent</em> authenticates
          as the delegating user. Every turn runs under a delegated token minted from that user's
          session, so the agent can only do what the user could do themselves via the CLI — no
          more, no less. Provider credentials on this page therefore carry no app-level authority;
          rotating them never grants or revokes a user's permissions.
        </p>
      </div>

      <TestInboundPanel provider={wa} />

      {providers.map((p) => (
        <ProviderPanel
          key={p.channel_kind}
          p={p}
          templates={p.channel_kind === "offapp_whatsapp" ? templates : p.templates}
        />
      ))}

      <div className="panel">
        <header className="panel__head">
          <h2>Per-workspace outbound caps</h2>
        </header>
        <p className="muted">
          Soft sub-cap per workspace on the shared number, so a noisy workspace cannot starve the
          others. Workspaces on a custom provider inherit Meta's own caps for their number instead.
        </p>
        <dl className="settings-kv">
          <dt>Default per-workspace sub-cap</dt>
          <dd className="mono">{wa ? wa.per_workspace_soft_cap : "—"} / day</dd>
          <dt>Deployment-wide ceiling (Meta tier)</dt>
          <dd className="mono">{wa ? wa.daily_outbound_cap : "—"} / day</dd>
        </dl>
      </div>

      <div className="panel">
        <header className="panel__head">
          <h2>Workspaces on a custom provider</h2>
        </header>
        {overrides.length === 0 ? (
          <p className="muted">No workspace overrides the deployment default today.</p>
        ) : (
          <>
            <p className="muted">
              These workspaces bring their own Meta Cloud account. The deployment does not hold
              their credentials — this list is audit-only.
            </p>
            <table className="table">
              <thead>
                <tr>
                  <th>Workspace</th><th>Channel</th><th>Number</th>
                  <th>Status</th><th>Since</th><th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {overrides.map((o) => (
                  <tr key={o.workspace_id + o.channel_kind}>
                    <td>{o.workspace_name}<div className="table__sub mono">{o.workspace_id}</div></td>
                    <td className="mono">{o.channel_kind}</td>
                    <td className="mono">{o.phone_display}</td>
                    <td><Chip tone={STATUS_TONE[o.status]} size="sm">{o.status.replace("_", " ")}</Chip></td>
                    <td className="mono muted">{o.created_at}</td>
                    <td className="muted">{o.reason ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </DeskPage>
  );
}
