import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { BriefcaseBusiness, Pencil, Plus, Trash2 } from "lucide-react";
import { ApiError, fetchJson } from "@/lib/api";
import { fetchAllList } from "@/lib/fetchAllList";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import DateTime from "@/components/DateTime";
import FormField from "@/components/FormField";
import { AssetIcon, isAssetIconName } from "@/components/AssetIcon";
import IconSelector from "@/components/IconSelector";
import { Avatar, Chip, EmptyState, Loading } from "@/components/common";
import { workspaceRouteForPathname } from "@/lib/workspaceRoutes";
import type { Booking, Employee, Me, Property, WorkRole } from "@/types/api";

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

interface WorkRoleFormState {
  name: string;
  key: string;
  description_md: string;
  icon_name: string;
}

type WorkRoleField = keyof WorkRoleFormState;

interface WorkRoleWriteRequest extends WorkRoleFormState {
  default_settings_json?: Record<string, unknown>;
}

const EMPTY_WORK_ROLE_FORM: WorkRoleFormState = {
  name: "",
  key: "",
  description_md: "",
  icon_name: "",
};

function workRoleInitials(name: string): string {
  const initials = name
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");
  return initials || "?";
}

export default function EmployeesPage() {
  const { pathname } = useLocation();
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
      <WorkRoleCatalogManager />

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
                  <Link className="link" to={workspaceRouteForPathname(pathname, "/employee/" + e.id)}>{e.name}</Link>
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
                      <Chip tone="moss" size="sm">
                        Booked · until <DateTime value={active.scheduled_end} showTime />
                      </Chip>
                    ) : (
                      <Chip tone="ghost" size="sm">Free</Chip>
                    );
                  })()}
                </td>
                <td>
                  <Link className="link link--muted" to={workspaceRouteForPathname(pathname, "/employee/" + e.id)}>View →</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}

