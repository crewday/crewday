import { useCallback, useState, type FormEvent, type ReactElement } from "react";
import { Link, useNavigate } from "react-router-dom";
import { normalizeWorkspaceSlugInput } from "@/lib/workspaceSlug";

export default function WorkspaceCreatePage(): ReactElement {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");

  const onSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const normalizedSlug = normalizeWorkspaceSlugInput(slug);
      if (!name.trim() || !normalizedSlug) return;
      navigate(`/w/${normalizedSlug}/today`);
    },
    [name, navigate, slug],
  );

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
              />
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
              />
              <span className="login__hint">
                Lowercase letters, digits, and hyphens. Lives at <code>/w/&lt;handle&gt;/</code>.
              </span>
            </label>
            <button type="submit" className="btn btn--moss btn--lg">
              Create workspace
            </button>
          </form>
          <Link to="/" className="login__recover">Back to current workspace</Link>
        </div>
      </main>
    </div>
  );
}
