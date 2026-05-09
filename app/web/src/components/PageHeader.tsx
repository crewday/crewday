import { type ReactNode, useCallback, useEffect, useRef } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { ChevronLeft, Menu, MoreHorizontal } from "lucide-react";
import { useShellNav } from "@/context/ShellNavContext";
import { useNavHistory } from "@/context/NavHistoryContext";
import { resolveParent, type ParentDescriptor } from "@/lib/routeParents";
import { workspaceRouteForPathname } from "@/lib/workspaceRoutes";

export interface PageHeaderOverflowItem {
  label: string;
  icon?: ReactNode;
  onSelect: () => void;
  destructive?: boolean;
  disabledReason?: string;
}

interface Props {
  title: ReactNode;
  sub?: ReactNode;
  /** Single primary trailing action (button / link). Anything beyond one
   *  action goes in `overflow` per §14 "Page header". */
  actions?: ReactNode;
  overflow?: PageHeaderOverflowItem[];
  /** Explicit parent for the back button. Pass `false` to suppress the
   *  route-derived default on a sub-page that would otherwise pick one up. */
  back?: ParentDescriptor | false;
}

// §14 "Page header". Three slots: leading (back OR hamburger), heading
// (title + optional sub), trailing (one primary action + overflow).
// Sticky on phone with safe-area inset; large Fraunces title on
// desktop. Back parent auto-derived from `routeParents.ts`.
export default function PageHeader({ title, sub, actions, overflow, back }: Props) {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const shellNav = useShellNav();
  const { backTarget } = useNavHistory();

  const resolved: ParentDescriptor | null =
    back === false
      ? null
      : back && typeof back === "object"
        ? back
        : resolveParent(pathname);

  // Back wins the leading slot over the hamburger: once the user is on
  // a sub-page, "take me back" is more useful than "open the drawer".
  const showHamburger = !resolved && Boolean(shellNav?.hasDrawer);

  // When the user actually navigated here in-app, "back" should return
  // to the previous different pathname (so /schedule -> /task/:id ->
  // back lands on /schedule, while /history and hash tab changes do not
  // walk through same-route entries). The static parent map only kicks
  // in for cold-load deep links or when the caller passed an explicit
  // `back` override.
  const historyBackTarget = resolved !== null && back === undefined ? backTarget : null;

  const hasOverflow = Boolean(overflow && overflow.length > 0);

  return (
    <header className="page-topbar">
      <div className="page-topbar__leading">
        {resolved && historyBackTarget !== null && (
          <button
            type="button"
            onClick={() => navigate(historyBackTarget, { replace: true })}
            className="page-topbar__icon-btn"
            aria-label="Back"
          >
            <ChevronLeft size={22} strokeWidth={2} aria-hidden="true" />
          </button>
        )}
        {resolved && historyBackTarget === null && (
          <Link
            to={workspaceRouteForPathname(pathname, resolved.to)}
            className="page-topbar__icon-btn"
            aria-label={`Back to ${resolved.label}`}
          >
            <ChevronLeft size={22} strokeWidth={2} aria-hidden="true" />
          </Link>
        )}
        {showHamburger && shellNav && (
          <button
            type="button"
            className="page-topbar__icon-btn page-topbar__menu-btn"
            onClick={shellNav.toggle}
            aria-label={shellNav.isOpen ? "Close menu" : "Open menu"}
            aria-expanded={shellNav.isOpen}
          >
            <Menu size={22} strokeWidth={2} aria-hidden="true" />
          </button>
        )}
      </div>
      <div className="page-topbar__heading">
        <h1 className="page-title">{title}</h1>
        {sub ? <p className="page-sub">{sub}</p> : null}
      </div>
      <div className="page-topbar__trailing">
        {actions}
        {hasOverflow ? <OverflowMenu items={overflow!} /> : null}
      </div>
    </header>
  );
}

function OverflowMenu({ items }: { items: PageHeaderOverflowItem[] }) {
  const ref = useRef<HTMLDialogElement>(null);

  const open = useCallback(() => ref.current?.showModal(), []);
  const close = useCallback(() => ref.current?.close(), []);

  // Close on outside-click within the dialog's scrim.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onClick = (e: MouseEvent) => {
      if (e.target === el) el.close();
    };
    el.addEventListener("click", onClick);
    return () => el.removeEventListener("click", onClick);
  }, []);

  return (
    <>
      <button
        type="button"
        className="page-topbar__icon-btn"
        onClick={open}
        aria-label="More actions"
        aria-haspopup="menu"
      >
        <MoreHorizontal size={22} strokeWidth={2} aria-hidden="true" />
      </button>
      <dialog ref={ref} className="overflow-menu" aria-label="More actions">
        <ul className="overflow-menu__list" role="menu">
          {items.map((it, idx) => (
            <li key={idx} role="none">
              <button
                type="button"
                role="menuitem"
                disabled={Boolean(it.disabledReason)}
                aria-disabled={it.disabledReason ? true : undefined}
                title={it.disabledReason}
                className={
                  "overflow-menu__item" +
                  (it.destructive ? " overflow-menu__item--destructive" : "") +
                  (it.disabledReason ? " overflow-menu__item--disabled" : "")
                }
                onClick={() => {
                  if (it.disabledReason) return;
                  close();
                  it.onSelect();
                }}
              >
                {it.icon ? <span className="overflow-menu__icon" aria-hidden="true">{it.icon}</span> : null}
                <span className="overflow-menu__label">
                  <span>{it.label}</span>
                  {it.disabledReason ? (
                    <span className="overflow-menu__reason">{it.disabledReason}</span>
                  ) : null}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </dialog>
    </>
  );
}
