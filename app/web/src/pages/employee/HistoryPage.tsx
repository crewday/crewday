import { Link, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import { fmtDate, fmtDateTime } from "@/lib/dates";
import { cap } from "@/lib/strings";
import { Loading } from "@/components/common";
import PageHeader from "@/components/PageHeader";
import type { HistoryPagePayload, HistoryTab, Property } from "@/types/api";

type Tab = HistoryTab;

const TABS: [HistoryTab, string][] = [
  ["tasks", "Tasks"],
  ["chats", "Chats"],
  ["expenses", "Expenses"],
  ["leaves", "Leaves"],
];

function isTab(v: string): v is Tab {
  return v === "tasks" || v === "chats" || v === "expenses" || v === "leaves";
}

type HistoryQueries = {
  tasks: ReturnType<typeof useHistoryTabQuery<"tasks">>;
  chats: ReturnType<typeof useHistoryTabQuery<"chats">>;
  expenses: ReturnType<typeof useHistoryTabQuery<"expenses">>;
  leaves: ReturnType<typeof useHistoryTabQuery<"leaves">>;
};

type HistoryContentState = {
  isPending: boolean;
  isError: boolean;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  onLoadMore: () => void;
};

export default function HistoryPage() {
  const [params] = useSearchParams();
  const raw = params.get("tab") ?? "tasks";
  const tab: Tab = isTab(raw) ? raw : "tasks";

  const queries: HistoryQueries = {
    tasks: useHistoryTabQuery("tasks", tab === "tasks"),
    chats: useHistoryTabQuery("chats", tab === "chats"),
    expenses: useHistoryTabQuery("expenses", tab === "expenses"),
    leaves: useHistoryTabQuery("leaves", tab === "leaves"),
  };

  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  const propsById = new Map((propsQ.data ?? []).map((p) => [p.id, p]));
  const activePanel = {
    tasks: <TaskHistoryPanel query={queries.tasks} propsById={propsById} />,
    chats: <ChatHistoryPanel query={queries.chats} />,
    expenses: <ExpenseHistoryPanel query={queries.expenses} />,
    leaves: <LeaveHistoryPanel query={queries.leaves} />,
  }[tab];

  return (
    <>
      <PageHeader
        title="History"
        sub="Everything already wrapped up — tasks, chats, expenses and leaves."
      />
      <section className="phone__section">
        <HistoryTabs activeTab={tab} />
        {activePanel}
      </section>
    </>
  );
}

function useHistoryTabQuery<T extends HistoryTab>(tab: T, enabled: boolean) {
  return useInfiniteQuery({
    queryKey: qk.history(tab),
    queryFn: ({ pageParam }) => fetchHistoryPage(tab, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? (lastPage.next_cursor ?? undefined) : undefined,
    enabled,
  });
}

function HistoryTabs({ activeTab }: { activeTab: Tab }) {
  return (
    <nav className="tabs" aria-label="History tabs">
      {TABS.map(([key, label]) => (
        <Link
          key={key}
          to={"/history?tab=" + key}
          className={"tab-link" + (activeTab === key ? " tab-link--active" : "")}
        >
          {label}
        </Link>
      ))}
    </nav>
  );
}

function TaskHistoryPanel({
  query,
  propsById,
}: {
  query: HistoryQueries["tasks"];
  propsById: Map<string, Property>;
}) {
  return (
    <HistoryContent state={historyContentState(query)}>
      <TaskHistory
        tasks={query.data?.pages.flatMap((page) => page.data) ?? []}
        propsById={propsById}
      />
    </HistoryContent>
  );
}

function ChatHistoryPanel({ query }: { query: HistoryQueries["chats"] }) {
  return (
    <HistoryContent state={historyContentState(query)}>
      <ChatHistory chats={query.data?.pages.flatMap((page) => page.data) ?? []} />
    </HistoryContent>
  );
}

function ExpenseHistoryPanel({ query }: { query: HistoryQueries["expenses"] }) {
  return (
    <HistoryContent state={historyContentState(query)}>
      <ExpenseHistory expenses={query.data?.pages.flatMap((page) => page.data) ?? []} />
    </HistoryContent>
  );
}

function LeaveHistoryPanel({ query }: { query: HistoryQueries["leaves"] }) {
  return (
    <HistoryContent state={historyContentState(query)}>
      <LeaveHistory leaves={query.data?.pages.flatMap((page) => page.data) ?? []} />
    </HistoryContent>
  );
}

function historyContentState(
  query: HistoryQueries[keyof HistoryQueries],
): HistoryContentState {
  return {
    isPending: query.isPending,
    isError: query.isError,
    hasNextPage: query.hasNextPage,
    isFetchingNextPage: query.isFetchingNextPage,
    onLoadMore: () => void query.fetchNextPage(),
  };
}

function fetchHistoryPage<T extends HistoryTab>(
  tab: T,
  cursor: string | null,
): Promise<HistoryPagePayload<T>> {
  const params = new URLSearchParams({ tab });
  if (cursor !== null) params.set("cursor", cursor);
  return fetchJson<HistoryPagePayload<T>>("/api/v1/history?" + params.toString());
}

function HistoryContent(props: {
  state: HistoryContentState;
  children: ReactNode;
}) {
  const { state, children } = props;

  if (state.isPending) {
    return <Loading />;
  }
  if (state.isError) {
    return <p className="muted">Failed to load.</p>;
  }
  return (
    <>
      {children}
      {state.hasNextPage ? (
        <button
          type="button"
          className="btn btn--secondary"
          onClick={state.onLoadMore}
          disabled={state.isFetchingNextPage}
        >
          {state.isFetchingNextPage ? "Loading..." : "Load more"}
        </button>
      ) : null}
    </>
  );
}

function TaskHistory({
  tasks,
  propsById,
}: {
  tasks: HistoryPagePayload<"tasks">["data"];
  propsById: Map<string, Property>;
}) {
  return (
    <ul className="task-list">
      {tasks.length === 0 ? (
        <li className="empty-state empty-state--quiet">No past tasks.</li>
      ) : (
        tasks.map((t) => {
          const prop = propsById.get(t.property_id);
          return (
            <li key={t.id} className="stack-row">
              <div>
                <strong>{t.title}</strong>
                <div className="stack-row__sub">
                  {prop ? prop.name : t.property_id} · {fmtDateTime(t.scheduled_start)}
                </div>
              </div>
              <span className={"chip chip--sm chip--" + (t.status === "completed" ? "moss" : "rust")}>
                {cap(t.status)}
              </span>
            </li>
          );
        })
      )}
    </ul>
  );
}

function ChatHistory({ chats }: { chats: HistoryPagePayload<"chats">["data"] }) {
  return (
    <ul className="task-list">
      {chats.length === 0 ? (
        <li className="empty-state empty-state--quiet">No archived chats.</li>
      ) : (
        chats.map((c) => (
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
  );
}

function ExpenseHistory({
  expenses,
}: {
  expenses: HistoryPagePayload<"expenses">["data"];
}) {
  return (
    <ul className="task-list">
      {expenses.length === 0 ? (
        <li className="empty-state empty-state--quiet">No past expenses.</li>
      ) : (
        expenses.map((x) => {
          const stamp = x.submitted_at ?? x.purchased_at;
          return (
            <li key={x.id} className="stack-row">
              <div>
                <strong>
                  {x.vendor} · {formatMoney(x.total_amount_cents, x.currency)}
                </strong>
                <div className="stack-row__sub">
                  {fmtDate(stamp)} · {x.note_md}
                </div>
              </div>
              <span className={"chip chip--sm chip--" + (x.state === "reimbursed" ? "moss" : "sky")}>
                {cap(x.state)}
              </span>
            </li>
          );
        })
      )}
    </ul>
  );
}

function LeaveHistory({ leaves }: { leaves: HistoryPagePayload<"leaves">["data"] }) {
  return (
    <ul className="task-list">
      {leaves.length === 0 ? (
        <li className="empty-state empty-state--quiet">No past leaves.</li>
      ) : (
        leaves.map((lv) => (
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
  );
}
