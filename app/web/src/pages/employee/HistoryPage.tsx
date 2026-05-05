import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import { fmtDate, fmtDateTime } from "@/lib/dates";
import { cap } from "@/lib/strings";
import { Loading } from "@/components/common";
import PageHeader from "@/components/PageHeader";
import type { HistoryPayload, Property } from "@/types/api";

type Tab = "tasks" | "chats" | "expenses" | "leaves";

const TABS: [Tab, string][] = [
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

      <HistoryContent
        isPending={q.isPending}
        isError={q.isError}
        tab={tab}
        data={q.data}
        propsById={propsById}
      />
      </section>
    </>
  );
}

function HistoryContent({
  isPending,
  isError,
  tab,
  data,
  propsById,
}: {
  isPending: boolean;
  isError: boolean;
  tab: Tab;
  data: HistoryPayload | undefined;
  propsById: Map<string, Property>;
}) {
  if (isPending) {
    return <Loading />;
  }
  if (isError || !data) {
    return <p className="muted">Failed to load.</p>;
  }
  if (tab === "tasks") {
    return <TaskHistory tasks={data.tasks} propsById={propsById} />;
  }
  if (tab === "chats") {
    return <ChatHistory chats={data.chats} />;
  }
  if (tab === "expenses") {
    return <ExpenseHistory expenses={data.expenses} />;
  }
  return <LeaveHistory leaves={data.leaves} />;
}

function TaskHistory({
  tasks,
  propsById,
}: {
  tasks: HistoryPayload["tasks"];
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

function ChatHistory({ chats }: { chats: HistoryPayload["chats"] }) {
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

function ExpenseHistory({ expenses }: { expenses: HistoryPayload["expenses"] }) {
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

function LeaveHistory({ leaves }: { leaves: HistoryPayload["leaves"] }) {
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