function WorkRoleCatalogManager() {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const deleteDialogRef = useRef<HTMLDialogElement>(null);
  const queryClient = useQueryClient();
  const rolesQ = useQuery({
    queryKey: qk.workRoles(),
    queryFn: () => fetchAllList<WorkRole>("/api/v1/work_roles"),
  });
  const [editingRole, setEditingRole] = useState<WorkRole | null>(null);
  const [roleToDelete, setRoleToDelete] = useState<WorkRole | null>(null);
  const [form, setForm] = useState<WorkRoleFormState>(EMPTY_WORK_ROLE_FORM);
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<WorkRoleField, string>>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const roles = rolesQ.data ?? [];

  const invalidateRoleDependents = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: qk.workRoles() }),
      queryClient.invalidateQueries({ queryKey: qk.employees() }),
      queryClient.invalidateQueries({
        predicate: (query) => {
          const key = query.queryKey;
          return Array.isArray(key) && key.includes("employee") && key.includes("user_work_roles");
        },
      }),
    ]);

  const saveRole = useMutation({
    mutationFn: (payload: WorkRoleWriteRequest) => {
      if (editingRole) {
        return fetchJson<WorkRole>("/api/v1/work_roles/" + encodeURIComponent(editingRole.id), {
          method: "PATCH",
          body: payload,
        });
      }
      return fetchJson<WorkRole>("/api/v1/work_roles", {
        method: "POST",
        body: { ...payload, default_settings_json: {} },
      });
    },
    onSuccess: async () => {
      await invalidateRoleDependents();
      dialogRef.current?.close();
    },
    onError: (error) => {
      const nextFieldErrors = workRoleFieldErrors(error);
      setFieldErrors(nextFieldErrors);
      setFormError(workRoleErrorMessage(error, nextFieldErrors));
    },
  });

  const deleteRole = useMutation({
    mutationFn: (role: WorkRole) =>
      fetchJson<void>("/api/v1/work_roles/" + encodeURIComponent(role.id), {
        method: "DELETE",
      }),
    onSuccess: async () => {
      await invalidateRoleDependents();
      deleteDialogRef.current?.close();
    },
  });

  function openCreateDialog(): void {
    setEditingRole(null);
    setForm(EMPTY_WORK_ROLE_FORM);
    setFieldErrors({});
    setFormError(null);
    saveRole.reset();
    dialogRef.current?.showModal();
  }

  function openEditDialog(role: WorkRole): void {
    setEditingRole(role);
    setForm({
      name: role.name,
      key: role.key,
      description_md: role.description_md,
      icon_name: role.icon_name,
    });
    setFieldErrors({});
    setFormError(null);
    saveRole.reset();
    dialogRef.current?.showModal();
  }

  function openDeleteDialog(role: WorkRole): void {
    setRoleToDelete(role);
    deleteRole.reset();
    deleteDialogRef.current?.showModal();
  }

  function setField(field: WorkRoleField, value: string): void {
    setForm((prev) => ({ ...prev, [field]: value }));
    setFieldErrors((prev) => ({ ...prev, [field]: undefined }));
    setFormError(null);
  }

  function submitForm(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const payload: WorkRoleWriteRequest = {
      name: form.name.trim(),
      key: form.key.trim(),
      description_md: form.description_md.trim(),
      icon_name: workRoleIconNameForSubmit(form.icon_name),
    };
    const nextErrors: Partial<Record<WorkRoleField, string>> = {};
    if (!payload.name) nextErrors.name = "Enter a role name.";
    if (!payload.key) nextErrors.key = "Enter a role key.";
    if (Object.keys(nextErrors).length > 0) {
      setFieldErrors(nextErrors);
      setFormError("Fix the highlighted fields before saving.");
      return;
    }
    setFieldErrors({});
    setFormError(null);
    saveRole.mutate(payload);
  }

  return (
    <section className="panel work-role-catalog" aria-labelledby="work-role-catalog-title">
      <header className="panel__head work-role-catalog__head">
        <div>
          <h2 id="work-role-catalog-title">Work roles</h2>
          <p className="work-role-catalog__sub">
            Workspace job definitions available for employee assignment.
          </p>
        </div>
        <button type="button" className="btn btn--moss" onClick={openCreateDialog}>
          <Plus size={16} aria-hidden="true" />
          Add role
        </button>
      </header>

      {rolesQ.isPending ? (
        <Loading />
      ) : rolesQ.isError ? (
        <p className="form-error" role="alert">
          Work roles could not be loaded.
        </p>
      ) : roles.length === 0 ? (
        <EmptyState
          icon={BriefcaseBusiness}
          title="No work roles yet"
          copy="Create the first role before assigning employees to jobs."
          action={
            <button type="button" className="btn btn--moss" onClick={openCreateDialog}>
              <Plus size={16} aria-hidden="true" />
              Add role
            </button>
          }
          variant="quiet"
        />
      ) : (
        <ul className="work-role-list">
          {roles.map((role) => (
            <li key={role.id} className="work-role-row">
              <div className="work-role-row__mark" aria-hidden="true">
                {role.icon_name ? (
                  <AssetIcon name={role.icon_name} size={18} className="work-role-row__icon" />
                ) : (
                  workRoleInitials(role.name)
                )}
              </div>
              <div className="work-role-row__main">
                <strong>{role.name}</strong>
                {role.description_md ? (
                  <p className="work-role-row__description">{role.description_md}</p>
                ) : null}
              </div>
              <div className="work-role-row__actions">
                <button
                  type="button"
                  className="btn btn--ghost btn--sm work-role-row__action"
                  aria-label="Edit"
                  onClick={() => openEditDialog(role)}
                >
                  <Pencil size={14} aria-hidden="true" />
                  <span className="work-role-row__action-label">Edit</span>
                </button>
                <button
                  type="button"
                  className="btn btn--ghost btn--sm work-role-row__action"
                  aria-label="Remove"
                  onClick={() => openDeleteDialog(role)}
                >
                  <Trash2 size={14} aria-hidden="true" />
                  <span className="work-role-row__action-label">Remove</span>
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <dialog
        className="modal modal--sheet sheet-form-dialog"
        ref={dialogRef}
        aria-labelledby="work-role-dialog-title"
        onCancel={(event) => {
          if (saveRole.isPending) event.preventDefault();
        }}
        onClose={() => {
          if (saveRole.isPending) return;
          setEditingRole(null);
          setForm(EMPTY_WORK_ROLE_FORM);
          setFieldErrors({});
          setFormError(null);
          saveRole.reset();
        }}
      >
        <form className="work-role-form sheet-form" onSubmit={submitForm} noValidate>
          <header className="work-role-form__head sheet-form__head">
            <div>
              <p className="work-role-form__eyebrow sheet-form__eyebrow">Work role</p>
              <h3 id="work-role-dialog-title" className="work-role-form__title sheet-form__title">
                {editingRole ? "Edit work role" : "Add work role"}
              </h3>
              <p className="work-role-form__sub sheet-form__sub">
                Keys are stable slugs used by assignments and integrations. Rename with care.
              </p>
            </div>
            <button
              type="button"
              className="work-role-form__close sheet-form__close"
              aria-label="Close"
              disabled={saveRole.isPending}
              onClick={() => dialogRef.current?.close()}
            >
              ×
            </button>
          </header>

          <div className="work-role-form__body sheet-form__body">
          <FormField label="Name" requirement="required" className="work-role-form__field sheet-form__field">
            <input
              autoFocus
              required
              value={form.name}
              aria-invalid={fieldErrors.name ? "true" : undefined}
              aria-describedby={fieldErrors.name ? "work-role-name-error" : undefined}
              onChange={(event) => setField("name", event.currentTarget.value)}
              placeholder="e.g. Housekeeper"
            />
            {fieldErrors.name ? <span id="work-role-name-error" className="form-field-error">{fieldErrors.name}</span> : null}
          </FormField>

          <FormField label="Key" requirement="required" className="work-role-form__field sheet-form__field">
            <input
              required
              value={form.key}
              aria-invalid={fieldErrors.key ? "true" : undefined}
              aria-describedby={fieldErrors.key ? "work-role-key-error" : undefined}
              onChange={(event) => setField("key", event.currentTarget.value)}
              placeholder="e.g. housekeeper"
            />
            {fieldErrors.key ? <span id="work-role-key-error" className="form-field-error">{fieldErrors.key}</span> : null}
          </FormField>

          <IconSelector
            label="Icon"
            value={form.icon_name}
            onChange={(value) => setField("icon_name", value)}
            className="work-role-form__field sheet-form__field"
            error={fieldErrors.icon_name}
            errorId="work-role-icon-error"
          />

          <FormField label="Description" requirement="optional" className="work-role-form__field sheet-form__field">
            <textarea
              rows={4}
              value={form.description_md}
              aria-invalid={fieldErrors.description_md ? "true" : undefined}
              aria-describedby={fieldErrors.description_md ? "work-role-description-error" : undefined}
              onChange={(event) => setField("description_md", event.currentTarget.value)}
              placeholder="What this role covers in this workspace."
            />
            {fieldErrors.description_md ? (
              <span id="work-role-description-error" className="form-field-error">{fieldErrors.description_md}</span>
            ) : null}
          </FormField>

          {formError ? <p className="form-error" role="alert">{formError}</p> : null}
          </div>

          <footer className="work-role-form__footer sheet-form__footer">
            <button
              type="button"
              className="btn btn--ghost"
              disabled={saveRole.isPending}
              onClick={() => dialogRef.current?.close()}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn--moss"
              disabled={saveRole.isPending || !form.name.trim() || !form.key.trim()}
            >
              {saveRole.isPending ? "Saving..." : "Save role"}
            </button>
          </footer>
        </form>
      </dialog>

      <dialog
        className="modal modal--sheet sheet-form-dialog"
        ref={deleteDialogRef}
        aria-labelledby="work-role-delete-title"
        onCancel={(event) => {
          if (deleteRole.isPending) event.preventDefault();
        }}
        onClose={() => {
          if (deleteRole.isPending) return;
          setRoleToDelete(null);
          deleteRole.reset();
        }}
      >
        <div className="modal__body">
          <h3 id="work-role-delete-title" className="modal__title">Remove work role?</h3>
          <p className="modal__sub">
            This soft-retires {roleToDelete ? roleToDelete.name : "the role"} and removes it from future
            employee assignment lists. Historical work remains attached to its original role record.
          </p>
          {deleteRole.isError ? (
            <p className="form-error" role="alert">
              {workRoleErrorMessage(deleteRole.error, {})}
            </p>
          ) : null}
          <div className="modal__actions">
            <button
              type="button"
              className="btn btn--ghost"
              disabled={deleteRole.isPending}
              onClick={() => deleteDialogRef.current?.close()}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn--rust"
              disabled={deleteRole.isPending || !roleToDelete}
              onClick={() => {
                if (roleToDelete) deleteRole.mutate(roleToDelete);
              }}
            >
              {deleteRole.isPending ? "Removing..." : "Remove role"}
            </button>
          </div>
        </div>
      </dialog>
    </section>
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
          className="invite-employee-form sheet-form"
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
          <header className="invite-employee-form__head sheet-form__head">
            <div>
              <p className="invite-employee-form__eyebrow sheet-form__eyebrow">Employee invite</p>
              <h3 id="invite-employee-title" className="invite-employee-form__title sheet-form__title">
                Invite employee
              </h3>
              <p className="invite-employee-form__sub sheet-form__sub">
                Send a click-to-accept invite for this workspace.
              </p>
            </div>
            <button
              type="button"
              className="invite-employee-form__close sheet-form__close"
              aria-label="Close"
              disabled={invite.isPending}
              onClick={() => dialogRef.current?.close()}
            >
              ×
            </button>
          </header>

          <div className="invite-employee-form__body sheet-form__body">
          {sentInvite ? (
            <>
              <p className="form-notice form-notice--success" role="status">
                Invite sent to {sentInvite.pending_email}. They will receive the acceptance link by email.
              </p>
              <p className="muted table__mono">Invite ID: {sentInvite.invite_id}</p>
            </>
          ) : (
            <>
              <FormField label="Full name" requirement="required" className="invite-employee-form__field sheet-form__field">
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
              </FormField>

              <FormField label="Email" requirement="required" className="invite-employee-form__field sheet-form__field">
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
              </FormField>
            </>
          )}

          {meQ.isError && !sentInvite && (
            <p className="form-error" role="alert">
              Workspace context could not load. Refresh and try again.
            </p>
          )}
          {formError && <p className="form-error" role="alert">{formError}</p>}
          </div>

          <footer className="invite-employee-form__footer sheet-form__footer">
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
          </footer>
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

function workRoleErrorMessage(
  error: unknown,
  fieldErrors: Partial<Record<WorkRoleField, string>>,
): string {
  const fieldMessages = Object.values(fieldErrors).filter((message): message is string => Boolean(message));
  if (fieldMessages.length > 0) {
    return "Could not save work role. " + fieldMessages.join(" ");
  }
  if (error instanceof ApiError) {
    if (workRoleProblemKey(error) === "work_role_key_conflict") {
      return "That role key is already used. Choose a unique key.";
    }
    if (error.status === 403) return "You do not have permission to manage work roles.";
    if (error.status === 401) return "Sign in again before managing work roles.";
    return error.detail ?? error.title ?? "Could not save work role. Check the fields and try again.";
  }
  if (error instanceof Error && error.message) return error.message;
  return "Could not save work role. Try again in a moment.";
}

function workRoleFieldErrors(error: unknown): Partial<Record<WorkRoleField, string>> {
  if (!(error instanceof ApiError)) return {};
  const errors: Partial<Record<WorkRoleField, string>> = {};
  for (const fieldError of error.fieldErrors) {
    const field = workRoleFieldFromLoc(fieldError.loc);
    const message = fieldError.msg?.trim();
    if (field && message) errors[field] = message;
  }
  return errors;
}

function workRoleFieldFromLoc(loc: readonly (string | number)[] | undefined): WorkRoleField | null {
  const field = loc?.at(-1);
  if (field === "name" || field === "key" || field === "description_md" || field === "icon_name") {
    return field;
  }
  return null;
}

function workRoleIconNameForSubmit(value: string): string {
  const trimmed = value.trim();
  return isAssetIconName(trimmed) ? trimmed : "";
}

function workRoleProblemKey(error: ApiError): string | null {
  const raw = error.problem?.error;
  return typeof raw === "string" ? raw : error.type;
}
