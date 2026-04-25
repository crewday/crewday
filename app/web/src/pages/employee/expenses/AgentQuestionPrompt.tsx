import AutoGrowTextarea from "@/components/AutoGrowTextarea";

// Inline chat bubble + reply composer that lets the OCR agent ask one
// follow-up question (e.g. "is this reimbursable for client X?"). The
// worker's reply is folded into the claim's `note` so the manager
// sees the full context at approval time.
export default function AgentQuestionPrompt({
  question,
  reply,
  onReplyChange,
  onDismiss,
}: {
  question: string;
  reply: string;
  onReplyChange: (next: string) => void;
  onDismiss: () => void;
}) {
  return (
    <div className="chat-log chat-log--inline">
      <div className="chat-msg chat-msg--agent">
        <span className="chat-msg__body">{question}</span>
      </div>
      <div className="comment__compose">
        <AutoGrowTextarea
          placeholder="Your reply..."
          value={reply}
          onChange={(e) => onReplyChange(e.target.value)}
        />
        <button
          type="button"
          className="btn btn--sm btn--moss"
          onClick={onDismiss}
        >
          Reply
        </button>
      </div>
    </div>
  );
}
