import { useState } from "react";
import { KeyRound, X, Copy, Check } from "lucide-react";
import type { ApiTokenCreated } from "@/types/api";

// §03 "Save this token now" ceremony. The only moment the plaintext
// secret is visible — rendered as a sealed-envelope reveal, not as
// an error panel. Copy-to-clipboard hints that the user should take
// the plaintext *now*; a curl example is built client-side from the
// shared bearer pattern (the API doesn't ship one to keep the
// response shape uniform across mint and rotate).
export default function TokenRevealPanel({
  created,
  onDismiss,
  kind = "workspace",
}: {
  created: ApiTokenCreated;
  onDismiss: () => void;
  kind?: "workspace" | "personal";
}) {
  const [copied, setCopied] = useState<"secret" | "curl" | null>(null);

  function copy(value: string, slot: "secret" | "curl") {
    try {
      void navigator.clipboard.writeText(value);
      setCopied(slot);
      window.setTimeout(() => setCopied((s) => (s === slot ? null : s)), 1800);
    } catch {
      // Presentational-only fallback — the user can still select + copy.
    }
  }

  // Built-in suggestion so the user can paste the secret into a
  // working request in the next breath. Workspace tokens hit the
  // workspace-scoped surface; personal tokens hit `/me/...`.
  const curl =
    kind === "personal"
      ? `curl -H "Authorization: Bearer ${created.token}" https://app.crew.day/api/v1/me`
      : `curl -H "Authorization: Bearer ${created.token}" https://app.crew.day/w/<slug>/api/v1/...`;

  return (
    <section className="tokens-reveal" role="status" aria-live="polite">
      <header className="tokens-reveal__ribbon">
        <span className="tokens-reveal__ribbon-mark" aria-hidden="true">
          <KeyRound size={16} strokeWidth={2} />
        </span>
        <div>
          <div className="tokens-reveal__ribbon-title">Save this token now</div>
          <div className="tokens-reveal__ribbon-sub">
            Only shown once — we store a hash, not the secret. Copy it before you dismiss this
            panel.
          </div>
        </div>
        <button
          type="button"
          className="btn btn--ghost btn--sm tokens-reveal__dismiss"
          onClick={onDismiss}
          aria-label="Dismiss"
        >
          <X size={14} strokeWidth={2} /> Dismiss
        </button>
      </header>

      <div className="tokens-reveal__body">
        <p className="tokens-reveal__label">
          {kind === "personal" ? "Personal access token" : "Workspace token"}
        </p>
        <div className="tokens-reveal__secret-row">
          <code className="tokens-reveal__secret" aria-label="Plaintext token">
            {created.token}
          </code>
          <button
            type="button"
            className={
              "tokens-reveal__copy" + (copied === "secret" ? " tokens-reveal__copy--done" : "")
            }
            onClick={() => copy(created.token, "secret")}
          >
            {copied === "secret" ? (
              <>
                <Check size={14} strokeWidth={2.5} /> Copied
              </>
            ) : (
              <>
                <Copy size={14} strokeWidth={2} /> Copy
              </>
            )}
          </button>
        </div>

        <div className="tokens-reveal__divider">Try it</div>
        <pre className="tokens-reveal__code">
          <code>{curl}</code>
        </pre>
      </div>
    </section>
  );
}
