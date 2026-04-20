import { useEffect, useRef } from "react";
import { fmtTime } from "@/lib/dates";
import type { AgentMessage } from "@/types/api";

type ActionDecision = "approve" | "details";

export interface ChatLogProps {
  messages: AgentMessage[] | undefined;
  onDecideAction?: (idx: number, decision: ActionDecision) => void;
  /** Applied to the outer `.chat-log`. `chat-log--inline` removes the
   *  flex:1 scroll-box behaviour so the log flows inside a regular page. */
  variant?: "screen" | "inline";
  ariaLabel?: string;
  /** §14 "Agent turn indicator" — when true, renders a WhatsApp-style
   *  typing pill (three animated dots) at the tail of the log. */
  typing?: boolean;
}

export default function ChatLog({
  messages,
  onDecideAction,
  variant = "screen",
  ariaLabel,
  typing = false,
}: ChatLogProps) {
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
        if (m.kind === "action") {
          return (
            <div key={idx} className="chat-msg chat-msg--action">
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
              <span className="chat-msg__time">{fmtTime(m.at)}</span>
            </div>
          );
        }
        return (
          <div key={idx} className={"chat-msg chat-msg--" + m.kind}>
            <span className="chat-msg__body">{m.body}</span>
            <span className="chat-msg__time">{fmtTime(m.at)}</span>
          </div>
        );
      })}
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
