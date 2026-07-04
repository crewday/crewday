import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { ApiError, fetchJson } from "@/lib/api";
import { fetchAllList } from "@/lib/fetchAllList";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import DateTime from "@/components/DateTime";
import FormField from "@/components/FormField";
import FormModal from "@/components/FormModal";
import { AssetIcon } from "@/components/AssetIcon";
import { isAssetIconName } from "@/components/AssetIcon.registry";
import {
  InlineIconField,
  InlineTableForm,
  InlineTextField,
  type InlineTableColumn,
  type InlineTableRow,
} from "@/components/InlineTableForm";
import { Avatar, Chip, Loading } from "@/components/common";
import { fieldErrorId, fieldErrorsByLoc, labeledFieldMessages } from "@/lib/apiErrorMessage";
import { clearMapValue, setMapValue } from "@/lib/mapState";
import { usePatchReducer } from "@/lib/usePatchReducer";
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

interface WorkRoleCatalogState {
  editedDrafts: ReadonlyMap<string, WorkRoleFormState>;
  rowFieldErrors: ReadonlyMap<string, Partial<Record<WorkRoleField, string>>>;
  rowErrors: ReadonlyMap<string, string>;
  createDraft: WorkRoleFormState;
  createDirty: boolean;
  createFieldErrors: Partial<Record<WorkRoleField, string>>;
  createError: string | null;
}

interface InviteEmployeeState {
  name: string;
  email: string;
  inviteDialogOpen: boolean;
  formError: string | null;
  sentInvite: InviteEmployeeResponse | null;
}

interface WorkRoleWriteRequest extends WorkRoleFormState {
  default_settings_json?: Record<string, unknown>;
}

