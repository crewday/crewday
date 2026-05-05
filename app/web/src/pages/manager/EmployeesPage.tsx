import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Avatar, Chip, Loading } from "@/components/common";
import { fmtTime } from "@/lib/dates";
import type { Booking, Employee, Me, Property } from "@/types/api";

interface InviteEmployeeRequest {
  email: string;
  display_name: string;
  grants: Array<{
    scope_kind: "workspace";
    scope_id: string;
    grant_role: "worker";
  }>;
}

interface InviteEmployeeResponse {
  invite_id: string;
  pending_email: string;
  user_id: string | null;
  user_created: boolean;
}

export default function EmployeesPage() {
  const empsQ = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const bookingsQ = useQuery({
    queryKey: qk.bookings(),
    queryFn: () => fetchJson<Booking[]>("/api/v1/bookings"),
  });
  const inviteAction = <InviteEmployeeAction />;

  if (empsQ.isPending || propsQ.isPending) {
    return (
      <DeskPage title="Employees" actions={inviteAction}>
        <Loading />
      </DeskPage>
    );
  }
  if (!empsQ.data || !propsQ.data) {
    return (
      <DeskPage title="Employees" actions={inviteAction}>
        Failed to load.
      </DeskPage>
    );
  }

  const employees = empsQ.data;
  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));

  return (
    <DeskPage
      title="Employees"
      actions={inviteAction}
    >
      <div className="panel">
        <table className="table table--roomy">
          <thead>
            <tr>
              <th></th>
              <th>Name</th>
              <th>Roles</th>
              <th>Properties</th>
              <th>Phone</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {employees.map((e) => (
              <tr key={e.id}>
                <td><Avatar url={e.avatar_url} initials={e.avatar_initials} size="md" alt={e.name} /></td>
                <td>
                  <Link className="link" to={"/employee/" + e.id}>{e.name}</Link>
                </td>
                <td>
                  {e.roles.map((r) => (
                    <Chip key={r} tone="ghost" size="sm">{r}</Chip>
                  ))}
                </td>
                <td>
                  {e.properties.map((pid) => {
                    const p = propsById.get(pid);
                    if (!p) return null;
                    return <Chip key={pid} tone={p.color} size="sm">{p.name}</Chip>;
                  })}
                </td>
                <td className="table__mono">{e.phone}</td>
                <td>
                  {(() => {
                    const now = Date.now();
                    const active = bookingsQ.data?.find(
                      (b) =>
                        b.employee_id === e.id &&
                        b.status === "scheduled" &&
                        new Date(b.scheduled_start).getTime() <= now &&
                        new Date(b.scheduled_end).getTime() >= now,
                    );
                    return active ? (
                      <Chip tone="moss" size="sm">Booked · until {fmtTime(active.scheduled_end)}</Chip>
                    ) : (
                      <Chip tone="ghost" size="sm">Free</Chip>
                    );
                  })()}
                </td>
                <td>
                  <Link className="link link--muted" to={"/employee/" + e.id}>View →</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}

function InviteEmployeeAction() {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const queryClient = useQueryClient();
  const meQ = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [sentInvite, setSentInvite] = useState<InviteEmployeeResponse | null>(null);

  const invite = useMutation({
    mutationFn: (payload: InviteEmployeeRequest) =>
      fetchJson<InviteEmployeeResponse>("/api/v1/users/invite", {
        method: "POST",
        body: payload,
      }),
    onSuccess: (result) => {
      setSentInvite(result);
      setName("");
      setEmail("");
      setFormError(null);
      void queryClient.invalidateQueries({ queryKey: qk.employees() });
      void queryClient.invalidateQueries({ queryKey: qk.users() });
    },
    onError: (error) => {
      setFormError(inviteEmployeeErrorMessage(error));
    },
  });

  const workspaceId = meQ.data?.current_workspace_id ?? "";

  function reset(): void {
    if (invite.isPending) return;
    setName("");
    setEmail("");
    setFormError(null);
    setSentInvite(null);
    invite.reset();
  }

  return (
    <>
      <button
        type="button"
        className="btn btn--moss"
        onClick={() => dialogRef.current?.showModal()}
      >
        + Invite employee
      </button>

      <dialog
        className="modal"
        ref={dialogRef}
        aria-labelledby="invite-employee-title"
        onCancel={(event) => {
          if (invite.isPending) event.preventDefault();
        }}
        onClose={reset}
      >
        <form
          className="modal__body"
          onSubmit={(event) => {
            event.preventDefault();
            if (invite.isPending || sentInvite) return;
            const trimmedName = name.trim();
            const trimmedEmail = email.trim();
            if (!trimmedName) {
              setFormError("Enter the employee's full name before sending the invite.");
              return;
            }
            if (!trimmedEmail) {
              setFormError("Enter the employee's email address before sending the invite.");
              return;
            }
            if (!workspaceId) {
              setFormError("Workspace context is still loading. Wait a moment and try again.");
              return;
            }
            setFormError(null);
            invite.mutate({
              email: trimmedEmail,
              display_name: trimmedName,
              grants: [
                {
                  scope_kind: "workspace",
                  scope_id: workspaceId,
                  grant_role: "worker",
                },
              ],
            });
          }}
        >
          <h3 id="invite-employee-title" className="modal__title">Invite employee</h3>
          <p className="modal__sub">
            Send a click-to-accept invite for this workspace.
          </p>

          {sentInvite ? (
            <>
              <p className="form-notice form-notice--success" role="status">
                Invite sent to {sentInvite.pending_email}. They will receive the acceptance link by email.
              </p>
              <p className="muted table__mono">Invite ID: {sentInvite.invite_id}</p>
            </>
          ) : (
            <>
              <label className="field">
                <span>Full name</span>
                <input
                  autoFocus
                  required
                  value={name}
                  onChange={(event) => {
                    setName(event.target.value);
                    setFormError(null);
                  }}
                  placeholder="e.g. Riley Chen"
                />
              </label>

              <label className="field">
                <span>Email</span>
                <input
                  type="email"
                  required
                  value={email}
                  onChange={(event) => {
                    setEmail(event.target.value);
                    setFormError(null);
                  }}
                  placeholder="riley@example.com"
                />
              </label>
            </>
          )}

          {meQ.isError && !sentInvite && (
            <p className="form-error" role="alert">
              Workspace context could not load. Refresh and try again.
            </p>
          )}
          {formError && <p className="form-error" role="alert">{formError}</p>}

          <div className="modal__actions">
            <button
              type="button"
              className="btn btn--ghost"
              disabled={invite.isPending}
              onClick={() => dialogRef.current?.close()}
            >
              {sentInvite ? "Done" : "Cancel"}
            </button>
            {!sentInvite && (
              <button
                type="submit"
                className="btn btn--moss"
                disabled={invite.isPending || !name.trim() || !email.trim() || !workspaceId}
              >
                {invite.isPending ? "Sending..." : "Send invite"}
              </button>
            )}
          </div>
        </form>
      </dialog>
    </>
  );
}

function inviteEmployeeErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const fieldMessages = error.fieldErrors
      .map((fieldError) => {
        const label = inviteFieldLabel(fieldError.loc);
        const message = fieldError.msg?.trim();
        if (!message) return null;
        return label ? `${label}: ${message}` : message;
      })
      .filter((message): message is string => Boolean(message));
    if (fieldMessages.length > 0) {
      return "Could not send invite. " + fieldMessages.join(" ");
    }
    if (error.status === 422) {
      return error.detail ?? error.title ?? "Could not send invite. Check the fields and try again.";
    }
    return error.detail ?? error.title ?? "Could not send invite. Try again in a moment.";
  }
  if (error instanceof Error && error.message) return error.message;
  return "Could not send invite. Try again in a moment.";
}

function inviteFieldLabel(loc: readonly (string | number)[] | undefined): string | null {
  const field = loc?.at(-1);
  if (field === "email") return "Email";
  if (field === "display_name") return "Full name";
  if (field === "grants") return "Role";
  return null;
}
