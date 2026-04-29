import { Avatar, Chip } from "@/components/common";
import type { Employee, TaskStatus } from "@/types/api";
import { fmtDayMon, fmtDayMonTime } from "./lib/propertyFormatters";
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
      </div>

      <div className="panel">
        <header className="panel__head"><h2>Tasks for this property</h2></header>
        <ul className="task-list task-list--desk">
          {property_tasks.map((t) => {
            const emp = empsById.get(t.assignee_id);
            return (
              <li key={t.id} className="task-row">
                <span className="task-row__time table__mono">
                  {fmtDayMonTime(t.scheduled_start)}
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
      </div>
    </section>
  );
}
