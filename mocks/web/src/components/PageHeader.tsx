import { type ReactNode } from "react";

interface Props {
  title: ReactNode;
  sub?: ReactNode;
  actions?: ReactNode;
}

// Shared page header used by both the manager (desk) and employee
// (phone) shells, so `/today`, `/week`, `/my/expenses`, etc. wear the
// same big Fraunces title as `/dashboard`. Layout-specific spacing
// lives in `.page-topbar` rules under `.desk__main` / `.phone__body`.
export default function PageHeader({ title, sub, actions }: Props) {
  return (
    <header className="page-topbar">
      <div className="page-topbar__heading">
        <h1 className="page-title">{title}</h1>
        {sub ? <p className="page-sub">{sub}</p> : null}
      </div>
      {actions ? <div className="page-topbar__actions">{actions}</div> : null}
    </header>
  );
}