const EMPTY_WORK_ROLE_FORM: WorkRoleFormState = {
  name: "",
  key: "",
  description_md: "",
  icon_name: "",
};
const CREATE_WORK_ROLE_ROW_ID = "__new_work_role__";
const INVITE_EMPLOYEE_ACTION = <InviteEmployeeAction />;

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
  const [nowMs] = useState(() => Date.now());

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
  const inviteAction = INVITE_EMPLOYEE_ACTION;

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
              <th aria-label="Actions"></th>
              <th>Name</th>
              <th>Roles</th>
              <th>Properties</th>
              <th>Phone</th>
              <th>Status</th>
              <th aria-label="Actions"></th>
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
                    const active = bookingsQ.data?.find(
                      (b) =>
                        b.employee_id === e.id &&
                        b.status === "scheduled" &&
                        new Date(b.scheduled_start).getTime() <= nowMs &&
                        new Date(b.scheduled_end).getTime() >= nowMs,
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

// react-doctor-disable-next-line react-doctor/no-giant-component -- Existing promoted surface is intentionally deferred until a focused component split preserves behavior.
function WorkRoleCatalogManager() {
  const queryClient = useQueryClient();
  const rolesQ = useQuery({
    queryKey: qk.workRoles(),
    queryFn: () => fetchAllList<WorkRole>("/api/v1/work_roles"),
  });
  const [catalogState, setCatalogState] = usePatchReducer<WorkRoleCatalogState>(() => ({
    editedDrafts: new Map(),
    rowFieldErrors: new Map(),
    rowErrors: new Map(),
    createDraft: EMPTY_WORK_ROLE_FORM,
    createDirty: false,
    createFieldErrors: {},
    createError: null,
  }));
  const {
    editedDrafts,
    rowFieldErrors,
    rowErrors,
    createDraft,
    createDirty,
    createFieldErrors,
    createError,
  } = catalogState;
  const setEditedDrafts = (
    update: ReadonlyMap<string, WorkRoleFormState>
      | ((current: ReadonlyMap<string, WorkRoleFormState>) => ReadonlyMap<string, WorkRoleFormState>),
  ) => setCatalogState((current) => ({
    ...current,
    editedDrafts: typeof update === "function" ? update(current.editedDrafts) : update,
  }));
  const setRowFieldErrors = (
    update: ReadonlyMap<string, Partial<Record<WorkRoleField, string>>>
      | ((
        current: ReadonlyMap<string, Partial<Record<WorkRoleField, string>>>,
      ) => ReadonlyMap<string, Partial<Record<WorkRoleField, string>>>),
  ) => setCatalogState((current) => ({
    ...current,
    rowFieldErrors: typeof update === "function" ? update(current.rowFieldErrors) : update,
  }));
  const setRowErrors = (
    update: ReadonlyMap<string, string>
      | ((current: ReadonlyMap<string, string>) => ReadonlyMap<string, string>),
  ) => setCatalogState((current) => ({
    ...current,
    rowErrors: typeof update === "function" ? update(current.rowErrors) : update,
  }));
  const roles = useMemo(() => rolesQ.data ?? [], [rolesQ.data]);
  const rolesById = useMemo(() => new Map(roles.map((role) => [role.id, role])), [roles]);

  const saveRole = useMutation({
    mutationFn: ({ rowId, draft }: { rowId: string; draft: WorkRoleFormState }) => {
      const payload = workRoleWritePayload(draft);
      if (rowId !== CREATE_WORK_ROLE_ROW_ID) {
        return fetchJson<WorkRole>("/api/v1/work_roles/" + encodeURIComponent(rowId), {
          method: "PATCH",
          body: payload,
        });
      }
      return fetchJson<WorkRole>("/api/v1/work_roles", {
        method: "POST",
        body: { ...payload, default_settings_json: {} },
      });
    },
    onSuccess: (_role, variables) => {
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: qk.workRoles() }),
        queryClient.invalidateQueries({ queryKey: qk.employees() }),
        queryClient.invalidateQueries({
          predicate: (query) => {
            const key = query.queryKey;
            return Array.isArray(key) && key.includes("employee") && key.includes("user_work_roles");
          },
        }),
      ]);
      if (variables.rowId === CREATE_WORK_ROLE_ROW_ID) {
        resetCreateRow();
        return;
      }
      setEditedDrafts((current) => clearMapValue(current, variables.rowId));
      setRowFieldErrors((current) => clearMapValue(current, variables.rowId));
      setRowErrors((current) => clearMapValue(current, variables.rowId));
    },
    onError: (error, variables) => {
      const nextFieldErrors = workRoleFieldErrors(error);
      const message = workRoleErrorMessage(error, nextFieldErrors);
      if (variables.rowId === CREATE_WORK_ROLE_ROW_ID) {
        setCatalogState({ createFieldErrors: nextFieldErrors, createError: message });
        return;
      }
      setRowFieldErrors((current) => setMapValue(current, variables.rowId, nextFieldErrors));
      setRowErrors((current) => setMapValue(current, variables.rowId, message));
    },
  });

  const deleteRole = useMutation({
    mutationFn: (roleId: string) =>
      fetchJson<void>("/api/v1/work_roles/" + encodeURIComponent(roleId), {
        method: "DELETE",
      }),
    onMutate: (roleId) => {
      setRowErrors((current) => clearMapValue(current, roleId));
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: qk.workRoles() }),
        queryClient.invalidateQueries({ queryKey: qk.employees() }),
        queryClient.invalidateQueries({
          predicate: (query) => {
            const key = query.queryKey;
            return Array.isArray(key) && key.includes("employee") && key.includes("user_work_roles");
          },
        }),
      ]);
    },
    onError: (error, roleId) => {
      setRowErrors((current) => setMapValue(current, roleId, workRoleErrorMessage(error, {})));
    },
  });

  const busy = saveRole.isPending || deleteRole.isPending;
  const rows = useMemo(
    () => roles.map((role): InlineTableRow<WorkRoleFormState> => {
      const draft = editedDrafts.get(role.id);
      const savingThisRow = saveRole.isPending && saveRole.variables?.rowId === role.id;
      const deletingThisRow = deleteRole.isPending && deleteRole.variables === role.id;
      return {
        id: role.id,
        label: role.name,
        draft: draft ?? draftFromWorkRole(role),
        committedDraft: draftFromWorkRole(role),
        editing: draft !== undefined,
        dirty: draft !== undefined,
        saving: savingThisRow || deletingThisRow,
        disabled: busy && !savingThisRow && !deletingThisRow,
        error: rowErrors.get(role.id) ? <span role="alert">{rowErrors.get(role.id)}</span> : undefined,
      };
    }),
    [busy, deleteRole.isPending, deleteRole.variables, editedDrafts, roles, rowErrors, saveRole.isPending, saveRole.variables],
  );
  const trailingCreateRow: InlineTableRow<WorkRoleFormState> = {
    id: CREATE_WORK_ROLE_ROW_ID,
    label: "New work role",
    draft: createDraft,
    editing: true,
    dirty: createDirty,
    isNew: true,
    saving: saveRole.isPending && saveRole.variables?.rowId === CREATE_WORK_ROLE_ROW_ID,
    disabled: busy && saveRole.variables?.rowId !== CREATE_WORK_ROLE_ROW_ID,
    error: createError ? <span role="alert">{createError}</span> : undefined,
  };
  const columns = useMemo(
    (): InlineTableColumn<WorkRoleFormState>[] => [
      {
        key: "icon",
        header: "Icon",
        width: { px: 112 },
        renderRead: ({ row }) => (
          <span className="work-role-row__mark" aria-hidden="true">
            {row.draft.icon_name ? (
              <AssetIcon name={row.draft.icon_name} size={18} className="work-role-row__icon" />
            ) : (
              workRoleInitials(row.draft.name)
            )}
          </span>
        ),
        renderEdit: ({ row, update, disabled }) => {
          const fieldErrors = fieldErrorsForRoleRow(row.id, rowFieldErrors, createFieldErrors);
          return (
            <InlineIconField
              label="Icon"
              value={row.draft.icon_name}
              onChange={(icon_name) => update({ icon_name })}
              disabled={disabled}
              allowEmpty
              error={fieldErrors.icon_name}
              errorId={workRoleFieldErrorId(row.id, "icon_name")}
            />
          );
        },
      },
      {
        key: "name",
        header: "Name",
        width: { flex: 1.2, min: 180 },
        renderRead: ({ row }) => <strong>{row.draft.name}</strong>,
        renderEdit: ({ row, update, disabled }) => {
          const fieldErrors = fieldErrorsForRoleRow(row.id, rowFieldErrors, createFieldErrors);
          return (
            <span className="work-role-inline-field">
              <InlineTextField
                value={row.draft.name}
                onChange={(name) => update({ name })}
                disabled={disabled}
                ariaLabel="Name"
                placeholder="e.g. Housekeeper"
                ariaInvalid={Boolean(fieldErrors.name)}
                ariaDescribedBy={fieldErrors.name ? workRoleFieldErrorId(row.id, "name") : undefined}
              />
              {fieldErrors.name ? (
                <span id={workRoleFieldErrorId(row.id, "name")} className="form-field-error">
                  {fieldErrors.name}
                </span>
              ) : null}
            </span>
          );
        },
      },
      {
        key: "key",
        header: "Key",
        width: { flex: 1, min: 160 },
        className: "mono",
        renderRead: ({ row }) => <span className="work-role-inline-key">{row.draft.key}</span>,
        renderEdit: ({ row, update, disabled }) => {
          const fieldErrors = fieldErrorsForRoleRow(row.id, rowFieldErrors, createFieldErrors);
          return (
            <span className="work-role-inline-field">
              <InlineTextField
                value={row.draft.key}
                onChange={(key) => update({ key })}
                disabled={disabled}
                ariaLabel="Key"
                placeholder="e.g. housekeeper"
                ariaInvalid={Boolean(fieldErrors.key)}
                ariaDescribedBy={fieldErrors.key ? workRoleFieldErrorId(row.id, "key") : undefined}
              />
              {fieldErrors.key ? (
                <span id={workRoleFieldErrorId(row.id, "key")} className="form-field-error">
                  {fieldErrors.key}
                </span>
              ) : null}
            </span>
          );
        },
      },
      {
        key: "description",
        header: "Description",
        width: { flex: 1.8, min: 240 },
        renderRead: ({ row }) => (
          <span className={row.draft.description_md ? "inline-table-form__read" : "inline-table-form__read inline-table-form__read--muted"}>
            {row.draft.description_md || "No description"}
          </span>
        ),
        renderEdit: ({ row, update, disabled }) => {
          const fieldErrors = fieldErrorsForRoleRow(row.id, rowFieldErrors, createFieldErrors);
          return (
            <span className="work-role-inline-field">
              <InlineTextField
                value={row.draft.description_md}
                onChange={(description_md) => update({ description_md })}
                disabled={disabled}
                ariaLabel="Description"
                placeholder="What this role covers in this workspace."
                ariaInvalid={Boolean(fieldErrors.description_md)}
                ariaDescribedBy={
                  fieldErrors.description_md
                    ? workRoleFieldErrorId(row.id, "description_md")
                    : undefined
                }
              />
              {fieldErrors.description_md ? (
                <span id={workRoleFieldErrorId(row.id, "description_md")} className="form-field-error">
                  {fieldErrors.description_md}
                </span>
              ) : null}
            </span>
          );
        },
      },
    ],
    [createFieldErrors, rowFieldErrors],
  );

  function resetCreateRow(): void {
    setCatalogState({
      createDraft: EMPTY_WORK_ROLE_FORM,
      createDirty: false,
      createFieldErrors: {},
      createError: null,
    });
  }

  function saveRow(rowId: string): void {
    const draft = rowId === CREATE_WORK_ROLE_ROW_ID ? createDraft : editedDrafts.get(rowId);
    if (!draft) return;
    const nextErrors = validateWorkRoleDraft(draft);
    if (Object.keys(nextErrors).length > 0) {
      if (rowId === CREATE_WORK_ROLE_ROW_ID) {
        setCatalogState({
          createFieldErrors: nextErrors,
          createError: "Fix the highlighted fields before saving.",
        });
        return;
      }
      setRowFieldErrors((current) => setMapValue(current, rowId, nextErrors));
      setRowErrors((current) => setMapValue(current, rowId, "Fix the highlighted fields before saving."));
      return;
    }
    if (rowId === CREATE_WORK_ROLE_ROW_ID) {
      setCatalogState({ createFieldErrors: {}, createError: null });
    } else {
      setRowFieldErrors((current) => clearMapValue(current, rowId));
      setRowErrors((current) => clearMapValue(current, rowId));
    }
    saveRole.mutate({ rowId, draft });
  }

  function cancelRow(rowId: string): void {
    if (rowId === CREATE_WORK_ROLE_ROW_ID) {
      resetCreateRow();
      return;
    }
    setEditedDrafts((current) => clearMapValue(current, rowId));
    setRowFieldErrors((current) => clearMapValue(current, rowId));
    setRowErrors((current) => clearMapValue(current, rowId));
  }

  function editRow(rowId: string): void {
    const role = rolesById.get(rowId);
    if (!role) return;
    setEditedDrafts((current) => setMapValue(current, rowId, draftFromWorkRole(role)));
    setRowFieldErrors((current) => clearMapValue(current, rowId));
    setRowErrors((current) => clearMapValue(current, rowId));
    saveRole.reset();
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
      </header>

      {rolesQ.isPending ? (
        <Loading />
      ) : rolesQ.isError ? (
        <p className="form-error" role="alert">
          Work roles could not be loaded.
        </p>
      ) : (
        <InlineTableForm
          compact
          ariaLabel="Work role catalog"
          className="work-role-catalog__table"
          columns={columns}
          rows={rows}
          saveMode="explicit"
          onDraftChange={(rowId, patch) => {
            if (rowId === CREATE_WORK_ROLE_ROW_ID) {
              setCatalogState((current) => ({
                ...current,
                createDraft: { ...current.createDraft, ...patch },
                createDirty: true,
                createFieldErrors: clearPatchedWorkRoleFieldErrors(current.createFieldErrors, patch),
                createError: null,
              }));
              return;
            }
            setEditedDrafts((current) => {
              const role = rolesById.get(rowId);
              if (!role) return current;
              const draft = current.get(rowId) ?? draftFromWorkRole(role);
              return setMapValue(current, rowId, { ...draft, ...patch });
            });
            setRowFieldErrors((current) => {
              const nextErrors = clearPatchedWorkRoleFieldErrors(current.get(rowId) ?? {}, patch);
              return Object.keys(nextErrors).length > 0
                ? setMapValue(current, rowId, nextErrors)
                : clearMapValue(current, rowId);
            });
            setRowErrors((current) => clearMapValue(current, rowId));
          }}
          onEdit={editRow}
          onSave={saveRow}
          onCancel={cancelRow}
          onDelete={(rowId) => deleteRole.mutate(rowId)}
          deleteActionLabel="Remove"
          trailingCreateRow={trailingCreateRow}
          getRowLabel={(row) => row.draft.name || row.label || "New work role"}
          renderDeleteConfirmation={({ label }) => ({
            title: "Remove work role?",
            confirmLabel: "Remove role",
            children: (
              <p>
                This soft-retires <strong>{label}</strong> and removes it from future employee assignment lists.
                Historical work remains attached to its original role record.
              </p>
            ),
          })}
        />
      )}
    </section>
  );
}

