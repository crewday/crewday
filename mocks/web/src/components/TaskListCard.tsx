import { Link } from "react-router-dom";
import DateTime from "@/components/DateTime";
import { Chip, Dot } from "@/components/common";
import type { Property, Task } from "@/types/api";

// Compact split card used by the Today and Week task lists. Left side
// carries the title + meta (area · min [· status]); right side carries
// the scheduled time, property chip, and priority/photo dots.
export default function TaskListCard({
  task,
  property,
  showStatus = false,
}: {
  task: Task;
  property: Property | null;
  showWeekday?: boolean;
  showStatus?: boolean;
}) {
  const metaBase = task.area
    ? `${task.area} · ${task.estimated_minutes} min`
    : `${task.estimated_minutes} min`;
  const meta = showStatus ? `${metaBase} · ${task.status}` : metaBase;
  const cls =
    "task-card task-card--compact task-card--split" +
    (task.status === "completed" ? " task-card--done" : "") +
    (task.is_personal ? " task-card--personal" : "");

  return (
    <Link to={"/task/" + task.id} className={cls}>
      <div className="task-card__main">
        <div className="task-card__title task-card__title--sm">{task.title}</div>
        <div className="task-card__meta">{meta}</div>
      </div>
      <div className="task-card__aside">
        <DateTime value={task.scheduled_start} showTime className="task-card__when" />
        {property ? (
          <Chip tone={property.color} size="sm">{property.name}</Chip>
        ) : task.is_personal ? (
          <Chip tone="ghost" size="sm">Personal</Chip>
        ) : null}
        {(task.priority === "high" || task.priority === "urgent") && <Dot tone="rust" />}
        {task.photo_evidence === "required" && <Dot tone="sand" />}
      </div>
    </Link>
  );
}
