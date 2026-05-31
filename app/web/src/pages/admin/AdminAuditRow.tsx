import DateTime from "@/components/DateTime";
import { Chip } from "@/components/common";
import type { AuditEntry } from "@/types/api";

const ACTOR_TONE: Record<AuditEntry["actor_kind"], "moss" | "sky" | "ghost"> = {
  user: "moss",
  agent: "sky",
  system: "ghost",
};

const ACTOR_LABEL: Record<AuditEntry["actor_kind"], string> = {
  user: "User",
  agent: "Agent",
  system: "System",
};

const ENTITY_LABELS: Record<string, string> = {
  admin_agent_action: "Admin agent action",
  admin_agent_message: "Admin agent message",
  agent_doc: "Agent document",
  deployment: "Deployment",
  deployment_owner: "Deployment owner",
  deployment_setting: "Deployment setting",
  llm_prompt_template: "Prompt template",
  role_grant: "Role grant",
  signup_attempt: "Signup attempt",
  workspace: "Workspace",
};

function readableEntityKind(kind: string): string {
  if (!kind) return "Audit target";
  const mapped = ENTITY_LABELS[kind];
  if (mapped) return mapped;
  return kind
    .split(/[_:-]+/)
    .filter(Boolean)
    .map((part, index) =>
      index === 0
        ? part.charAt(0).toUpperCase() + part.slice(1)
        : part.toLowerCase(),
    )
    .join(" ");
}

function readableRole(role: AuditEntry["actor_grant_role"]): string | null {
  if (!role) return null;
  return role.charAt(0).toUpperCase() + role.slice(1);
}

function AdminAuditActorCell({ row }: { row: AuditEntry }) {
  const role = readableRole(row.actor_grant_role);
  const actorId = row.actor_id ?? row.actor;
  return (
    <div className="admin-audit-cell admin-audit-actor">
      <div className="admin-audit-cell__primary">
        <Chip tone={ACTOR_TONE[row.actor_kind]} size="sm">
          {ACTOR_LABEL[row.actor_kind]}
        </Chip>
        {role ? <Chip tone="ghost" size="sm">{role}</Chip> : null}
        {row.actor_was_owner_member ? (
          <Chip tone="sand" size="sm">Owner</Chip>
        ) : null}
      </div>
      {actorId ? (
        <div className="admin-audit-cell__secondary admin-audit-cell__secondary--mono">
          {actorId}
        </div>
      ) : null}
    </div>
  );
}

function AdminAuditActionCell({ row }: { row: AuditEntry }) {
  return (
    <div className="admin-audit-cell admin-audit-action-cell">
      <code className="inline-code admin-audit-action">{row.action}</code>
      {row.actor_action_key ? (
        <div className="admin-audit-cell__secondary">via {row.actor_action_key}</div>
      ) : null}
    </div>
  );
}

function AdminAuditTargetCell({ row }: { row: AuditEntry }) {
  const kind = row.entity_kind ?? "";
  const id = row.entity_id ?? row.target;
  const target = row.target || id;
  return (
    <div className="admin-audit-cell admin-audit-target">
      <div className="admin-audit-cell__primary">{readableEntityKind(kind)}</div>
      <div
        className="admin-audit-cell__secondary admin-audit-cell__secondary--mono"
        title={target}
        aria-label={target}
      >
        {id}
      </div>
    </div>
  );
}

export function AdminAuditRow({
  row,
  showVia = false,
}: {
  row: AuditEntry;
  showVia?: boolean;
}) {
  return (
    <tr>
      <td data-label="When"><DateTime value={row.at} showTime className="mono" /></td>
      <td data-label="Actor"><AdminAuditActorCell row={row} /></td>
      <td data-label="Action"><AdminAuditActionCell row={row} /></td>
      <td data-label="Target"><AdminAuditTargetCell row={row} /></td>
      {showVia ? <td data-label="Via" className="muted admin-audit-via">{row.via}</td> : null}
      <td data-label="Reason" className="muted admin-audit-reason">{row.reason ?? ""}</td>
    </tr>
  );
}
