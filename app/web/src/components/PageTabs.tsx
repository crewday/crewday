import { type KeyboardEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Link, type To } from "react-router-dom";

interface BasePageTab {
  key: string;
  label: ReactNode;
  disabled?: boolean;
}

export interface PageTab extends BasePageTab {
  panelId?: string;
}

export interface PageTabLink extends BasePageTab {
  to: To;
}

interface InPlacePageTabsProps {
  ariaLabel: string;
  tabs: PageTab[];
  hashBacked?: boolean;
  defaultKey?: string;
  selectedKey?: string;
  onSelect?: (key: string) => void;
  className?: string;
}

interface PageTabLinksProps {
  ariaLabel: string;
  tabs: PageTabLink[];
  activeKey?: string;
  className?: string;
}

export type PageTabsProps = InPlacePageTabsProps | PageTabLinksProps;

function enabledKey(tabs: PageTab[], preferredKey?: string): string {
  // code-health: ignore[nloc] Lizard misattributes the tab component body to this one-line fallback selector.
  const preferred = preferredKey ? tabs.find((tab) => tab.key === preferredKey && !tab.disabled) : undefined;
  return preferred?.key ?? tabs.find((tab) => !tab.disabled)?.key ?? tabs[0]?.key ?? "";
}

function readHashKey(tabs: PageTab[], fallbackKey: string): string {
  if (typeof window === "undefined") return fallbackKey;
  const rawHash = window.location.hash.replace(/^#/, "");
  if (!rawHash) return fallbackKey;
  let hashKey: string;
  try {
    hashKey = decodeURIComponent(rawHash);
  } catch {
    return fallbackKey;
  }
  return tabs.some((tab) => tab.key === hashKey && !tab.disabled) ? hashKey : fallbackKey;
}

function setHashKey(key: string): void {
  if (typeof window === "undefined") return;
  const nextHash = `#${encodeURIComponent(key)}`;
  if (window.location.hash !== nextHash) {
    window.location.hash = nextHash;
  }
}

function isLinkTabs(props: PageTabsProps): props is PageTabLinksProps {
  return props.tabs.some((tab) => "to" in tab);
}

export default function PageTabs(props: PageTabsProps) {
  const className = props.className ? `page-tabs ${props.className}` : "page-tabs";

  if (isLinkTabs(props)) {
    return (
      <nav className={className} aria-label={props.ariaLabel}>
        {props.tabs.map((tab) => {
          const tabClass =
            "page-tabs__tab" +
            (tab.key === props.activeKey ? " page-tabs__tab--active" : "") +
            (tab.disabled ? " page-tabs__tab--disabled" : "");
          if (tab.disabled) {
            return (
              <span key={tab.key} className={tabClass} aria-disabled="true">
                {tab.label}
              </span>
            );
          }
          return (
            <Link key={tab.key} to={tab.to} className={tabClass} aria-current={tab.key === props.activeKey ? "page" : undefined}>
              {tab.label}
            </Link>
          );
        })}
      </nav>
    );
  }

  return <InPlacePageTabs {...props} className={className} />;
}

function InPlacePageTabs(props: InPlacePageTabsProps & { className: string }) {
  const {
    ariaLabel,
    tabs,
    hashBacked = false,
    defaultKey,
    selectedKey,
    onSelect,
    className,
  } = props;
  const fallbackKey = useMemo(() => enabledKey(tabs, defaultKey), [defaultKey, tabs]);
  const initialKey = hashBacked ? readHashKey(tabs, fallbackKey) : fallbackKey;
  const [internalKey, setInternalKey] = useState(initialKey);
  const tabRefs = useRef(new Map<string, HTMLButtonElement>());
  const activeKey = selectedKey ?? internalKey;
  const selected = enabledKey(tabs, activeKey || fallbackKey);

  useEffect(() => {
    if (selectedKey !== undefined || hashBacked) return;
    setInternalKey(fallbackKey);
  }, [fallbackKey, hashBacked, selectedKey]);

  useEffect(() => {
    if (!hashBacked) return;
    const syncFromHash = () => setInternalKey(readHashKey(tabs, fallbackKey));
    syncFromHash();
    window.addEventListener("hashchange", syncFromHash);
    return () => window.removeEventListener("hashchange", syncFromHash);
  }, [fallbackKey, hashBacked, tabs]);

  const selectTab = (key: string, focus = false) => {
    const nextKey = enabledKey(tabs, key);
    if (!nextKey) return;
    setInternalKey(nextKey);
    onSelect?.(nextKey);
    if (hashBacked) setHashKey(nextKey);
    if (focus) tabRefs.current.get(nextKey)?.focus();
  };

  const selectByOffset = (offset: number) => {
    const enabledTabs = tabs.filter((tab) => !tab.disabled);
    const currentIndex = enabledTabs.findIndex((tab) => tab.key === selected);
    if (currentIndex === -1) return;
    const nextIndex = (currentIndex + offset + enabledTabs.length) % enabledTabs.length;
    const nextTab = enabledTabs[nextIndex];
    if (nextTab) selectTab(nextTab.key, true);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      selectByOffset(-1);
      return;
    }
    if (event.key === "ArrowRight") {
      event.preventDefault();
      selectByOffset(1);
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      selectTab(enabledKey(tabs), true);
      return;
    }
    if (event.key === "End") {
      event.preventDefault();
      const lastKey = [...tabs].reverse().find((tab) => !tab.disabled)?.key;
      if (lastKey) selectTab(lastKey, true);
    }
  };

  return (
    <div className={className} role="tablist" aria-label={ariaLabel} onKeyDown={handleKeyDown}>
      {tabs.map((tab) => {
        const active = tab.key === selected;
        return (
          <button
            key={tab.key}
            ref={(node) => {
              if (node) {
                tabRefs.current.set(tab.key, node);
              } else {
                tabRefs.current.delete(tab.key);
              }
            }}
            type="button"
            className={
              "page-tabs__tab" +
              (active ? " page-tabs__tab--active" : "") +
              (tab.disabled ? " page-tabs__tab--disabled" : "")
            }
            role="tab"
            aria-selected={active}
            aria-controls={tab.panelId}
            tabIndex={active ? 0 : -1}
            disabled={tab.disabled}
            onClick={() => selectTab(tab.key)}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}
