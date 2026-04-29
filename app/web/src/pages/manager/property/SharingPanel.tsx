import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Chip } from "@/components/common";
import type { AvailableWorkspace, PropertyWorkspace } from "@/types/api";
import { fmtDayMon } from "./lib/propertyFormatters";
import type { PropertyDetail } from "./types";

const MEMBERSHIP_LABEL: Record<string, string> = {
  owner_workspace: "Owner workspace",
  managed_workspace: "Managed workspace",
  observer_workspace: "Observer workspace",
};

const MEMBERSHIP_TONE: Record<string, "moss" | "sky" | "ghost"> = {
  owner_workspace: "moss",
  managed_workspace: "sky",
  observer_workspace: "ghost",
};

export default function SharingPanel({
  detail,
  meAvailable,
}: {
  detail: PropertyDetail;
  meAvailable: AvailableWorkspace[];
}) {
  const queryClient = useQueryClient();
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const [confirm, setConfirm] = useState<
    | { kind: "share"; workspaceRef: string }
    | { kind: "revoke"; workspaceRef: string }
    | null
  >(null);
  const [shareTarget, setShareTarget] = useState<string>("");

  useEffect(() => {
    const el = dialogRef.current;
    if (!el) return;
    if (confirm && !el.open) el.showModal();
    if (!confirm && el.open) el.close();
  }, [confirm]);

  const shareMu = useMutation({
    mutationFn: (vars: { workspace_slug: string }) =>
      fetchJson<PropertyWorkspace>("/api/v1/properties/" + detail.property.id + "/share", {
        method: "POST",
        body: {
          workspace_slug: vars.workspace_slug,
          membership_role: "managed_workspace",
        },
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.property(detail.property.id) });
      void queryClient.invalidateQueries({ queryKey: qk.propertyWorkspaces() });
      void queryClient.invalidateQueries({ queryKey: qk.propertyWorkspaces(detail.property.id) });
      setShareTarget("");
      setConfirm(null);
    },
  });
  const revokeMu = useMutation({
    mutationFn: (vars: { workspace_ref: string }) =>
      fetchJson<{ ok: boolean }>("/api/v1/properties/" + detail.property.id + "/share/" + encodeURIComponent(vars.workspace_ref), {
        method: "DELETE",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.property(detail.property.id) });
      void queryClient.invalidateQueries({ queryKey: qk.propertyWorkspaces() });
      void queryClient.invalidateQueries({ queryKey: qk.propertyWorkspaces(detail.property.id) });
      setConfirm(null);
    },
  });

  const wsById = new Map(detail.membership_workspaces.map((w) => [w.id, w]));
  const linkedIds = new Set(detail.memberships.map((m) => m.workspace_id));
  const owner = detail.memberships.find((m) => m.membership_role === "owner_workspace");
  const isOwnerSurface = owner?.workspace_id === detail.active_workspace_id;
  const shareCandidates = meAvailable.filter((a) => {
    const actualWorkspaceId = detail.workspace_id_by_slug[a.workspace.id] ?? a.workspace.id;
    return !linkedIds.has(actualWorkspaceId);
  });

  return (
    <div className="panel">
      <header className="panel__head">
        <h2>Sharing &amp; client</h2>
        {isOwnerSurface && shareCandidates.length > 0 && (
          <div className="sharing-add">
            <label className="field field--inline">
              <span className="muted">Workspace</span>
              <select
                value={shareTarget}
                onChange={(e) => setShareTarget(e.target.value)}
              >
                <option value="">Invite a workspace…</option>
                {shareCandidates.map((c) => (
                  <option key={c.workspace.id} value={c.workspace.id}>
                    {c.workspace.name}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="btn btn--moss btn--sm"
              disabled={!shareTarget}
              onClick={() => setConfirm({ kind: "share", workspaceRef: shareTarget })}
            >
              Invite as agency
            </button>
          </div>
        )}
      </header>
      <p className="muted">
        Multi-belonging property. The owner workspace controls who else may see or manage it.
        Worker shifts and work orders carry their own workspace tag forward, so payroll, billing and
        history stay separated even when several teams share the same villa.
      </p>

      <table className="table">
        <thead>
          <tr><th>Workspace</th><th>Membership</th><th>Since</th><th></th></tr>
        </thead>
        <tbody>
          {detail.memberships.map((m) => {
            const ws = wsById.get(m.workspace_id);
            const isCurrent = m.workspace_id === detail.active_workspace_id;
            const canRevoke = isOwnerSurface && m.membership_role !== "owner_workspace";
            return (
              <tr key={m.workspace_id}>
                <td>
                  <strong>{ws?.name ?? m.workspace_id}</strong>
                  {isCurrent && <span className="muted"> (current view)</span>}
                </td>
                <td>
                  <Chip tone={MEMBERSHIP_TONE[m.membership_role] ?? "ghost"} size="sm">
                    {MEMBERSHIP_LABEL[m.membership_role] ?? m.membership_role}
                  </Chip>
                </td>
                <td className="table__mono">{fmtDayMon(m.added_at)}</td>
                <td>
                  {canRevoke && (
                    <button
                      type="button"
                      className="btn btn--ghost btn--sm"
                      onClick={() => setConfirm({ kind: "revoke", workspaceRef: detail.workspace_slug_by_id[m.workspace_id] ?? m.workspace_id })}
                    >
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <div className="sharing-client">
        <h3>Billing client</h3>
        {detail.client_org ? (
          <div className="sharing-client__row">
            <div>
              <strong>{detail.client_org.name}</strong>
              {detail.client_org.legal_name && (
                <div className="muted">{detail.client_org.legal_name}</div>
              )}
              {detail.client_org.tax_id && (
                <div className="muted mono">{detail.client_org.tax_id}</div>
              )}
            </div>
            <div className="sharing-client__chips">
              <Chip tone="sand" size="sm">{detail.client_org.default_currency}</Chip>
              {detail.client_org.is_client && <Chip tone="moss" size="sm">Client</Chip>}
              {detail.client_org.is_supplier && <Chip tone="sky" size="sm">Supplier</Chip>}
            </div>
          </div>
        ) : (
          <p className="muted">
            No client organization linked. Shifts and vendor invoices for this property are
            paid by the workspace itself; no client-billing rollup applies.
          </p>
        )}
        {detail.owner_user && (
          <div className="sharing-client__owner">
            <span className="muted">Owner of record:</span>{" "}
            <strong>{detail.owner_user.display_name}</strong>
          </div>
        )}
      </div>

      <dialog className="modal" ref={dialogRef} onClose={() => setConfirm(null)}>
        {confirm && (
          <div className="modal__body">
            <h3 className="modal__title">
              {confirm.kind === "share" ? "Invite this workspace as agency?" : "Revoke this workspace?"}
            </h3>
            <p className="modal__sub">
              {confirm.kind === "share"
                ? "Adds a managed_workspace link. The workspace gains operational access — its members can dispatch workers and create work orders here. Acceptance and invoicing remain bound to the owner workspace's policy."
                : "Removes the property_workspace link. In production this is approval-gated. The mock skips the approval and applies it immediately so you can see the result."}
            </p>
            <div className="modal__actions">
              <button type="button" className="btn btn--ghost" onClick={() => setConfirm(null)}>
                Cancel
              </button>
              <button
                type="button"
                className={"btn " + (confirm.kind === "share" ? "btn--moss" : "btn--rust")}
                disabled={shareMu.isPending || revokeMu.isPending}
                onClick={() => {
                  if (confirm.kind === "share") shareMu.mutate({ workspace_slug: confirm.workspaceRef });
                  else revokeMu.mutate({ workspace_ref: confirm.workspaceRef });
                }}
              >
                {confirm.kind === "share" ? "Invite" : "Revoke"}
              </button>
            </div>
          </div>
        )}
      </dialog>
    </div>
  );
}
