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

export default function HistoryPage() {
  const [params] = useSearchParams();
  const raw = params.get("tab") ?? "tasks";
  const tab: Tab = isTab(raw) ? raw : "tasks";

  const tasksQ = useHistoryTabQuery("tasks", tab === "tasks");
  const chatsQ = useHistoryTabQuery("chats", tab === "chats");
  const expensesQ = useHistoryTabQuery("expenses", tab === "expenses");
  const leavesQ = useHistoryTabQuery("leaves", tab === "leaves");

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
        <nav className="tabs" aria-label="History tabs">
        {TABS.map(([key, label]) => (
          <Link
            key={key}
            to={"/history?tab=" + key}
            className={"tab-link" + (tab === key ? " tab-link--active" : "")}
          >
            {label}
          </Link>
        ))}
      </nav>

      {tab === "tasks" ? (
        <HistoryContent
          isPending={tasksQ.isPending}
          isError={tasksQ.isError}
          hasNextPage={tasksQ.hasNextPage}
          isFetchingNextPage={tasksQ.isFetchingNextPage}
          onLoadMore={() => void tasksQ.fetchNextPage()}
        >
          <TaskHistory
            tasks={tasksQ.data?.pages.flatMap((page) => page.data) ?? []}
            propsById={propsById}
          />
        </HistoryContent>
      ) : null}
      {tab === "chats" ? (
        <HistoryContent
          isPending={chatsQ.isPending}
          isError={chatsQ.isError}
          hasNextPage={chatsQ.hasNextPage}
          isFetchingNextPage={chatsQ.isFetchingNextPage}
          onLoadMore={() => void chatsQ.fetchNextPage()}
        >
          <ChatHistory chats={chatsQ.data?.pages.flatMap((page) => page.data) ?? []} />
        </HistoryContent>
      ) : null}
      {tab === "expenses" ? (
        <HistoryContent
          isPending={expensesQ.isPending}
          isError={expensesQ.isError}
          hasNextPage={expensesQ.hasNextPage}
          isFetchingNextPage={expensesQ.isFetchingNextPage}
          onLoadMore={() => void expensesQ.fetchNextPage()}
        >
          <ExpenseHistory
            expenses={expensesQ.data?.pages.flatMap((page) => page.data) ?? []}
          />
        </HistoryContent>
      ) : null}
      {tab === "leaves" ? (
        <HistoryContent
          isPending={leavesQ.isPending}
          isError={leavesQ.isError}
          hasNextPage={leavesQ.hasNextPage}
          isFetchingNextPage={leavesQ.isFetchingNextPage}
          onLoadMore={() => void leavesQ.fetchNextPage()}
        >
          <LeaveHistory leaves={leavesQ.data?.pages.flatMap((page) => page.data) ?? []} />
        </HistoryContent>
      ) : null}
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

function fetchHistoryPage<T extends HistoryTab>(
  tab: T,
  cursor: string | null,
): Promise<HistoryPagePayload<T>> {
  const params = new URLSearchParams({ tab });
  if (cursor !== null) params.set("cursor", cursor);
  return fetchJson<HistoryPagePayload<T>>("/api/v1/history?" + params.toString());
}

function HistoryContent({
  isPending,
  isError,
  hasNextPage,
  isFetchingNextPage,
  onLoadMore,
  children,
}: {
  isPending: boolean;
  isError: boolean;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  onLoadMore: () => void;
  children: ReactNode;
}) {
  if (isPending) {
    return <Loading />;
  }
  if (isError) {
    return <p className="muted">Failed to load.</p>;
  }
  return (
    <>
      {children}
      {hasNextPage ? (
        <button
          type="button"
          className="btn btn--secondary"
          onClick={onLoadMore}
          disabled={isFetchingNextPage}
        >
          {isFetchingNextPage ? "Loading..." : "Load more"}
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
