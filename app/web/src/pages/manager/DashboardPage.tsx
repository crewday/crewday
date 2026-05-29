import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import {
  CalendarOff,
  CircleAlert,
  ClipboardList,
  Search,
  ShieldCheck,
  Users,
  X,
} from "lucide-react";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { workspaceRouteForPathname } from "@/lib/workspaceRoutes";
import { useDecideMutation } from "@/lib/useDecideMutation";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import DeskPage from "@/components/DeskPage";
import DateTime from "@/components/DateTime";
import FormField from "@/components/FormField";
import FormModal from "@/components/FormModal";
import { Avatar, Chip, EmptyState, Loading, Panel, StatCard } from "@/components/common";
import {
  APPROVAL_RISK_TONE,
  ISSUE_SEVERITY_TONE,
  ISSUE_STATUS_TONE,
  TASK_STATUS_TONE,
} from "@/lib/tones";
import type { DashboardPayload as Dashboard, Me } from "@/types/api";
import type {
  BroadcastAudienceGroupPayload,
  BroadcastRecipientPayload,
  BroadcastRecipientsResponse,
  BroadcastSendRequest,
  BroadcastSendResponse,
} from "@/types/messaging";

interface BroadcastAudienceOption {
  token: string;
  label: string;
  detail: string | null;
  kind: "group" | "person";
  count: number | null;
  userId: string | null;
  recipientUserIds: string[] | null;
}

function audienceGroupDetail(group: BroadcastAudienceGroupPayload): string {
  const typeLabel =
    group.kind === "everyone"
      ? "Workspace group"
      : group.kind === "workspace_role"
        ? "Workspace role"
        : "Work role";
  const count = group.resolved_recipient_count;
  return `${typeLabel} · ${count} recipient${count === 1 ? "" : "s"}`;
}

function broadcastAudienceOptions(
  groups: BroadcastAudienceGroupPayload[] = [],
  people: BroadcastRecipientPayload[] = [],
): BroadcastAudienceOption[] {
  return [
    ...groups.map((group) => ({
      token: group.token,
      label: group.label,
      detail: audienceGroupDetail(group),
      kind: "group" as const,
      count: group.resolved_recipient_count,
      userId: null,
      recipientUserIds: group.recipient_user_ids,
    })),
    ...people.map((person) => ({
      token: person.token,
      label: person.display_name,
      detail: person.email,
      kind: "person" as const,
      count: 1,
      userId: person.user_id,
      recipientUserIds: [person.user_id],
    })),
  ];
}

function resolvedRecipientCount(
  selectedTokens: string[],
  options: BroadcastAudienceOption[],
): number {
  const selected = new Set(selectedTokens);
  const selectedOptions = options.filter((option) => selected.has(option.token));
  const everyone = selectedOptions.find((option) => option.kind === "group" && option.token === "group:everyone");
  if (everyone?.count !== null && everyone?.count !== undefined) return everyone.count;

  const resolvedIds = new Set<string>();
  let hasCompleteMembership = selectedOptions.length > 0;
  let fallbackCount = 0;
  for (const option of selectedOptions) {
    if (option.recipientUserIds) {
      option.recipientUserIds.forEach((id) => resolvedIds.add(id));
      continue;
    }
    hasCompleteMembership = false;
    fallbackCount += option.count ?? 0;
  }
  return hasCompleteMembership ? resolvedIds.size : fallbackCount + resolvedIds.size;
}

