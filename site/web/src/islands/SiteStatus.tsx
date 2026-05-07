import { ClipboardList, MessageSquare } from "@/icons";

export function SiteStatus({
  ariaLabel,
  label,
  text,
}: {
  ariaLabel: string;
  label: string;
  text: string;
}) {
  return (
    <aside className="site-status" aria-label={ariaLabel}>
      <div className="site-status__icon" aria-hidden="true">
        <ClipboardList size={20} strokeWidth={1.75} />
      </div>
      <div className="site-status__body">
        <p className="site-status__label">{label}</p>
        <p className="site-status__text">{text}</p>
      </div>
      <MessageSquare
        className="site-status__mark"
        size={20}
        strokeWidth={1.75}
        aria-hidden="true"
      />
    </aside>
  );
}
