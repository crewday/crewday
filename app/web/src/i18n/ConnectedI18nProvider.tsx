import { type ReactNode } from "react";
import { useAuth } from "@/auth";
import type { AuthMe } from "@/auth/types";
import { useWorkspace } from "@/context/WorkspaceContext";
import { I18nProvider } from "@/i18n/I18nProvider";

// §18 UI-locale seam. Bridges the auth + workspace context into
// `<I18nProvider>` so the negotiation chain (user preferred_locale →
// browser → workspace default → en-US) has real inputs. Mounted below
// `AuthProvider`/`WorkspaceProvider` — before login there is no user or
// workspace, so both inputs are null and the chain lands on the browser
// language (English on our target audience) then `en-US`.

/**
 * Default locale of the workspace the caller is currently acting in.
 * The active slug is matched against `available_workspaces` the same
 * way `activeWorkspaceGrantRole` does; a lone-workspace user resolves
 * to their only workspace even before a slug is picked.
 */
function activeWorkspaceDefaultLocale(
  user: AuthMe | null,
  workspaceId: string | null,
): string | null {
  if (!user) return null;
  const entries = user.available_workspaces;
  const activeId = workspaceId ?? user.current_workspace_id;
  const active = activeId
    ? entries.find((w) => w.workspace.id === activeId || w.workspace_id === activeId)
    : undefined;
  const chosen = active ?? (entries.length === 1 ? entries[0] : undefined);
  return chosen?.workspace.default_locale ?? null;
}

export function ConnectedI18nProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const { workspaceId } = useWorkspace();
  return (
    <I18nProvider
      preferredLocale={user?.preferred_locale ?? null}
      workspaceDefaultLocale={activeWorkspaceDefaultLocale(user, workspaceId)}
    >
      {children}
    </I18nProvider>
  );
}