function BroadcastRecipientPicker({
  groups,
  people,
  selectedTokens,
  onChange,
  loading,
  resolvedCount,
}: {
  groups: BroadcastAudienceGroupPayload[];
  people: BroadcastRecipientPayload[];
  selectedTokens: string[];
  onChange: (tokens: string[]) => void;
  loading: boolean;
  resolvedCount: number;
}) {
  const inputId = useId();
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const options = useMemo(() => broadcastAudienceOptions(groups, people), [groups, people]);
  const selected = useMemo(() => new Set(selectedTokens), [selectedTokens]);
  const selectedOptions = options.filter((option) => selected.has(option.token));
  const normalizedQuery = query.trim().toLowerCase();
  const filteredOptions = options.filter((option) => {
    if (selected.has(option.token)) return false;
    if (!normalizedQuery) return true;
    return (
      option.label.toLowerCase().includes(normalizedQuery) ||
      (option.detail?.toLowerCase().includes(normalizedQuery) ?? false)
    );
  });
  const activeOption = filteredOptions[activeIndex] ?? null;

  useEffect(() => {
    setActiveIndex(0);
  }, [normalizedQuery, selectedTokens.length]);

  useEffect(() => {
    if (activeIndex >= filteredOptions.length) {
      setActiveIndex(Math.max(filteredOptions.length - 1, 0));
    }
  }, [activeIndex, filteredOptions.length]);

  const selectOption = (option: BroadcastAudienceOption) => {
    onChange([...selectedTokens, option.token]);
    setQuery("");
    setOpen(true);
    inputRef.current?.focus();
  };

  const removeToken = (token: string) => {
    onChange(selectedTokens.filter((selectedToken) => selectedToken !== token));
    inputRef.current?.focus();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      if (!open) {
        setActiveIndex(0);
        return;
      }
      setActiveIndex((index) => Math.min(index + 1, Math.max(filteredOptions.length - 1, 0)));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      if (!open) {
        setActiveIndex(Math.max(filteredOptions.length - 1, 0));
        return;
      }
      setActiveIndex((index) => Math.max(index - 1, 0));
      return;
    }
    if (event.key === "Enter" && open && activeOption) {
      event.preventDefault();
      selectOption(activeOption);
      return;
    }
    if (event.key === "Backspace" && query === "" && selectedOptions.length > 0) {
      event.preventDefault();
      removeToken(selectedOptions[selectedOptions.length - 1]!.token);
      return;
    }
    if (event.key === "Escape") {
      setOpen(false);
    }
  };

  return (
    <div
      className="broadcast-recipient-picker"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
    >
      <div
        className="broadcast-recipient-picker__control"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) inputRef.current?.focus();
        }}
      >
        <Search className="broadcast-recipient-picker__search" size={16} aria-hidden="true" />
        {selectedOptions.map((option) => (
          <span
            key={option.token}
            className={
              "broadcast-recipient-picker__chip" +
              (option.kind === "group" ? " broadcast-recipient-picker__chip--group" : "")
            }
          >
            {option.label}
            <button
              type="button"
              className="broadcast-recipient-picker__remove"
              onClick={() => removeToken(option.token)}
              aria-label={`Remove ${option.label}`}
            >
              <X size={13} aria-hidden="true" />
            </button>
          </span>
        ))}
        <input
          id={inputId}
          ref={inputRef}
          role="combobox"
          aria-label="Recipients"
          aria-autocomplete="list"
          aria-expanded={open}
          aria-controls={listId}
          aria-activedescendant={open && activeOption ? `${listId}-${activeOption.token}` : undefined}
          aria-describedby={`${inputId}-summary`}
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={handleKeyDown}
          placeholder={selectedOptions.length === 0 ? "Search groups or people" : ""}
          className="broadcast-recipient-picker__input"
        />
      </div>
      <div id={`${inputId}-summary`} className="broadcast-recipient-picker__summary" aria-live="polite">
        Server-resolved recipient count: <strong>{resolvedCount}</strong>
      </div>
      {open && (
        <div className="broadcast-recipient-picker__popover">
          <ul id={listId} className="broadcast-recipient-picker__list" role="listbox">
            {loading ? (
              <li className="broadcast-recipient-picker__empty">Loading recipients...</li>
            ) : filteredOptions.length === 0 ? (
              <li className="broadcast-recipient-picker__empty">No matching audiences</li>
            ) : (
              filteredOptions.map((option, index) => (
                <li
                  id={`${listId}-${option.token}`}
                  key={option.token}
                  role="option"
                  aria-selected={index === activeIndex}
                  className={
                    "broadcast-recipient-picker__option" +
                    (index === activeIndex ? " broadcast-recipient-picker__option--active" : "")
                  }
                  onMouseDown={(event) => {
                    event.preventDefault();
                    selectOption(option);
                  }}
                  onMouseEnter={() => setActiveIndex(index)}
                >
                  <span className="broadcast-recipient-picker__option-main">
                    <span className="broadcast-recipient-picker__option-label">{option.label}</span>
                    {option.detail ? (
                      <span className="broadcast-recipient-picker__option-detail">{option.detail}</span>
                    ) : null}
                  </span>
                  <span className="broadcast-recipient-picker__option-kind">
                    {option.kind === "group" ? "Group" : "Person"}
                  </span>
                </li>
              ))
            )}
          </ul>
        </div>
      )}
    </div>
  );
}

