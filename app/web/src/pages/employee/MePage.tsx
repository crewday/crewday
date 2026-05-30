import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil } from "lucide-react";
import {
  PasskeyCancelledError,
  PasskeyTimeoutError,
  PasskeyTransientError,
  PasskeyUnsupportedError,
} from "@/auth";
import { runAuthenticatedPasskeyRegisterCeremony } from "@/auth/passkey-register";
import { setUnauthenticated } from "@/auth/authStore";
import { workspaceSlug } from "@/auth/roleLanding";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { fmtDate } from "@/lib/dates";
import { workspaceRoute, workspaceRouteForPathname } from "@/lib/workspaceRoutes";
import { useWorkspace } from "@/context/WorkspaceContext";
import { Chip, Loading } from "@/components/common";
import AgentApprovalModePanel from "@/components/AgentApprovalModePanel";
import AgentPreferencesPanel from "@/components/AgentPreferencesPanel";
import AppearancePanel from "@/components/AppearancePanel";
import AvatarEditor from "@/components/AvatarEditor";
import ChatChannelsMeCard from "@/components/ChatChannelsMeCard";
import PersonalTokensPanel from "@/components/PersonalTokensPanel";
import WorkspacePickList from "@/components/WorkspacePickList";
import type { Me } from "@/types/api";

const LANG_LABEL: Record<string, string> = {
  fr: "Français",
  en: "English",
  es: "Español",
  pt: "Português",
};

type PasskeyRegisterState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "error"; message: string };

export default function MePage() {
  // code-health: ignore[ccn nloc] Profile page is declarative settings composition with passkey, workspace switch, and preferences kept in one account route.
  const [editorOpen, setEditorOpen] = useState(false);
  const [passkeyRegister, setPasskeyRegister] = useState<PasskeyRegisterState>({ kind: "idle" });
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { workspaceId, setWorkspaceId } = useWorkspace();

  const me = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });

  if (me.isPending) {
    return (
      <section className="me-page"><Loading /></section>
    );
  }
  if (me.isError || !me.data) {
    return (
      <section className="me-page"><p className="muted">Failed to load.</p></section>
    );
  }

  const { employee } = me.data;
  const langLabel = LANG_LABEL[employee.language] ?? employee.language;
  const registerPending = passkeyRegister.kind === "pending";
  const availableWorkspaces = me.data.available_workspaces ?? [];

  async function onRegisterPasskey(): Promise<void> {
    if (registerPending) return;
    setPasskeyRegister({ kind: "pending" });
    try {
      await runAuthenticatedPasskeyRegisterCeremony();
      queryClient.clear();
      setUnauthenticated();
      navigate("/login", {
        replace: true,
        state: {
          notice: "Passkey registered. For your security, all sessions were signed out. Sign in again to continue.",
        },
      });
    } catch (err) {
      setPasskeyRegister({ kind: "error", message: messageForPasskeyRegisterError(err) });
    }
  }

  return (
    <section className="me-page">
      {availableWorkspaces.length > 1 && (
        <section className="panel me-workspace-switch" aria-labelledby="me-workspace-switch-title">
          <header className="panel__head">
            <h2 id="me-workspace-switch-title">Workspaces</h2>
          </header>
          <WorkspacePickList
            workspaces={availableWorkspaces}
            activeWorkspaceSlug={workspaceId}
            label="Switch workspace"
            onPick={(workspace) => {
              const slug = workspaceSlug(workspace);
              setWorkspaceId(slug);
              navigate(workspaceRoute(slug, "/me"));
            }}
          />
        </section>
      )}

      <section className="panel">
        <div className="profile-card">
          <button
            type="button"
            className="avatar-trigger"
            onClick={() => setEditorOpen(true)}
            aria-label="Change profile photo"
          >
            <span className="avatar avatar--xl">
              {employee.avatar_url
                ? <img className="avatar__img" src={employee.avatar_url} alt={employee.name} />
                : employee.avatar_initials}
            </span>
            <span className="avatar-trigger__edit" aria-hidden="true">
              <Pencil size={12} strokeWidth={2.5} />
            </span>
          </button>
          <div>
            <h2 className="profile-card__name">{employee.name}</h2>
            <p className="profile-card__intro">
              Your personal profile and preferences. Workspace-wide defaults are managed
              separately.
            </p>
            <div className="profile-card__roles">
              {employee.roles.map((r) => (
                <Chip key={r} tone="ghost" size="sm">{r}</Chip>
              ))}
            </div>
            <div className="profile-card__meta">
              Started{" "}
              {fmtDate(employee.started_on, "en-GB", {
                day: "2-digit",
                month: "short",
                year: "numeric",
              })}{" "}
              · {employee.phone}
            </div>
          </div>
        </div>
      </section>

      <section className="panel">
        <header className="panel__head"><h2>Email</h2></header>
        <div className="stack-row">
          <div>
            <strong>{employee.email}</strong>
            <div className="stack-row__sub">
              Used for magic links (invites, lost-device recovery) and digests. Changing it sends
              a confirmation link to the new address and a 72-hour revert link to this one.
            </div>
          </div>
          <button type="button" className="btn btn--ghost btn--sm">Change</button>
        </div>
      </section>

      <ChatChannelsMeCard me={me.data} />

      <section className="panel">
        <header className="panel__head"><h2>Language</h2></header>
        <div className="stack-row">
          <div>
            <strong>{langLabel}</strong>
            <div className="stack-row__sub">
              Used for the agent, digests and reminders.
            </div>
          </div>
          <button type="button" className="btn btn--ghost btn--sm">Change</button>
        </div>
      </section>

      <AppearancePanel />

      <AgentApprovalModePanel />

      <AgentPreferencesPanel
        scope="user"
        title="My agent preferences"
        subtitle="Private to you. Written in plain language; sent to your chat agent on every turn."
      />

      <PersonalTokensPanel />

      <section className="panel">
        <header className="panel__head">
          <div className="panel__head-stack">
            <h2>Passkeys</h2>
            <p className="panel__sub">
              Devices you've registered to sign in. Remove any you no longer trust,
              re-enrolling on a new device revokes the rest automatically.
            </p>
          </div>
          <button
            className="btn btn--moss btn--sm"
            type="button"
            onClick={() => { void onRegisterPasskey(); }}
            disabled={registerPending}
            aria-busy={registerPending}
          >
            {registerPending ? "Registering…" : "+ Register another device"}
          </button>
        </header>
        {passkeyRegister.kind === "error" && (
          <p className="muted" role="alert">{passkeyRegister.message}</p>
        )}
        <ul className="entry-cards">
          <li className="entry-card">
            <div className="entry-card__head">
              <span className="entry-card__name">iPhone 14 · Face ID</span>
              <Chip tone="moss" size="sm">active</Chip>
              <div className="entry-card__action">
                <button type="button" className="btn btn--sm btn--ghost">Remove</button>
              </div>
            </div>
            <div className="entry-card__meta">
              <span>
                <span className="entry-card__meta-label">Added</span>
                12 Mar 2025
              </span>
              <span>
                <span className="entry-card__meta-label">Last used</span>
                today
              </span>
            </div>
          </li>
        </ul>
      </section>

      <section className="panel">
        <header className="panel__head"><h2>History</h2></header>
        <Link to={workspaceRouteForPathname(pathname, "/history")} className="stack-row">
          <div>
            <strong>Past tasks, chats, expenses, leaves</strong>
            <div className="stack-row__sub">Browse what's been wrapped up →</div>
          </div>
          <span className="btn btn--ghost btn--sm">View</span>
        </Link>
      </section>

      <AvatarEditor
        open={editorOpen}
        onClose={() => setEditorOpen(false)}
        currentUrl={employee.avatar_url}
        userName={employee.name}
      />
    </section>
  );
}

