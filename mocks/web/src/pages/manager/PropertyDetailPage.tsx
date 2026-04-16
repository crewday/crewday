import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Avatar, Chip, Loading } from "@/components/common";
import type {
  Employee,
  Instruction,
  InventoryItem,
  Property,
  PropertyClosure,
  Stay,
  Task,
  TaskStatus,
} from "@/types/api";

interface PropertyDetail {
  property: Property;
  property_tasks: Task[];
  stays: Stay[];
  inventory: InventoryItem[];
  instructions: Instruction[];
  closures: PropertyClosure[];
}

const STATUS_TONE: Record<TaskStatus, "moss" | "sky" | "ghost" | "rust"> = {
  completed: "moss",
  in_progress: "sky",
  pending: "ghost",
  skipped: "rust",
};

function fmtDayMon(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

function fmtDayMonTime(iso: string): string {
  const d = new Date(iso);
  const date = d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return date + " · " + time;
}

export default function PropertyDetailPage() {
  const { pid = "" } = useParams<{ pid: string }>();
  const detailQ = useQuery({
    queryKey: qk.property(pid),
    queryFn: () => fetchJson<PropertyDetail>("/api/v1/properties/" + pid),
    enabled: pid !== "",
  });
  const empsQ = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });

  if (detailQ.isPending || empsQ.isPending) {
    return <DeskPage title="Property"><Loading /></DeskPage>;
  }
  if (!detailQ.data || !empsQ.data) {
    return <DeskPage title="Property">Failed to load.</DeskPage>;
  }

  const { property, property_tasks, stays } = detailQ.data;
  const empsById = new Map(empsQ.data.map((e) => [e.id, e]));

  return (
    <DeskPage
      title={property.name}
      sub={property.city + " · " + property.timezone}
      actions={
        <>
          <button className="btn btn--ghost">New task</button>
          <button className="btn btn--moss">Edit property</button>
        </>
      }
    >
      <nav className="tabs tabs--h">
        <a className="tab-link tab-link--active">Overview</a>
        <a className="tab-link">Areas</a>
        <a className="tab-link">Stays</a>
        <a className="tab-link">Inventory</a>
        <a className="tab-link">Instructions</a>
        <a className="tab-link">Closures</a>
      </nav>

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
                  <td><strong>{s.guest}</strong></td>
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
                        <Avatar initials={emp.avatar_initials} size="xs" />{" "}
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
    </DeskPage>
  );
}
