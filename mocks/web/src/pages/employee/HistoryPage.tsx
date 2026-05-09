import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import { fmtDate, fmtDateTime } from "@/lib/dates";
import { cap } from "@/lib/strings";
import { Loading } from "@/components/common";
import PageHeader from "@/components/PageHeader";
import PageTabs, { type PageTab } from "@/components/PageTabs";
import type { HistoryPayload, Property } from "@/types/api";

type Tab = "tasks" | "chats" | "expenses" | "leaves";

const TABS: [Tab, string][] = [
  ["tasks", "Tasks"],
  ["chats", "Chats"],
  ["expenses", "Expenses"],
  ["leaves", "Leaves"],
];

const PAGE_TABS: PageTab[] = TABS.map(([key, label]) => ({
  key,
  label,
  panelId: `history-${key}-panel`,
}));

function isTab(v: string | null): v is Tab {
  return v === "tasks" || v === "chats" || v === "expenses" || v === "leaves";
}

function tabFromHash(hash: string): Tab | null {
  if (!hash) return null;
  try {
    const decoded = decodeURIComponent(hash.replace(/^#/, ""));
    return isTab(decoded) ? decoded : null;
  } catch {
    return null;
  }
}

function tabFromSearch(search: string): Tab | null {
  const tab = new URLSearchParams(search).get("tab");
  return isTab(tab) ? tab : null;
}

function tabFromLocation(location: { hash: string; search: string }): Tab {
  return tabFromHash(location.hash) ?? tabFromSearch(location.search) ?? "tasks";
}

export default function HistoryPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const routeTab = useMemo(() => tabFromLocation(location), [location]);
  const [tab, setTab] = useState<Tab>(routeTab);

  useEffect(() => {
    setTab(routeTab);
  }, [routeTab]);

  useEffect(() => {
    const queryTab = tabFromSearch(location.search);
    if (!queryTab || tabFromHash(location.hash)) return;
    const params = new URLSearchParams(location.search);
    params.delete("tab");
    const nextSearch = params.toString();
    navigate(
      {
        pathname: location.pathname,
        search: nextSearch ? `?${nextSearch}` : "",
        hash: `#${queryTab}`,
      },
      { replace: true },
    );
  }, [location.hash, location.pathname, location.search, navigate]);

  useEffect(() => {
    const syncFromWindowHash = () => {
      const hashTab = tabFromHash(window.location.hash);
      if (hashTab) setTab(hashTab);
    };
    window.addEventListener("hashchange", syncFromWindowHash);
    return () => window.removeEventListener("hashchange", syncFromWindowHash);
  }, []);

  const q = useQuery({
    queryKey: qk.history(tab),
    queryFn: () => fetchJson<HistoryPayload>("/api/v1/history?tab=" + tab),
  });

  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  const propsById = new Map((propsQ.data ?? []).map((p) => [p.id, p]));

  return (
    <>
      <PageHeader
        title="History"
        sub="Everything already wrapped up — tasks, chats, expenses and leaves."
      />
      <section className="phone__section">
        <PageTabs
          ariaLabel="History tabs"
          tabs={PAGE_TABS}
          hashBacked
          defaultKey="tasks"
          selectedKey={tab}
          onSelect={(key) => {
            if (isTab(key)) setTab(key);
          }}
        />

        <div id={`history-${tab}-panel`} role="tabpanel">
          {q.isPending ? (
            <Loading />
          ) : q.isError || !q.data ? (
            <p className="muted">Failed to load.</p>
          ) : tab === "tasks" ? (
            <ul className="task-list">
              {q.data.tasks.length === 0 ? (
                <li className="empty-state empty-state--quiet">No past tasks.</li>
              ) : (
                q.data.tasks.map((t) => {
                  const prop = propsById.get(t.property_id);
                  return (
                    <li key={t.id} className="stack-row">
                      <div>
                        <strong>{t.title}</strong>
                        <div className="stack-row__sub">
                          {prop ? prop.name : t.property_id} · {fmtDateTime(t.scheduled_start)}
                        </div>
                      </div>
                      <span
                        className={
                          "chip chip--sm chip--" +
                          (t.status === "completed" ? "moss" : "rust")
                        }
                      >
                        {cap(t.status)}
                      </span>
                    </li>
                  );
                })
              )}
            </ul>
          ) : tab === "chats" ? (
            <ul className="task-list">
              {q.data.chats.length === 0 ? (
                <li className="empty-state empty-state--quiet">No archived chats.</li>
              ) : (
                q.data.chats.map((c) => (
                  <li key={c.id} className="stack-row">
                    <div>
                      <strong>{c.title}</strong>
                      <div className="stack-row__sub">{c.summary}</div>
                    </div>
                    <span className="chip chip--sm chip--ghost">{c.last_at}</span>
                  </li>
                ))
              )}
            </ul>
          ) : tab === "expenses" ? (
            <ul className="task-list">
              {q.data.expenses.length === 0 ? (
                <li className="empty-state empty-state--quiet">No past expenses.</li>
              ) : (
                q.data.expenses.map((x) => (
                  <li key={x.id} className="stack-row">
                    <div>
                      <strong>
                        {x.merchant} · {formatMoney(x.amount_cents, x.currency)}
                      </strong>
                      <div className="stack-row__sub">
                        {fmtDate(x.submitted_at)} · {x.note}
                      </div>
                    </div>
                    <span
                      className={
                        "chip chip--sm chip--" +
                        (x.status === "reimbursed" ? "moss" : "sky")
                      }
                    >
                      {cap(x.status)}
                    </span>
                  </li>
                ))
              )}
            </ul>
          ) : (
            <ul className="task-list">
              {q.data.leaves.length === 0 ? (
                <li className="empty-state empty-state--quiet">No past leaves.</li>
              ) : (
                q.data.leaves.map((lv) => (
                  <li key={lv.id} className="stack-row">
                    <div>
                      <strong>
                        {fmtDate(lv.starts_on)} → {fmtDate(lv.ends_on)}
                      </strong>
                      <div className="stack-row__sub">
                        {cap(lv.category)} · {lv.note}
                      </div>
                    </div>
                    <span className="chip chip--sm chip--moss">Approved</span>
                  </li>
                ))
              )}
            </ul>
          )}
        </div>
      </section>
    </>
  );
}
