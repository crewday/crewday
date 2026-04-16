import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Avatar, Chip, Loading } from "@/components/common";
import type { Employee, Leave } from "@/types/api";

interface LeavesPayload {
  pending: Leave[];
  approved: Leave[];
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

function rangeDays(starts: string, ends: string): number {
  const ms = new Date(ends).getTime() - new Date(starts).getTime();
  return Math.floor(ms / (24 * 60 * 60 * 1000)) + 1;
}

export default function LeavesInboxPage() {
  const qc = useQueryClient();
  const leaves = useQuery({
    queryKey: qk.leaves(),
    queryFn: () => fetchJson<LeavesPayload>("/api/v1/leaves"),
  });
  const employees = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });

  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "reject" }) =>
      fetchJson("/api/v1/leaves/" + id + "/" + decision, { method: "POST" }),
    onMutate: async ({ id }) => {
      await qc.cancelQueries({ queryKey: qk.leaves() });
      const prev = qc.getQueryData<LeavesPayload>(qk.leaves());
      if (prev) {
        qc.setQueryData<LeavesPayload>(qk.leaves(), {
          pending: prev.pending.filter((lv) => lv.id !== id),
          approved: prev.approved,
        });
      }
      return { prev };
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(qk.leaves(), ctx.prev);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.leaves() });
      qc.invalidateQueries({ queryKey: qk.dashboard() });
    },
  });

  const sub = "Approve or reject time-off requests. Approved leave drops the employee out of assignment.";

  if (leaves.isPending || employees.isPending) {
    return <DeskPage title="Leaves" sub={sub}><Loading /></DeskPage>;
  }
  if (!leaves.data || !employees.data) {
    return <DeskPage title="Leaves" sub={sub}>Failed to load.</DeskPage>;
  }

  const empById = new Map(employees.data.map((e) => [e.id, e]));
  const { pending, approved } = leaves.data;

  return (
    <DeskPage title="Leaves" sub={sub}>
      <div className="panel">
        <header className="panel__head">
          <h2>Pending · {pending.length}</h2>
        </header>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Employee</th><th>Dates</th><th>Days</th><th>Category</th><th>Note</th><th></th>
            </tr>
          </thead>
          <tbody>
            {pending.length === 0 && (
              <tr><td colSpan={6} className="empty-state empty-state--quiet">Inbox zero. Nice.</td></tr>
            )}
            {pending.map((lv) => {
              const emp = empById.get(lv.employee_id);
              return (
                <tr key={lv.id}>
                  <td>
                    {emp && <><Avatar initials={emp.avatar_initials} size="xs" /> {emp.name}</>}
                  </td>
                  <td className="mono">{fmtDate(lv.starts_on)} → {fmtDate(lv.ends_on)}</td>
                  <td>{rangeDays(lv.starts_on, lv.ends_on)}</td>
                  <td><Chip tone="ghost" size="sm">{lv.category}</Chip></td>
                  <td className="table__sub">{lv.note}</td>
                  <td>
                    <button
                      className="btn btn--sm btn--moss"
                      type="button"
                      onClick={() => decide.mutate({ id: lv.id, decision: "approve" })}
                    >
                      Approve
                    </button>{" "}
                    <button
                      className="btn btn--sm btn--ghost"
                      type="button"
                      onClick={() => decide.mutate({ id: lv.id, decision: "reject" })}
                    >
                      Reject
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head"><h2>Approved (upcoming)</h2></header>
        <table className="table">
          <thead>
            <tr><th>Employee</th><th>Dates</th><th>Category</th><th>Note</th></tr>
          </thead>
          <tbody>
            {approved.map((lv) => {
              const emp = empById.get(lv.employee_id);
              return (
                <tr key={lv.id}>
                  <td>
                    {emp && <><Avatar initials={emp.avatar_initials} size="xs" /> {emp.name}</>}
                  </td>
                  <td className="mono">{fmtDate(lv.starts_on)} → {fmtDate(lv.ends_on)}</td>
                  <td><Chip tone="ghost" size="sm">{lv.category}</Chip></td>
                  <td className="table__sub">{lv.note}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
