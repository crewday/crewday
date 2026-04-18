import { type ReactNode } from "react";
import PageHeader from "./PageHeader";

interface Props {
  title: string;
  sub?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}

// Shared manager-page chrome: topbar (title + optional sub + actions)
// that scrolls with the page, then content stacked at 22px gaps.
// Every ManagerLayout page renders through this so topbar spacing
// stays uniform. Shared routes (`/today`, `/week`, …) use `PageHeader`
// directly so the same header lives inside the phone shell too.
export default function DeskPage({ title, sub, actions, children }: Props) {
  return (
    <>
      <PageHeader title={title} sub={sub} actions={actions} />
      <div className="desk__content">{children}</div>
    </>
  );
}