function messageForPasskeyRegisterError(err: unknown): string {
  if (err instanceof PasskeyCancelledError) {
    return "Passkey prompt closed. Click “+ Register another device” to try again.";
  }
  if (err instanceof PasskeyTimeoutError) {
    return "Your authenticator didn't respond in time. Click “+ Register another device” to try again.";
  }
  if (err instanceof PasskeyTransientError) {
    return "Couldn't reach your authenticator. Wait a moment and try again.";
  }
  if (err instanceof PasskeyUnsupportedError) {
    if (err.kind === "invalid_state") {
      return "This device already has a passkey registered. Try another device.";
    }
    if (err.kind === "security") {
      return "This browser blocked passkey registration for this site. Use the secure app URL and try again.";
    }
    return "This browser or device can't register passkeys. Try another browser or device.";
  }

  const apiError = apiErrorStatus(err);
  if (apiError?.status === 422 && apiError.error === "too_many_passkeys") {
    return "You already have the maximum 5 passkeys. Remove one before registering another device.";
  }
  if (apiError?.status === 429) {
    return "Too many register attempts. Wait a minute and try again.";
  }
  if (apiError?.detail) return apiError.detail;
  return "We couldn't finish registering your passkey. Try again in a moment.";
}

function apiErrorStatus(err: unknown): { status: number; error: string | null; detail: string | null } | null {
  if (!err || typeof err !== "object") return null;
  const record = err as Record<string, unknown>;
  if (typeof record.status !== "number") return null;
  const problem = record.problem;
  const problemRecord =
    problem && typeof problem === "object" && !Array.isArray(problem)
      ? problem as Record<string, unknown>
      : null;
  return {
    status: record.status,
    error: typeof problemRecord?.error === "string" ? problemRecord.error : null,
    detail: typeof problemRecord?.detail === "string" ? problemRecord.detail : null,
  };
}
