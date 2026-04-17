import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Chip, Dot, EmptyState, Loading } from "@/components/common";
import type { Property, Task } from "@/types/api";

interface WeekPayload {
  tasks: Task[];
  properties: Property[];
}

function weekWhen(iso: string): string {
  const d = new Date(iso);
  const day = d.toLocaleDateString([], { weekday: "short" });
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return day + " " + time;
}

export default function WeekPage() {
  const q = useQuery({
    queryKey: qk.week(),
    queryFn: () => fetchJson<WeekPayload>("/api/v1/week"),
  });

  if (q.isPending) return <section className="phone__section"><Loading /></section>;
  if (q.isError || !q.data) {
    return <section className="phone__section"><EmptyState>Failed to load.</EmptyState></section>;
  }

  const { tasks, properties } = q.data;
  const propsById = new Map(properties.map((p) => [p.id, p]));

  return (
    <section className="phone__section">
      <h2 className="section-title">This week</h2>
      <ul className="task-list">
        {tasks.map((t) => {
          const prop = propsById.get(t.property_id);
          if (!prop) return null;
          const cardCls =
            "task-card task-card--compact task-card--split" +
            (t.status === "completed" ? " task-card--done" : "");
          return (
            <li key={t.id}>
              <Link to={"/task/" + t.id} className={cardCls}>
                <div className="task-card__main">
                  <div className="task-card__title task-card__title--sm">{t.title}</div>
                  <div className="task-card__meta">
                    {t.area} · {t.estimated_minutes} min · {t.status}
                  </div>
                </div>
                <div className="task-card__aside">
                  <span className="task-card__when">{weekWhen(t.scheduled_start)}</span>
                  <Chip tone={prop.color} size="sm">{prop.name}</Chip>
                  {(t.priority === "high" || t.priority === "urgent") && <Dot tone="rust" />}
                  {t.photo_evidence === "required" && <Dot tone="sand" />}
                </div>
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
