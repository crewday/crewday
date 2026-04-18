import { useState } from "react";

// Static (mock) self-service recovery surface (§03).
// Workers, clients, and guests: enter email only.
// Managers and owners-group members: step-up — email AND an unused
// break-glass code. The server responds with a generic
// "sent_if_exists" payload either way to avoid role enumeration.
// Magic links never issue a session on their own; the link lands
// on /recover/enroll and walks the user through a fresh passkey
// ceremony with the usual re-enrollment side-effects.

export default function RecoverPage() {
  const [stepUp, setStepUp] = useState(false);

  return (
    <div className="surface surface--login">
      <main className="login">
        <div className="login__card">
          <div className="login__brand">
            <span className="desk__logo" aria-hidden="true">◈</span>
            <span className="desk__wordmark">crewday</span>
          </div>
          <h1 className="login__headline">Lost your device?</h1>
          <p className="login__sub">
            We'll email you a one-time link that lets you register a new passkey. Your old
            passkeys are revoked when the new one is saved.
          </p>
          <form className="form" onSubmit={(e) => e.preventDefault()}>
            <label className="field">
              <span>Your email</span>
              <input type="email" placeholder="you@example.com" autoComplete="email" required />
            </label>

            <label className="field field--inline">
              <input
                type="checkbox"
                checked={stepUp}
                onChange={(e) => setStepUp(e.target.checked)}
              />
              <span>I'm a manager or owner (I have a break-glass code)</span>
            </label>

            {stepUp ? (
              <label className="field">
                <span>Break-glass code</span>
                <input
                  className="recovery-code"
                  placeholder="XXXXXXXXXX"
                  autoComplete="one-time-code"
                  required
                />
              </label>
            ) : null}

            <button type="button" className="btn btn--moss btn--lg">
              Send recovery link
            </button>
          </form>
          <p className="login__footnote muted">
            Links expire after one use or 15 minutes, whichever comes first. If nothing arrives,
            your workspace may have disabled self-service recovery — ask a manager to re-issue
            your link.
          </p>
          <a href="/login" className="login__recover">← Back to sign in</a>
        </div>
      </main>
    </div>
  );
}
