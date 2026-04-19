import type { ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import WorkspaceSwitcher from "@/components/WorkspaceSwitcher";

// Shared sidebar used by both ManagerLayout (`.desk__nav` inside
// `.desk`) and EmployeeLayout (`.desk__nav` inside `.phone`, revealed
// at >=720px; the phone-mode bottom tab bar takes over below).
//
// The visual system — brand row, section labels, nav-link padding,
// hover / active colours, bottom "me" card — lives entirely in CSS
// against these class names (`.desk__brand`, `.desk__nav-group`,
// `.nav-section`, `.nav-link`, `.desk__me`). Callers pass items +
// footer; the component renders the chrome.
//
// Collapsed state (`collapsed={true}`): the wordmark disappears
// (only the ◈ bookplate stays), nav labels collapse to their icons,
// and labels appear as hover tooltips to the right of each glyph.
// Callers own the state so one cookie roundtrip sits in the layout.

// `phoneHidden` items still render in the DOM (so the desktop side nav
// shows them), but pick up a `--phone-hidden` modifier the CSS uses to
// hide them inside the off-canvas hamburger drawer at <=720px. Use it
// for entries that the bottom tab bar already exposes.
export interface SideNavLinkItem {
  type: "link";
  to: string;
  label: string;
  // Icon rendered when the rail is collapsed and alongside the label
  // when expanded. Every link should ship one so the collapsed rail is
  // fully legible; the component falls back to a generic glyph when
  // missing so older call sites still render.
  icon?: ReactNode;
  matchPrefix?: string | string[];
  phoneHidden?: boolean;
}

export interface SideNavSectionItem {
  type: "section";
  label: string;
  phoneHidden?: boolean;
}

export type SideNavItem = SideNavLinkItem | SideNavSectionItem;

interface SideNavFooter {
  initials: string;
  avatarUrl?: string | null;
  name: string;
  role: string;
}

interface SideNavProps {
  items: SideNavItem[];
  footer?: SideNavFooter;
  action?: React.ReactNode;
  ariaLabel?: string;
  onLinkClick?: () => void;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
  brand?: string;
}

export default function SideNav({
  items,
  footer,
  action,
  ariaLabel = "Main navigation",
  onLinkClick,
  collapsed = false,
  onToggleCollapsed,
  brand = "crew.day",
}: SideNavProps) {
  const canCollapse = typeof onToggleCollapsed === "function";
  const className =
    "desk__nav" + (collapsed && canCollapse ? " desk__nav--collapsed" : "");
  return (
    <aside
      className={className}
      aria-label={ariaLabel}
      data-collapsed={collapsed && canCollapse ? "true" : "false"}
    >
      <div className="desk__brand" title={collapsed ? brand : undefined}>
        <span className="desk__logo" aria-hidden="true">◈</span>
        <span className="desk__wordmark">{brand}</span>
        {canCollapse && (
          <button
            type="button"
            className="desk__nav-hinge"
            onClick={onToggleCollapsed}
            aria-expanded={!collapsed}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            data-tip={collapsed ? "Expand sidebar" : undefined}
          >
            <span className="desk__nav-hinge__icon" aria-hidden="true">
              {collapsed
                ? <PanelLeftOpen size={16} strokeWidth={1.8} />
                : <PanelLeftClose size={16} strokeWidth={1.8} />}
            </span>
          </button>
        )}
      </div>
      <WorkspaceSwitcher />
      <nav className="desk__nav-group">
        {items.map((item, i) =>
          item.type === "section" ? (
            <div
              key={"s-" + i}
              className={"nav-section" + (item.phoneHidden ? " nav-section--phone-hidden" : "")}
            >
              <span className="nav-section__text">{item.label}</span>
              <span className="nav-section__rule" aria-hidden="true" />
            </div>
          ) : (
            <NavItem
              key={item.to}
              to={item.to}
              matchPrefix={item.matchPrefix}
              phoneHidden={item.phoneHidden}
              icon={item.icon}
              label={item.label}
              onClick={onLinkClick}
            />
          ),
        )}
      </nav>
      {action && <div className="desk__nav-action">{action}</div>}
      {footer && (
        <div className="desk__me" data-tip={footer.name + " · " + footer.role}>
          <span className="avatar avatar--md">
            {footer.avatarUrl
              ? <img className="avatar__img" src={footer.avatarUrl} alt={footer.name} />
              : footer.initials}
          </span>
          <div className="desk__me-meta">
            <div className="desk__me-name">{footer.name}</div>
            <div className="desk__me-role">{footer.role}</div>
          </div>
        </div>
      )}
    </aside>
  );
}

interface NavItemProps {
  to: string;
  matchPrefix?: string | string[];
  phoneHidden?: boolean;
  icon?: ReactNode;
  label: string;
  onClick?: () => void;
}

function NavItem({ to, matchPrefix, phoneHidden, icon, label, onClick }: NavItemProps) {
  const { pathname } = useLocation();
  const prefixes = matchPrefix
    ? Array.isArray(matchPrefix) ? matchPrefix : [matchPrefix]
    : null;
  const active = prefixes ? prefixes.some((p) => pathname.startsWith(p)) : pathname === to;
  return (
    <NavLink
      to={to}
      onClick={onClick}
      data-tip={label}
      className={
        "nav-link" +
        (active ? " nav-link--active" : "") +
        (phoneHidden ? " nav-link--phone-hidden" : "")
      }
    >
      <span className="nav-link__icon" aria-hidden="true">
        {icon ?? <NavFallbackGlyph label={label} />}
      </span>
      <span className="nav-link__label">{label}</span>
      <span className="nav-link__marker" aria-hidden="true" />
    </NavLink>
  );
}

// Cheap fallback so legacy items without an `icon` still render
// recognisably when the rail is collapsed — draws the first letter of
// the label inside the slot where a lucide icon would sit.
function NavFallbackGlyph({ label }: { label: string }) {
  const ch = (label.trim()[0] ?? "·").toUpperCase();
  return <span className="nav-link__fallback-glyph">{ch}</span>;
}
