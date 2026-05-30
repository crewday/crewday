import { useCallback, useRef, useState, type FormEvent, type ReactElement } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { ApiError, fetchJson } from "@/lib/api";
import { useWorkspace } from "@/context/WorkspaceContext";
import { normalizeWorkspaceSlugInput } from "@/lib/workspaceSlug";
import {
  messageForSignupSlugError,
  stateForSignupError,
  type SignupFormState,
  type SlugError,
} from "@/pages/public/publicAuthMappers";

interface WorkspaceCreateBody {
  slug: string;
  name: string;
}

interface WorkspaceCreateResponse {
  workspace_id: string;
  workspace_slug: string;
  redirect: string;
}

type WorkspaceCreateState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "slug_error"; error: SlugError }
  | { kind: "error"; message: string };

export default function WorkspaceCreatePage(): ReactElement {
  const navigate = useNavigate();
  const { workspaceId, setWorkspaceId } = useWorkspace();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [form, setForm] = useState<WorkspaceCreateState>({ kind: "idle" });
  const inflightRef = useRef(false);

  const mutation = useMutation<WorkspaceCreateResponse, Error, WorkspaceCreateBody>({
    mutationFn: (body) =>
      fetchJson<WorkspaceCreateResponse>("/api/v1/me/workspaces", {
        method: "POST",
        body,
      }),
    onMutate: () => {
      setForm({ kind: "pending" });
    },
    onSuccess: (created) => {
      inflightRef.current = false;
      setWorkspaceId(created.workspace_slug);
      navigate(created.redirect, { replace: true });
    },
    onError: (err) => {
      setForm(workspaceCreateStateForError(err));
      inflightRef.current = false;
    },
  });

  const onSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (inflightRef.current) return;
      if (mutation.isPending) return;
      const trimmedName = name.trim();
      const normalizedSlug = normalizeWorkspaceSlugInput(slug);
      if (!trimmedName || !normalizedSlug) return;
      inflightRef.current = true;
      mutation.mutate({ slug: normalizedSlug, name: trimmedName });
    },
    [mutation, name, slug],
  );

  const acceptSuggestion = useCallback((suggestion: string) => {
    setSlug(normalizeWorkspaceSlugInput(suggestion));
    setForm({ kind: "idle" });
  }, []);

  const pending = form.kind === "pending";
  const backTo = workspaceId ? `/w/${encodeURIComponent(workspaceId)}` : "/";

  return (
    <div className="surface surface--login">
      <main className="login">
        <div className="login__card">
          <div className="login__brand">
            <span className="desk__logo" aria-hidden="true">◈</span>
            <span className="desk__wordmark">crew.day</span>
          </div>
          <h1 className="login__headline">New workspace</h1>
          <p className="login__sub">
            Create another workspace for this signed-in account. Your existing passkey stays
            attached to your account.
          </p>
          {form.kind === "error" && (
            <p
              className="login__notice login__notice--danger"
              role="alert"
              data-testid="workspace-create-error"
            >
              {form.message}
            </p>
          )}
          <form className="form" onSubmit={onSubmit}>
            <label className="field">
              <span>Workspace name</span>
              <input
                type="text"
                placeholder="Villa Sud"
                autoComplete="organization"
                required
                value={name}
                onChange={(event) => setName(event.target.value)}
                data-testid="workspace-create-name"
               aria-label="field Workspace name text Villa Sud organization workspace-create-name"/>
            </label>

            <label className="field">
              <span>Workspace handle</span>
              <input
                type="text"
                placeholder="villa-sud"
                autoComplete="off"
                spellCheck={false}
                inputMode="url"
                pattern="[a-z][a-z0-9-]{1,38}[a-z0-9]"
                required
                value={slug}
                onChange={(event) => setSlug(normalizeWorkspaceSlugInput(event.target.value))}
                data-testid="workspace-create-slug"
                aria-describedby="workspace-create-slug-hint"
               aria-label="field Workspace handle text villa-sud off url [a-z][a-z0-9-]{1,38}[a-z0-9] workspace-create-slug workspace-create-slug-hint workspace-create-slug-hint login__hint Lowercase letters, digits, and hyphens. Lives at /w/&lt;handle&gt;/ ."/>
              <span id="workspace-create-slug-hint" className="login__hint">
                Lowercase letters, digits, and hyphens. Lives at <code>/w/&lt;handle&gt;/</code>.
              </span>
            </label>

            {form.kind === "slug_error" && (
              <WorkspaceCreateSlugError error={form.error} onAccept={acceptSuggestion} />
            )}

            <button
              type="submit"
              className="btn btn--moss btn--lg"
              disabled={pending}
              aria-busy={pending}
              data-testid="workspace-create-submit"
            >
              {pending ? "Creating workspace..." : "Create workspace"}
            </button>
          </form>
          <Link to={backTo} className="login__recover">Back to current workspace</Link>
        </div>
      </main>
    </div>
  );
}

function WorkspaceCreateSlugError({
  error,
  onAccept,
}: {
  error: SlugError;
  onAccept: (suggestion: string) => void;
}): ReactElement {
  return (
    <p
      className="login__notice login__notice--danger"
      role="alert"
      data-testid="workspace-create-slug-error"
    >
      {messageForSignupSlugError(error)}
      {error.suggestion && (
        <>
          {" "}
          <button
            type="button"
            className="login__recover"
            onClick={() => onAccept(error.suggestion!)}
            data-testid="workspace-create-slug-accept"
          >
            Use <strong>{error.suggestion}</strong> instead?
          </button>
        </>
      )}
    </p>
  );
}

function workspaceCreateStateForError(error: unknown): WorkspaceCreateState {
  if (!(error instanceof ApiError)) {
    return { kind: "error", message: "We couldn't reach the workspace service. Try again in a moment." };
  }
  const detail = workspaceErrorDetail(error);
  if (error.status === 409) {
    const signupState: SignupFormState = stateForSignupError(error);
    if (signupState.kind === "slug_error") return signupState;
    return { kind: "error", message: "That workspace handle cannot be used. Try another." };
  }
  if (error.status === 422 && detail?.error === "invalid_slug") {
    return {
      kind: "error",
      message:
        "That workspace handle isn't valid. Use 3-40 lowercase letters, digits, or hyphens "
        + "(no leading or trailing hyphen).",
    };
  }
  if (error.status === 429) {
    return { kind: "error", message: "Too many workspace creation attempts. Wait a minute, then try again." };
  }
  return { kind: "error", message: "We couldn't create that workspace. Try again in a moment." };
}

function workspaceErrorDetail(error: ApiError): { error?: string } | null {
  if (!isRecord(error.body)) return null;
  const detail = error.body.detail;
  return isRecord(detail) ? detail : error.body;
}

function isRecord(value: unknown): value is Record<string, string | unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
