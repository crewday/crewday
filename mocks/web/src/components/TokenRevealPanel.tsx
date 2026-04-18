import { useState } from "react";
import { KeyRound, X, Copy, Check } from "lucide-react";
import type { ApiTokenCreated } from "@/types/api";

// §03 "Save this token now" ceremony. The only moment the plaintext
// secret is visible — rendered as a sealed-envelope reveal, not as
// an error panel. Copy-to-clipboard hints that the user should take
// the plaintext *now*; the curl snippet below lets them paste it
// into a script in the next breath.
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
            {created.plaintext}
          </code>
          <button
            type="button"
            className={
              "tokens-reveal__copy" + (copied === "secret" ? " tokens-reveal__copy--done" : "")
            }
            onClick={() => copy(created.plaintext, "secret")}
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
          <code>{created.curl_example}</code>
        </pre>
      </div>
    </section>
  );
}
