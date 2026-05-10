import { ClipboardList, CalendarClock } from "lucide-react";
import DateTime from "@/components/DateTime";
import { Avatar, Chip, EmptyState } from "@/components/common";
import type { Employee, TaskStatus } from "@/types/api";
import { fmtDayMon } from "./lib/propertyFormatters";
import type { PropertyDetail } from "./types";

const STATUS_TONE: Record<TaskStatus, "moss" | "sky" | "ghost" | "rust"> = {
  completed: "moss",
  in_progress: "sky",
  pending: "ghost",
  scheduled: "ghost",
  skipped: "rust",
  cancelled: "rust",
  overdue: "rust",
};

export default function OverviewPanel({
  detail,
  employees,
}: {
  detail: PropertyDetail;
  employees: Employee[];
}) {
  const { property_tasks, stays } = detail;
  const empsById = new Map(employees.map((e) => [e.id, e]));

  return (
    <section className="grid grid--split">
      <div className="panel">
        <header className="panel__head"><h2>Upcoming stays</h2></header>
        {stays.length > 0 ? (
          <table className="table">
            <thead>
              <tr>
                <th>Guest</th><th>Source</th><th>In</th><th>Out</th><th>Guests</th>
              </tr>
            </thead>
            <tbody>
              {stays.map((s) => (
                <tr key={s.id}>
                  <td><strong>{s.guest_name}</strong></td>
                  <td>{s.source}</td>
                  <td className="table__mono">{fmtDayMon(s.check_in)}</td>
                  <td className="table__mono">{fmtDayMon(s.check_out)}</td>
                  <td>{s.guests}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState
            glyph={<CalendarClock size={22} strokeWidth={2} />}
            title="No upcoming stays"
            copy="New reservations for this property will appear here with guest, source, and date details."
            variant="compact"
          />
        )}
      </div>

      <div className="panel">
        <header className="panel__head"><h2>Tasks for this property</h2></header>
        {property_tasks.length > 0 ? (
          <ul className="task-list task-list--desk">
            {property_tasks.map((t) => {
              const emp = empsById.get(t.assignee_id);
              return (
                <li key={t.id} className="task-row">
                  <span className="task-row__time table__mono">
                    <DateTime value={t.scheduled_start} showTime />
                  </span>
                  <span className="task-row__title">
                    <strong>{t.title}</strong>
                    <span className="task-row__area">{t.area}</span>
                  </span>
                  <span className="task-row__assignee">
                    {emp && (
                      <>
                        <Avatar url={emp.avatar_url} initials={emp.avatar_initials} size="xs" alt={emp.name} />{" "}
                        {emp.name.split(" ")[0]}
                      </>
                    )}
                  </span>
                  <Chip tone={STATUS_TONE[t.status]} size="sm">{t.status}</Chip>
                </li>
              );
            })}
          </ul>
        ) : (
          <EmptyState
            glyph={<ClipboardList size={22} strokeWidth={2} />}
            title="No tasks scheduled"
            copy="Property tasks will land here once cleanings, inspections, or maintenance work are assigned."
            variant="compact"
          />
        )}
      </div>
    </section>
  );
}