export default function DashboardPage() {
  // code-health: ignore[ccn nloc] Dashboard route keeps broadcast, approvals, and task summary composition in one manager landing page.
  const { pathname } = useLocation();
  const d = useQuery({ queryKey: qk.dashboard(), queryFn: () => fetchJson<Dashboard>("/api/v1/dashboard") });
  const me = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const qc = useQueryClient();
  const [broadcastOpen, setBroadcastOpen] = useState(false);
  const [selectedAudienceTokens, setSelectedAudienceTokens] = useState<string[]>(["group:everyone"]);
  const [broadcastSubject, setBroadcastSubject] = useState("");
  const [broadcastBody, setBroadcastBody] = useState("");
  const [broadcastNotice, setBroadcastNotice] = useState<string | null>(null);

  const decideApproval = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "reject" }) =>
      fetchJson("/api/v1/approvals/" + id + "/" + decision, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.dashboard() }),
  });
  const decideLeave = useDecideMutation<Dashboard, "approve" | "reject">({
    queryKey: qk.dashboard(),
    endpoint: (id, decision) => "/api/v1/leaves/" + id + "/" + decision,
    applyOptimistic: (prev, id) => ({
      ...prev,
      pending_leaves: prev.pending_leaves.filter((lv) => lv.id !== id),
    }),
    alsoInvalidate: [qk.leaves()],
  });
  const broadcastRecipients = useQuery({
    queryKey: qk.broadcastRecipients(),
    queryFn: () =>
      fetchJson<BroadcastRecipientsResponse>("/api/v1/messaging/broadcast/recipients"),
    enabled: broadcastOpen,
  });
  const broadcastOptions = useMemo(
    () => broadcastAudienceOptions(broadcastRecipients.data?.groups, broadcastRecipients.data?.people),
    [broadcastRecipients.data?.groups, broadcastRecipients.data?.people],
  );
  const recipientCount = resolvedRecipientCount(selectedAudienceTokens, broadcastOptions);
  const sendBroadcast = useMutation({
    mutationFn: () =>
      fetchJson<BroadcastSendResponse>("/api/v1/messaging/broadcast", {
        method: "POST",
        body: {
          audience_tokens: selectedAudienceTokens,
          confirmed_recipient_count: recipientCount,
          subject: broadcastSubject.trim(),
          body_md: broadcastBody.trim(),
        } satisfies BroadcastSendRequest,
      }),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: qk.dashboard() });
      if (result.status === "pending_approval") {
        setBroadcastNotice(`Queued for approval before sending to ${result.recipient_count} recipients.`);
        return;
      }
      setBroadcastOpen(false);
    },
  });

  const resetBroadcast = () => {
    setSelectedAudienceTokens(["group:everyone"]);
    setBroadcastSubject("");
    setBroadcastBody("");
    setBroadcastNotice(null);
    setBroadcastOpen(false);
    sendBroadcast.reset();
  };

  const openBroadcast = () => {
    setBroadcastNotice(null);
    sendBroadcast.reset();
    setBroadcastOpen(true);
  };

  if (d.isPending || me.isPending) return <DeskPage title="Dashboard"><Loading /></DeskPage>;
  if (!d.data || !me.data) return <DeskPage title="Dashboard">Failed to load.</DeskPage>;

  const {
    on_booking, by_status, pending_approvals, pending_leaves, open_issues, stays_today,
    properties, employees,
  } = d.data;
  const propsById = new Map(properties.map((p) => [p.id, p]));
  const empsById = new Map(employees.map((e) => [e.id, e]));
  const totalToday =
    by_status.completed.length + by_status.in_progress.length + by_status.pending.length;
  const todayTasks = [...by_status.in_progress, ...by_status.pending, ...by_status.completed];
  const firstName = me.data.manager_name.split(" ")[0];
  const todayLong = new Date(me.data.today).toLocaleDateString("en-GB", {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
  });

  return (
    <DeskPage
      title="Dashboard"
      sub={`Good morning, ${firstName} · ${todayLong} · ${properties.length} properties · 5 staff · ${totalToday} tasks today`}
      actions={
        <button type="button" className="btn btn--moss" onClick={openBroadcast}>
          Broadcast message
        </button>
      }
    >
      <FormModal
        open={broadcastOpen}
        title="Broadcast message"
        eyebrow="Staff message"
        subtitle={
          <>
            {recipientCount} recipient{recipientCount === 1 ? "" : "s"}
          </>
        }
        formClassName="broadcast-message-form"
        onClose={resetBroadcast}
        onSubmit={(e) => {
          e.preventDefault();
          if (!broadcastSubject.trim() || !broadcastBody.trim() || recipientCount < 1 || selectedAudienceTokens.length < 1) return;
          sendBroadcast.mutate();
        }}
        actions={
          <>
            <button type="button" className="btn btn--ghost" onClick={() => setBroadcastOpen(false)}>
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn--moss"
              disabled={
                sendBroadcast.isPending ||
                broadcastNotice !== null ||
                !broadcastSubject.trim() ||
                !broadcastBody.trim() ||
                recipientCount < 1 ||
                selectedAudienceTokens.length < 1
              }
            >
              {sendBroadcast.isPending
                ? "Sending..."
                : broadcastNotice !== null
                  ? "Queued"
                  : "Send"}
            </button>
          </>
        }
      >
          {broadcastNotice && (
            <div className="form-notice form-notice--success" role="status">
              {broadcastNotice}
            </div>
          )}
          {sendBroadcast.isError && (
            <div className="form-notice form-notice--error" role="alert">
              Broadcast could not be sent.
            </div>
          )}
          {broadcastRecipients.isError && (
            <div className="form-notice form-notice--error" role="alert">
              Recipients could not be loaded.
            </div>
          )}

          <FormField label="Recipients" requirement="required" className="broadcast-message-form__field sheet-form__field">
            <BroadcastRecipientPicker
              groups={broadcastRecipients.data?.groups ?? []}
              people={broadcastRecipients.data?.people ?? []}
              selectedTokens={selectedAudienceTokens}
              onChange={setSelectedAudienceTokens}
              loading={broadcastRecipients.isPending}
              resolvedCount={recipientCount}
            />
          </FormField>

          <FormField label="Subject" requirement="required" className="broadcast-message-form__field sheet-form__field">
            <input
              required
              maxLength={160}
              value={broadcastSubject}
              onChange={(e) => setBroadcastSubject(e.target.value)}
              placeholder="e.g. Storm watch"
            />
          </FormField>

          <FormField label="Body" requirement="required" className="broadcast-message-form__field sheet-form__field">
            <AutoGrowTextarea
              required
              rows={6}
              maxLength={20000}
              value={broadcastBody}
              onChange={(e) => setBroadcastBody(e.target.value)}
              placeholder="Write the message staff will receive."
            />
          </FormField>
      </FormModal>

      <section className="grid grid--stats">
        <StatCard
          label="Tasks today"
          value={<>{by_status.completed.length}<span className="stat-card__divider">/</span>{totalToday}</>}
          sub="completed"
        />
        <StatCard label="Working now" value={on_booking.length} sub="of 5 staff" />
        <StatCard
          label="Approvals"
          value={pending_approvals.length}
          sub="agent actions awaiting review"
          warn={pending_approvals.length > 0}
        />
        <StatCard label="Stays in house" value={stays_today.length} sub={`across ${properties.length} properties`} />
      </section>

      <section className="grid grid--split">
        <Panel title="Today's tasks" right={<Link className="link" to={workspaceRouteForPathname(pathname, "/properties")}>By property →</Link>}>
          {todayTasks.length === 0 ? (
            <EmptyState
              icon={ClipboardList}
              title="No tasks scheduled"
              copy="Today's task list will appear here once work is assigned."
              variant="quiet"
            />
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Time</th><th>Task</th><th>Property</th><th>Assignee</th><th>Status</th>
                </tr>
              </thead>
              <tbody>
                {todayTasks.map((t) => {
                  const prop = propsById.get(t.property_id);
                  const emp = empsById.get(t.assignee_id);
                  return (
                    <tr key={t.id}>
                      <td><DateTime value={t.scheduled_start} showTime className="mono" /></td>
                      <td><strong>{t.title}</strong><div className="table__sub">{t.area}</div></td>
                      <td>{prop && <Chip tone={prop.color} size="sm">{prop.name}</Chip>}</td>
                      <td>
                        {emp && <><Avatar url={emp.avatar_url} initials={emp.avatar_initials} size="xs" alt={emp.name} /> {emp.name.split(" ")[0]}</>}
                      </td>
                      <td><Chip tone={TASK_STATUS_TONE[t.status]} size="sm">{t.status.replace("_", " ")}</Chip></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel title="Agent approvals" right={<Link className="link" to={workspaceRouteForPathname(pathname, "/approvals")}>All →</Link>}>
          {pending_approvals.length === 0 ? (
            <EmptyState
              icon={ShieldCheck}
              title="No agent approvals waiting"
              copy="Agent requests that need a manager decision will appear here."
              variant="quiet"
            />
          ) : (
            <ul className="approval-list">
              {pending_approvals.map((a) => (
                <li key={a.id} className={"approval approval--" + a.risk}>
                  <div className="approval__head">
                    <Chip tone="ghost" size="sm">{a.agent}</Chip>
                    <Chip tone={APPROVAL_RISK_TONE[a.risk]} size="sm">{a.risk} risk</Chip>
                  </div>
                  <div className="approval__title"><strong>{a.action}</strong> · {a.target}</div>
                  <div className="approval__reason">{a.reason}</div>
                  <div className="approval__actions">
                    <button
                      className="btn btn--moss btn--sm"
                      type="button"
                      onClick={() => decideApproval.mutate({ id: a.id, decision: "approve" })}
                    >
                      Approve
                    </button>
                    <button
                      className="btn btn--ghost btn--sm"
                      type="button"
                      onClick={() => decideApproval.mutate({ id: a.id, decision: "reject" })}
                    >
                      Reject
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </section>

      <section className="grid grid--split">
        <Panel title="Open issues" right={<span className="muted">{open_issues.length}</span>}>
          {open_issues.length === 0 ? (
            <EmptyState
              icon={CircleAlert}
              title="No open issues"
              copy="Guest, staff, and property issues will show here when they need attention."
              variant="quiet"
            />
          ) : (
            <ul className="issue-list">
              {open_issues.map((i) => {
                const reporter = empsById.get(i.reported_by);
                const prop = propsById.get(i.property_id);
                return (
                  <li key={i.id} className="issue-row">
                    <div>
                      <strong>{i.title}</strong>
                      <div className="table__sub">
                        {reporter?.name.split(" ")[0]} · {prop?.name} · {i.area}
                      </div>
                    </div>
                    <Chip tone={ISSUE_SEVERITY_TONE[i.severity]} size="sm">{i.severity}</Chip>
                    <Chip tone={ISSUE_STATUS_TONE[i.status]} size="sm">{i.status.replace("_", " ")}</Chip>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>

        <Panel title="Pending leaves" right={<Link className="link" to={workspaceRouteForPathname(pathname, "/leaves")}>All →</Link>}>
          {pending_leaves.length === 0 ? (
            <EmptyState
              icon={CalendarOff}
              title="No pending leave requests"
              copy="Submitted leave requests will appear here for approval."
              variant="quiet"
            />
          ) : (
            <ul className="task-list task-list--desk">
              {pending_leaves.map((lv) => {
                const emp = empsById.get(lv.employee_id);
                const range =
                  new Date(lv.starts_on).toLocaleDateString("en-GB", { day: "2-digit", month: "short" }) +
                  " → " +
                  new Date(lv.ends_on).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
                return (
                  <li key={lv.id} className="task-row">
                    <span className="task-row__time mono">{range}</span>
                    <span className="task-row__title">
                      <strong>{emp?.name}</strong>
                      <span className="task-row__area">{lv.category} · {lv.note}</span>
                    </span>
                    <span>
                      <button
                        className="btn btn--sm btn--moss"
                        type="button"
                        onClick={() => decideLeave.mutate({ id: lv.id, decision: "approve" })}
                      >
                        Approve
                      </button>{" "}
                      <button
                        className="btn btn--sm btn--ghost"
                        type="button"
                        onClick={() => decideLeave.mutate({ id: lv.id, decision: "reject" })}
                      >
                        Reject
                      </button>
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>
      </section>
    </DeskPage>
  );
}
