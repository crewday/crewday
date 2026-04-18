import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { EmptyState, Loading } from "@/components/common";
import PageHeader from "@/components/PageHeader";
import TaskListCard from "@/components/TaskListCard";
import NewTaskButton from "@/components/NewTaskModal";
import type { Property, Task } from "@/types/api";

interface WeekPayload {
  tasks: Task[];
  properties: Property[];
}

export default function WeekPage() {
  const q = useQuery({
    queryKey: qk.week(),
    queryFn: () => fetchJson<WeekPayload>("/api/v1/week"),
  });

  const header = (
    <PageHeader
      title="This week"
      sub={q.data ? `${q.data.tasks.length} task${q.data.tasks.length === 1 ? "" : "s"} scheduled` : null}
      actions={<NewTaskButton />}
    />
  );

  if (q.isPending) return <>{header}<section className="phone__section"><Loading /></section></>;
  if (q.isError || !q.data) {
    return <>{header}<section className="phone__section"><EmptyState>Failed to load.</EmptyState></section></>;
  }

  const { tasks, properties } = q.data;
  const propsById = new Map(properties.map((p) => [p.id, p]));

  return (
    <>
      {header}
      <section className="phone__section">
        <ul className="task-list">
          {tasks.map((t) => (
            <li key={t.id}>
              <TaskListCard
                task={t}
                property={propsById.get(t.property_id) ?? null}
                showWeekday
                showStatus
              />
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}
