import { Fragment, useEffect, useRef } from "react";
import DateTime from "@/components/DateTime";
import AgentMessageLinks from "@/components/chat/AgentMessageLinks";
import ChatMessageBody from "@/components/chat/ChatMessageBody";
import type { AgentActivityState } from "@/lib/sse";
import type { AgentMessage } from "@/types/api";

type ActionDecision = "approve" | "details";

export interface ChatLogProps {
  messages: AgentMessage[] | undefined;
  onDecideAction?: (idx: number, decision: ActionDecision) => void;
  /** Applied to the outer `.chat-log`. `chat-log--inline` removes the
   *  flex:1 scroll-box behaviour so the log flows inside a regular page. */
  variant?: "screen" | "inline";
  ariaLabel?: string;
  /** §14 "Agent turn indicator", when true, renders a WhatsApp-style
   *  typing pill (three animated dots) at the tail of the log. */
  typing?: boolean;
  activity?: AgentActivityState;
}

export default function ChatLog(props: ChatLogProps) {
  const {
    messages,
    onDecideAction,
    variant = "screen",
    ariaLabel,
    typing = false,
    activity,
  } = props;
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (variant !== "screen") return;
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages?.length, typing, variant]);

  const className = variant === "inline" ? "chat-log chat-log--inline" : "chat-log";

  return (
    <div
      className={className}
      role="log"
      aria-live="polite"
      aria-label={ariaLabel}
      ref={logRef}
    >
      {messages?.map((m, idx) => {
        const completedActivityLabel = activity?.label;
        const showCompletedActivity =
          completedActivityLabel &&
          !typing &&
          m.kind === "agent" &&
          idx === messages.length - 1;
        if (m.kind === "action") {
          return (
            <Fragment key={idx}>
              {showCompletedActivity && (
                <ActivityLine label={completedActivityLabel} />
              )}
              <div className="chat-msg chat-msg--action">
                <span className="chat-msg__body">{m.body}</span>
                {onDecideAction && (
                  <div className="chat-msg__ctas">
                    <button
                      className="btn btn--moss btn--sm"
                      type="button"
                      onClick={() => onDecideAction(idx, "approve")}
                    >
                      Approve
                    </button>
                    <button
                      className="btn btn--ghost btn--sm"
                      type="button"
                      onClick={() => onDecideAction(idx, "details")}
                    >
                      Details
                    </button>
                  </div>
                )}
                <DateTime value={m.at} showTime className="chat-msg__time" />
              </div>
            </Fragment>
          );
        }
        return (
          <Fragment key={idx}>
            {showCompletedActivity && (
              <ActivityLine label={completedActivityLabel} />
            )}
            <div className={"chat-msg chat-msg--" + m.kind}>
              {m.kind === "agent" ? (
                <>
                  <ChatMessageBody body={m.body} className="chat-msg__body" />
                  <AgentMessageLinks message={m} />
                </>
              ) : (
                <span className="chat-msg__body">{m.body}</span>
              )}
              <DateTime value={m.at} showTime className="chat-msg__time" />
            </div>
          </Fragment>
        );
      })}
      {typing && activity?.label && <ActivityLine label={activity.label} live />}
      {typing && (
        <div className="chat-msg chat-msg--agent chat-msg--typing">
          <span className="chat-msg__body">
            <span className="chat-typing" aria-hidden="true">
              <span className="chat-typing__dot" />
              <span className="chat-typing__dot" />
              <span className="chat-typing__dot" />
            </span>
            <span className="sr-only">Agent is typing</span>
          </span>
        </div>
      )}
    </div>
  );
}

function ActivityLine({ label, live = false }: { label: string; live?: boolean }) {
  return (
    <div className="chat-activity">
      <span aria-hidden="true">{label}</span>
      {live && (
        <output className="sr-only" aria-live="polite">
          {label}
        </output>
      )}
    </div>
  );
}
