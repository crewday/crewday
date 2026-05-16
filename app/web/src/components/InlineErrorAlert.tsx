import { useId, useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import DisplayErrorDetails from "@/components/DisplayErrorDetails";
import type { DisplayError } from "@/lib/displayError";

interface InlineErrorAlertProps {
  error: DisplayError;
  role?: "alert" | "status";
}

export default function InlineErrorAlert({
  error,
  role = "alert",
}: InlineErrorAlertProps) {
  const detailsId = useId();
  const [expanded, setExpanded] = useState(false);
  const Icon = expanded ? ChevronUp : ChevronDown;

  return (
    <section
      className={`inline-error-alert${expanded ? " inline-error-alert--expanded" : ""}`}
      role={role}
      aria-atomic="true"
    >
      <div className="inline-error-alert__head">
        <p className="inline-error-alert__message">{error.message}</p>
        <button
          type="button"
          className="inline-error-alert__toggle"
          aria-expanded={expanded}
          aria-controls={detailsId}
          aria-label={expanded ? "Hide error details" : "Show error details"}
          onClick={() => setExpanded((current) => !current)}
        >
          <Icon size={16} strokeWidth={2.2} aria-hidden="true" />
        </button>
      </div>

      {expanded ? (
        <div className="inline-error-alert__details" id={detailsId}>
          <DisplayErrorDetails error={error} classNamePrefix="inline-error-alert" />
        </div>
      ) : null}
    </section>
  );
}
