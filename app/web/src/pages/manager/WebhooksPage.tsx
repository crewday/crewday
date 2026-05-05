import { type FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useCloseOnEscape } from "@/lib/useCloseOnEscape";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type { Webhook, WebhookDelivery } from "@/types/api";

function fmt(iso: string): string {
  // code-health: ignore[nloc] Tiny date formatter is over-counted by lizard after TSX parsing.
  return new Date(iso).toLocaleString("en-GB", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

function isOkStatus(status: string | number | null): boolean {
  if (typeof status === "number") return status >= 200 && status < 300;
  return status === "succeeded" || status === "success";
}

function statusText(status: string | number | null): string {
  return status === null ? "pending" : String(status);
}

function splitEvents(raw: string): string[] {
  return raw.split(/[,\s]+/).map((event) => event.trim()).filter(Boolean);
}

function deliveryResponseText(delivery: WebhookDelivery): string | number {
  return delivery.response_status ?? delivery.last_status_code ?? delivery.error ?? delivery.last_error ?? "pending";
}

export default function WebhooksPage() {
  const qc = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState("");
  const [active, setActive] = useState(true);
  const [secret, setSecret] = useState<{ label: string; value: string } | null>(null);
  const [selected, setSelected] = useState<Webhook | null>(null);

  const q = useQuery({
    queryKey: qk.webhooks(),
    queryFn: () => fetchJson<Webhook[]>("/api/v1/webhooks"),
  });

  const deliveriesQ = useQuery({
    queryKey: selected ? qk.webhookDeliveries(selected.id) : qk.webhookDeliveries("_"),
    queryFn: () => fetchJson<WebhookDelivery[]>(`/api/v1/webhooks/${selected?.id}/deliveries`),
    enabled: selected !== null,
  });

  const create = useMutation({
    mutationFn: (body: { name: string; url: string; events: string[]; active: boolean }) =>
      fetchJson<Webhook>("/api/v1/webhooks", { method: "POST", body }),
    onSuccess: (created) => {
      setSecret(created.secret ? { label: "Webhook signing secret", value: created.secret } : null);
      setCreateOpen(false);
      setName("");
      setUrl("");
      setEvents("");
      setActive(true);
      qc.invalidateQueries({ queryKey: qk.webhooks() });
    },
  });

  const test = useMutation({
    mutationFn: (webhook: Webhook) =>
      fetchJson<WebhookDelivery>(`/api/v1/webhooks/${webhook.id}/test`, { method: "POST" }),
    onSuccess: (_delivery, webhook) => {
      setSelected(webhook);
      qc.invalidateQueries({ queryKey: qk.webhooks() });
      qc.invalidateQueries({ queryKey: qk.webhookDeliveries(webhook.id) });
    },
  });

  const rotate = useMutation({
    mutationFn: (webhook: Webhook) =>
      fetchJson<Webhook>(`/api/v1/webhooks/${webhook.id}/rotate-secret`, { method: "POST" }),
    onSuccess: (updated) => {
      setSecret(updated.secret ? { label: "Rotated webhook signing secret", value: updated.secret } : null);
      qc.invalidateQueries({ queryKey: qk.webhooks() });
      qc.invalidateQueries({ queryKey: qk.webhookDeliveries(updated.id) });
    },
  });

  function submitCreate(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const picked = splitEvents(events);
    if (picked.length === 0) return;
    create.mutate({ name, url, events: picked, active });
  }

  const sub = "Outbound notifications when things happen. HMAC-signed with a per-subscription secret.";
  const actions = (
    <button type="button" className="btn btn--moss" onClick={() => setCreateOpen(true)}>
      + New subscription
    </button>
  );

  if (q.isPending) return (
    <DeskPage title="Webhooks" sub={sub} actions={actions}>
      <CreateDialog
        open={createOpen}
        name={name}
        url={url}
        events={events}
        active={active}
        pending={create.isPending}
        error={create.isError ? create.error.message : null}
        onName={setName}
        onUrl={setUrl}
        onEvents={setEvents}
        onActive={setActive}
        onClose={() => setCreateOpen(false)}
        onSubmit={submitCreate}
      />
      <Loading />
    </DeskPage>
  );
  if (!q.data) return (
    <DeskPage title="Webhooks" sub={sub} actions={actions}>
      <CreateDialog
        open={createOpen}
        name={name}
        url={url}
        events={events}
        active={active}
        pending={create.isPending}
        error={create.isError ? create.error.message : null}
        onName={setName}
        onUrl={setUrl}
        onEvents={setEvents}
        onActive={setActive}
        onClose={() => setCreateOpen(false)}
        onSubmit={submitCreate}
      />
      Failed to load.
    </DeskPage>
  );

  return (
    <DeskPage title="Webhooks" sub={sub} actions={actions}>
      <CreateDialog
        open={createOpen}
        name={name}
        url={url}
        events={events}
        active={active}
        pending={create.isPending}
        error={create.isError ? create.error.message : null}
        onName={setName}
        onUrl={setUrl}
        onEvents={setEvents}
        onActive={setActive}
        onClose={() => setCreateOpen(false)}
        onSubmit={submitCreate}
      />
      {secret && <SecretReveal label={secret.label} value={secret.value} onDismiss={() => setSecret(null)} />}
      <div className="panel">
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>URL</th><th>Events</th><th>Last delivery</th><th>Status</th><th></th>
            </tr>
          </thead>
          <tbody>
            {q.data.map((w) => {
              const ok = isOkStatus(w.last_delivery_status);
              return (
                <tr key={w.id}>
                  <td className="mono">{w.url}</td>
                  <td>
                    {w.events.map((e) => (
                      <Chip key={e} tone="ghost" size="sm">{e}</Chip>
                    ))}
                  </td>
                  <td className="mono">{w.last_delivery_at ? fmt(w.last_delivery_at) : "never"}</td>
                  <td>
                    {!w.active ? (
                      <Chip tone="ghost" size="sm">disabled</Chip>
                    ) : w.last_delivery_status === null ? (
                      <Chip tone="ghost" size="sm">pending</Chip>
                    ) : ok ? (
                      <Chip tone="moss" size="sm">{statusText(w.last_delivery_status)} · active</Chip>
                    ) : (
                      <Chip tone="rust" size="sm">{statusText(w.last_delivery_status)} · failing</Chip>
                    )}
                  </td>
                  <td>
                    <button type="button" className="btn btn--sm btn--ghost" onClick={() => setSelected(w)}>
                      Log
                    </button>{" "}
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost"
                      disabled={test.isPending && test.variables?.id === w.id}
                      onClick={() => test.mutate(w)}
                    >
                      {test.isPending && test.variables?.id === w.id ? "Testing…" : "Test"}
                    </button>{" "}
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost"
                      disabled={rotate.isPending && rotate.variables?.id === w.id}
                      onClick={() => rotate.mutate(w)}
                    >
                      {rotate.isPending && rotate.variables?.id === w.id ? "Rotating…" : "Rotate secret"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {test.isError && <p className="tokens-form__error">{test.error.message}</p>}
      {rotate.isError && <p className="tokens-form__error">{rotate.error.message}</p>}
      {selected && (
        <DeliveryLogDrawer
          webhook={selected}
          deliveries={deliveriesQ.data}
          loading={deliveriesQ.isPending}
          error={deliveriesQ.isError ? deliveriesQ.error.message : null}
          onClose={() => setSelected(null)}
        />
      )}
    </DeskPage>
  );
}

interface CreateDialogProps {
  open: boolean;
  name: string;
  url: string;
  events: string;
  active: boolean;
  pending: boolean;
  error: string | null;
  onName: (value: string) => void;
  onUrl: (value: string) => void;
  onEvents: (value: string) => void;
  onActive: (value: boolean) => void;
  onClose: () => void;
  onSubmit: (e: FormEvent<HTMLFormElement>) => void;
}

function CreateDialog(props: CreateDialogProps) {
  const {
    open,
    name,
    url,
    events,
    active,
    pending,
    error,
    onName,
    onUrl,
    onEvents,
    onActive,
    onClose,
    onSubmit,
  } = props;

  return (
    <dialog className="modal" open={open} onClose={onClose} aria-label="New webhook subscription">
      <form className="modal__body" onSubmit={onSubmit}>
        <h3 className="modal__title">New subscription</h3>
        <p className="modal__sub">Create an HMAC-signed endpoint for workspace events.</p>

        <label className="field">
          <span>Name</span>
          <input value={name} onChange={(e) => onName(e.target.value)} placeholder="hermes-prod" required />
        </label>
        <label className="field">
          <span>URL</span>
          <input
            type="url"
            value={url}
            onChange={(e) => onUrl(e.target.value)}
            placeholder="https://example.com/crewday"
            required
          />
        </label>
        <label className="field">
          <span>Events</span>
          <textarea
            value={events}
            onChange={(e) => onEvents(e.target.value)}
            placeholder="task.completed, approval.pending"
            required
          />
        </label>
        <label className="field--inline">
          <input type="checkbox" checked={active} onChange={(e) => onActive(e.target.checked)} />
          <span>Active</span>
        </label>

        {error && <p className="tokens-form__error">{error}</p>}

        <div className="modal__actions">
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn--moss" disabled={pending || splitEvents(events).length === 0}>
            {pending ? "Creating…" : "Create subscription"}
          </button>
        </div>
      </form>
    </dialog>
  );
}

function SecretReveal({
  label,
  value,
  onDismiss,
}: {
  label: string;
  value: string;
  onDismiss: () => void;
}) {
  return (
    <section className="tokens-reveal" role="status" aria-live="polite">
      <header className="tokens-reveal__ribbon">
        <div>
          <div className="tokens-reveal__ribbon-title">Save this secret now</div>
          <div className="tokens-reveal__ribbon-sub">
            Only shown once. Copy it before you dismiss this panel.
          </div>
        </div>
        <button type="button" className="btn btn--ghost btn--sm tokens-reveal__dismiss" onClick={onDismiss}>
          Dismiss
        </button>
      </header>
      <div className="tokens-reveal__body">
        <p className="tokens-reveal__label">{label}</p>
        <div className="tokens-reveal__secret-row">
          <code className="tokens-reveal__secret" aria-label="Plaintext webhook secret">{value}</code>
        </div>
      </div>
    </section>
  );
}

function DeliveryLogDrawer({
  webhook,
  deliveries,
  loading,
  error,
  onClose,
}: {
  webhook: Webhook;
  deliveries: WebhookDelivery[] | undefined;
  loading: boolean;
  error: string | null;
  onClose: () => void;
}) {
  useCloseOnEscape(onClose);

  return (
    <>
      <div className="day-drawer__scrim" onClick={onClose} />
      <aside className="day-drawer" role="dialog" aria-label={`Delivery log for ${webhook.url}`}>
        <header className="day-drawer__head">
          <div>
            <div className="day-drawer__eyebrow">Delivery log</div>
            <div className="day-drawer__title">{webhook.name || webhook.url}</div>
          </div>
          <button type="button" className="day-drawer__close" onClick={onClose} aria-label="Close delivery log">
            ×
          </button>
        </header>
        <div className="day-drawer__body">
          {loading ? (
            <Loading />
          ) : error ? (
            <p className="day-drawer__muted">{error}</p>
          ) : !deliveries || deliveries.length === 0 ? (
            <p className="day-drawer__muted">No deliveries yet.</p>
          ) : (
            <table className="table table--roomy">
              <thead>
                <tr>
                  <th>Event</th><th>Status</th><th>Attempt</th><th>Response</th><th>Created</th>
                </tr>
              </thead>
              <tbody>
                {deliveries.map((delivery) => (
                  <tr key={delivery.id}>
                    <td>{delivery.event}</td>
                    <td>
                      <Chip tone={delivery.status === "succeeded" ? "moss" : "ghost"} size="sm">
                        {delivery.status}
                      </Chip>
                    </td>
                    <td className="mono">{delivery.attempt}</td>
                    <td className="mono">{deliveryResponseText(delivery)}</td>
                    <td className="mono">{fmt(delivery.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </aside>
    </>
  );
}
