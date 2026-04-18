import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageSquare, Unlink } from "lucide-react";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Chip, Loading } from "@/components/common";
import type { ChatChannelBinding, Me } from "@/types/api";

// §23 — per-user "Chat channels" section for /me. Lists the current
// user's live bindings as credential cards, provides a two-step link
// ceremony that posts to /api/v1/chat/channels/link/{start,verify}
// (mock code 424242), and lets the user unlink. No off-app preference
// toggles: the user either has an active WhatsApp binding (agent may
// reach out) or does not (agent is web-only). Quiet-hours are
// handled by the OS, not the app. Revoked bindings are hidden — the
// user starts a fresh link ceremony if they want one back.

function fmt(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-GB", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

export default function ChatChannelsMeCard({ me }: { me: Me }) {
  const qc = useQueryClient();
  const myUserId = me.user_id;

  const bindingsQ = useQuery({
    queryKey: qk.chatChannels(),
    queryFn: () => fetchJson<ChatChannelBinding[]>("/api/v1/chat/channels"),
  });

  const myBindings = (bindingsQ.data ?? []).filter(
    (b) => b.user_id === myUserId && b.state !== "revoked",
  );
  const hasActiveWhatsApp = myBindings.some(
    (b) => b.channel_kind === "offapp_whatsapp",
  );

  const [pendingId, setPendingId] = useState<string | null>(
    myBindings.find((b) => b.state === "pending")?.id ?? null,
  );
  const [address, setAddress] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);

  const startLink = useMutation({
    mutationFn: () =>
      fetchJson<{ binding_id: string; state: string; hint: string }>(
        "/api/v1/chat/channels/link/start",
        {
          method: "POST",
          body: {
            channel_kind: "offapp_whatsapp" as const,
            address,
            user_id: myUserId,
          },
        },
      ),
    onSuccess: (r) => {
      setPendingId(r.binding_id);
      setError(null);
      qc.invalidateQueries({ queryKey: qk.chatChannels() });
    },
    onError: () => setError("Could not start the link ceremony."),
  });

  const verifyLink = useMutation({
    mutationFn: () =>
      fetchJson<{ binding_id: string; state: string }>(
        "/api/v1/chat/channels/link/verify",
        { method: "POST", body: { binding_id: pendingId, code } },
      ),
    onSuccess: () => {
      setCode("");
      setAddress("");
      setPendingId(null);
      setError(null);
      qc.invalidateQueries({ queryKey: qk.chatChannels() });
    },
    onError: () => setError("Wrong code — try 424242 in the mock."),
  });

  const unlink = useMutation({
    mutationFn: (id: string) =>
      fetchJson<{ binding_id: string; state: string }>(
        `/api/v1/chat/channels/${id}/unlink`,
        { method: "POST" },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.chatChannels() });
    },
  });

  return (
    <section className="panel">
      <header className="panel__head">
        <div className="panel__head-stack">
          <h2>Chat channels</h2>
          <p className="panel__sub">
            Reach your agent over WhatsApp. Messages you send land in the same conversation as
            the Chat tab. Unlink to stop off-app reach-out entirely.
          </p>
        </div>
      </header>

      <div className="panel-stack">
        {bindingsQ.isPending && <Loading />}

        {!bindingsQ.isPending && myBindings.length === 0 && !pendingId && (
          <div className="tokens-empty">
            <span className="tokens-empty__glyph" aria-hidden="true">
              <MessageSquare size={20} strokeWidth={1.75} />
            </span>
            <p className="tokens-empty__title">No channels linked yet</p>
            <p className="tokens-empty__sub">
              Link WhatsApp below so your agent can reach you off-app.
            </p>
          </div>
        )}

        {myBindings.length > 0 && (
          <ul className="entry-cards">
            {myBindings.map((b) => (
              <li
                key={b.id}
                className={
                  "entry-card" + (b.state === "pending" ? " entry-card--sand" : "")
                }
              >
                <div className="entry-card__head">
                  <span className="entry-card__name">WhatsApp</span>
                  <Chip tone={b.state === "active" ? "moss" : "sand"} size="sm">
                    {b.state}
                  </Chip>
                  <div className="entry-card__action">
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost"
                      onClick={() => unlink.mutate(b.id)}
                    >
                      <Unlink size={13} strokeWidth={2} /> Unlink
                    </button>
                  </div>
                </div>

                <div className="entry-card__prefix">
                  <span className="entry-card__prefix-label">number</span>
                  <span>{b.address}</span>
                </div>

                <div className="entry-card__meta">
                  <span>
                    <span className="entry-card__meta-label">Label</span>
                    {b.display_label}
                  </span>
                  {b.state === "active" && (
                    <span>
                      <span className="entry-card__meta-label">Last used</span>
                      {fmt(b.last_message_at)}
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}

        {!hasActiveWhatsApp && (
          <div className="me-chat-channels__link">
            <h3 className="me-chat-channels__subtitle">Link WhatsApp</h3>
            {pendingId ? (
              <form
                className="me-chat-channels__form"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (code.trim()) verifyLink.mutate();
                }}
              >
                <label className="me-chat-channels__field">
                  <span>6-digit code</span>
                  <input
                    inputMode="numeric"
                    pattern="\d{6}"
                    maxLength={6}
                    placeholder="424242"
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                  />
                </label>
                <div className="me-chat-channels__actions">
                  <button className="btn btn--moss btn--sm" type="submit">
                    Verify
                  </button>
                  <button
                    className="btn btn--ghost btn--sm"
                    type="button"
                    onClick={() => {
                      setPendingId(null);
                      setCode("");
                    }}
                  >
                    Cancel
                  </button>
                </div>
                <p className="me-chat-channels__hint">
                  A code was sent via the <code>chat_channel_link_code</code>{" "}
                  template. Mock accepts <code>424242</code>.
                </p>
              </form>
            ) : (
              <form
                className="me-chat-channels__form"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (address.trim()) startLink.mutate();
                }}
              >
                <label className="me-chat-channels__field">
                  <span>E.164 phone number</span>
                  <input
                    type="tel"
                    placeholder="+33 6 00 00 00 00"
                    value={address}
                    onChange={(e) => setAddress(e.target.value)}
                  />
                </label>
                <button className="btn btn--moss btn--sm" type="submit">
                  Send code
                </button>
              </form>
            )}
            {error && <p className="me-chat-channels__error">{error}</p>}
          </div>
        )}
      </div>
    </section>
  );
}
