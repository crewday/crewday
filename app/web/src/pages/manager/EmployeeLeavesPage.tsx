import { useQuery } from "@tanstack/react-query";
import { CalendarOff } from "lucide-react";
import { Link, useLocation, useParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { workspaceRouteForPathname } from "@/lib/workspaceRoutes";
import DeskPage from "@/components/DeskPage";
import { Chip, EmptyState, Loading } from "@/components/common";
import type { Employee, Leave } from "@/types/api";
import { fmtDayMonYear, inclusiveDays } from "./leaveDisplay";

interface LeavesPayload {
  subject: Employee;
  leaves: Leave[];
}

export default function EmployeeLeavesPage() {
  const { eid = "" } = useParams<{ eid: string }>();
  const { pathname } = useLocation();
  const dataQ = useQuery({
    queryKey: qk.employeeLeaves(eid),
    queryFn: () => fetchJson<LeavesPayload>("/api/v1/employees/" + eid + "/leaves"),
    enabled: eid !== "",
  });

  if (dataQ.isPending) return <DeskPage title="Leave ledger"><Loading /></DeskPage>;
  if (!dataQ.data) return <DeskPage title="Leave ledger">Failed to load.</DeskPage>;

  const { subject, leaves } = dataQ.data;

  return (
    <DeskPage
      title={subject.name + " — leave ledger"}
      sub={
        <Link to={workspaceRouteForPathname(pathname, "/employee/" + subject.id)} className="link">
          ← Back to profile
        </Link>
      }
      actions={<button className="btn btn--moss">+ Add leave</button>}
    >
      <div className="panel">
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Dates</th>
              <th>Days</th>
              <th>Category</th>
              <th>Note</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {leaves.length === 0 ? (
              <tr>
                <td colSpan={6}>
                  <EmptyState
                    icon={CalendarOff}
                    title="No leave on file"
                    copy="Approved and pending leave for this employee will appear here."
                    variant="quiet"
                  />
                </td>
              </tr>
            ) : (
              leaves.map((lv) => (
                <tr key={lv.id}>
                  <td className="mono">
                    {fmtDayMonYear(lv.starts_on)} → {fmtDayMonYear(lv.ends_on)}
                  </td>
                  <td>{inclusiveDays(lv.starts_on, lv.ends_on)}</td>
                  <td><Chip tone="ghost" size="sm">{lv.category}</Chip></td>
                  <td className="table__sub">{lv.note}</td>
                  <td>
                    <Chip tone={lv.approved_at ? "moss" : "sand"} size="sm">
                      {lv.approved_at ? "Approved" : "Pending"}
                    </Chip>
                  </td>
                  <td>
                    {!lv.approved_at && (
                      <>
                        <button className="btn btn--sm btn--moss" type="button">Approve</button>{" "}
                        <button className="btn btn--sm btn--ghost" type="button">Reject</button>
                      </>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
