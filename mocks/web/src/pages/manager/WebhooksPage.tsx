import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import DateTime from "@/components/DateTime";
import { Chip, Loading } from "@/components/common";
import type { Webhook } from "@/types/api";

export default function WebhooksPage() {
  const q = useQuery({
    queryKey: qk.webhooks(),
    queryFn: () => fetchJson<Webhook[]>("/api/v1/webhooks"),
  });

  const sub = "Outbound notifications when things happen. HMAC-signed with a per-subscription secret.";
  const actions = <button className="btn btn--moss">+ New subscription</button>;

  if (q.isPending) return <DeskPage title="Webhooks" sub={sub} actions={actions}><Loading /></DeskPage>;
  if (!q.data) return <DeskPage title="Webhooks" sub={sub} actions={actions}>Failed to load.</DeskPage>;

  return (
    <DeskPage title="Webhooks" sub={sub} actions={actions}>
      <div className="panel">
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>URL</th><th>Events</th><th>Last delivery</th><th>Status</th><th></th>
            </tr>
          </thead>
          <tbody>
            {q.data.map((w) => {
              const ok = w.last_delivery_status >= 200 && w.last_delivery_status < 300;
              return (
                <tr key={w.id}>
                  <td className="mono">{w.url}</td>
                  <td>
                    {w.events.map((e) => (
                      <Chip key={e} tone="ghost" size="sm">{e}</Chip>
                    ))}
                  </td>
                  <td><DateTime value={w.last_delivery_at} showTime className="mono" /></td>
                  <td>
                    {!w.active ? (
                      <Chip tone="ghost" size="sm">disabled</Chip>
                    ) : ok ? (
                      <Chip tone="moss" size="sm">{w.last_delivery_status} · active</Chip>
                    ) : (
                      <Chip tone="rust" size="sm">{w.last_delivery_status} · failing</Chip>
                    )}
                  </td>
                  <td>
                    <button className="btn btn--sm btn--ghost">Test</button>{" "}
                    <button className="btn btn--sm btn--ghost">Rotate secret</button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
