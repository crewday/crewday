import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { fmtTime } from "@/lib/dates";
import type { Employee, Property } from "@/types/api";

export default function EmployeesPage() {
  const empsQ = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  if (empsQ.isPending || propsQ.isPending) {
    return (
      <DeskPage title="Employees" actions={<button className="btn btn--moss">+ Invite employee</button>}>
        <Loading />
      </DeskPage>
    );
  }
  if (!empsQ.data || !propsQ.data) {
    return (
      <DeskPage title="Employees" actions={<button className="btn btn--moss">+ Invite employee</button>}>
        Failed to load.
      </DeskPage>
    );
  }

  const employees = empsQ.data;
  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));

  return (
    <DeskPage
      title="Employees"
      actions={<button className="btn btn--moss">+ Invite employee</button>}
    >
      <div className="panel">
        <table className="table table--roomy">
          <thead>
            <tr>
              <th></th>
              <th>Name</th>
              <th>Roles</th>
              <th>Properties</th>
              <th>Phone</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {employees.map((e) => (
              <tr key={e.id}>
                <td><div className="avatar avatar--md">{e.avatar_initials}</div></td>
                <td>
                  <Link className="link" to={"/employee/" + e.id}>{e.name}</Link>
                </td>
                <td>
                  {e.roles.map((r) => (
                    <Chip key={r} tone="ghost" size="sm">{r}</Chip>
                  ))}
                </td>
                <td>
                  {e.properties.map((pid) => {
                    const p = propsById.get(pid);
                    if (!p) return null;
                    return <Chip key={pid} tone={p.color} size="sm">{p.name}</Chip>;
                  })}
                </td>
                <td className="table__mono">{e.phone}</td>
                <td>
                  {e.clocked_in_at ? (
                    <Chip tone="moss" size="sm">On shift · {fmtTime(e.clocked_in_at)}</Chip>
                  ) : (
                    <Chip tone="ghost" size="sm">Off shift</Chip>
                  )}
                </td>
                <td>
                  <Link className="link link--muted" to={"/employee/" + e.id}>View →</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