function InviteEmployeeAction() {
  const queryClient = useQueryClient();
  const meQ = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });
  const [inviteState, setInviteState] = usePatchReducer<InviteEmployeeState>({
    name: "",
    email: "",
    inviteDialogOpen: false,
    formError: null,
    sentInvite: null,
  });
  const { name, email, inviteDialogOpen, formError, sentInvite } = inviteState;

  const invite = useMutation({
    mutationFn: (payload: InviteEmployeeRequest) =>
      fetchJson<InviteEmployeeResponse>("/api/v1/users/invite", {
        method: "POST",
        body: payload,
      }),
    onSuccess: (result) => {
      setInviteState({ sentInvite: result, name: "", email: "", formError: null });
      void queryClient.invalidateQueries({ queryKey: qk.employees() });
      void queryClient.invalidateQueries({ queryKey: qk.users() });
    },
    onError: (error) => {
      setInviteState({ formError: inviteEmployeeErrorMessage(error) });
    },
  });

  const workspaceId = meQ.data?.current_workspace_id ?? "";

  function reset(): void {
    if (invite.isPending) return;
    setInviteState({ name: "", email: "", formError: null, sentInvite: null });
    invite.reset();
  }

  function submitInvite(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (invite.isPending || sentInvite) return;
    const trimmedName = name.trim();
    const trimmedEmail = email.trim();
    if (!trimmedName) {
      setInviteState({ formError: "Enter the employee's full name before sending the invite." });
      return;
    }
    if (!trimmedEmail) {
      setInviteState({ formError: "Enter the employee's email address before sending the invite." });
      return;
    }
    if (!workspaceId) {
      setInviteState({ formError: "Workspace context is still loading. Wait a moment and try again." });
      return;
    }
    setInviteState({ formError: null });
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
  }

  return (
    <>
      <button
        type="button"
        className="btn btn--moss"
        onClick={() => setInviteState({ inviteDialogOpen: true })}
      >
        + Invite employee
      </button>

      <FormModal
        open={inviteDialogOpen}
        title="Invite employee"
        titleId="invite-employee-title"
        eyebrow="Employee invite"
        subtitle="Send a click-to-accept invite for this workspace."
        formClassName="invite-employee-form"
        onClose={() => {
          setInviteState({ inviteDialogOpen: false });
          reset();
        }}
        onCancel={(event) => {
          if (invite.isPending) event.preventDefault();
        }}
        onSubmit={submitInvite}
        closeDisabled={invite.isPending}
        actions={
          <>
            <button
              type="button"
              className="btn btn--ghost"
              disabled={invite.isPending}
              onClick={() => setInviteState({ inviteDialogOpen: false })}
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
          </>
        }
      >
        <>
          {sentInvite ? (
            <>
              <output className="form-notice form-notice--success">
                Invite sent to {sentInvite.pending_email}. They will receive the acceptance link by email.
              </output>
              <p className="muted table__mono">Invite ID: {sentInvite.invite_id}</p>
            </>
          ) : (
            <>
              <FormField label="Full name" requirement="required" className="invite-employee-form__field sheet-form__field">
                <input
                  required
                  value={name}
                  onChange={(event) => {
                    setInviteState({ name: event.target.value, formError: null });
                  }}
                  placeholder="e.g. Riley Chen"
                 aria-label="Full name"/>
              </FormField>

              <FormField label="Email" requirement="required" className="invite-employee-form__field sheet-form__field">
                <input
                  type="email"
                  required
                  value={email}
                  onChange={(event) => {
                    setInviteState({ email: event.target.value, formError: null });
                  }}
                  placeholder="riley@example.com"
                 aria-label="Email"/>
              </FormField>
            </>
          )}

          {meQ.isError && !sentInvite && (
            <p className="form-error" role="alert">
              Workspace context could not load. Refresh and try again.
            </p>
          )}
          {formError && <p className="form-error" role="alert">{formError}</p>}
        </>
      </FormModal>
    </>
  );
}

function inviteEmployeeErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const fieldMessages = labeledFieldMessages(error, inviteFieldLabel);
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

function draftFromWorkRole(role: WorkRole): WorkRoleFormState {
  return {
    name: role.name,
    key: role.key,
    description_md: role.description_md,
    icon_name: role.icon_name,
  };
}

function workRoleWritePayload(draft: WorkRoleFormState): WorkRoleWriteRequest {
  return {
    name: draft.name.trim(),
    key: draft.key.trim(),
    description_md: draft.description_md.trim(),
    icon_name: workRoleIconNameForSubmit(draft.icon_name),
  };
}

function validateWorkRoleDraft(draft: WorkRoleFormState): Partial<Record<WorkRoleField, string>> {
  const errors: Partial<Record<WorkRoleField, string>> = {};
  if (!draft.name.trim()) errors.name = "Enter a role name.";
  if (!draft.key.trim()) errors.key = "Enter a role key.";
  return errors;
}

function fieldErrorsForRoleRow(
  rowId: string,
  rowFieldErrors: ReadonlyMap<string, Partial<Record<WorkRoleField, string>>>,
  createFieldErrors: Partial<Record<WorkRoleField, string>>,
): Partial<Record<WorkRoleField, string>> {
  return rowId === CREATE_WORK_ROLE_ROW_ID ? createFieldErrors : rowFieldErrors.get(rowId) ?? {};
}

function clearPatchedWorkRoleFieldErrors(
  fieldErrors: Partial<Record<WorkRoleField, string>>,
  patch: Partial<WorkRoleFormState>,
): Partial<Record<WorkRoleField, string>> {
  const next = { ...fieldErrors };
  for (const field of Object.keys(patch) as WorkRoleField[]) {
    delete next[field];
  }
  return next;
}

function workRoleFieldErrorId(rowId: string, field: WorkRoleField): string {
  return fieldErrorId("work-role", rowId, field);
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
  return fieldErrorsByLoc(error, workRoleFieldFromLoc);
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
