import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, EmptyState, Loading } from "@/components/common";
import { fmtTime } from "@/lib/dates";
import type { ApprovalRequest } from "@/types/api";

const RISK_TONE: Record<ApprovalRequest["risk"], "sky" | "sand" | "rust"> = {
  low: "sky",
  medium: "sand",
  high: "rust",
};

export default function ApprovalsPage() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: qk.approvals(),
    queryFn: () => fetchJson<ApprovalRequest[]>("/api/v1/approvals"),
  });

  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "reject" }) =>
      fetchJson("/api/v1/approvals/" + id + "/" + decision, { method: "POST" }),
    onMutate: async ({ id }) => {
      await qc.cancelQueries({ queryKey: qk.approvals() });
      const prev = qc.getQueryData<ApprovalRequest[]>(qk.approvals());
      if (prev) qc.setQueryData<ApprovalRequest[]>(qk.approvals(), prev.filter((a) => a.id !== id));
      return { prev };
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(qk.approvals(), ctx.prev);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.approvals() });
      qc.invalidateQueries({ queryKey: qk.dashboard() });
    },
  });

  const sub = "Actions an LLM agent has proposed — review before they happen.";

  if (q.isPending) return <DeskPage title="Agent approvals" sub={sub}><Loading /></DeskPage>;
  if (!q.data) return <DeskPage title="Agent approvals" sub={sub}>Failed to load.</DeskPage>;

  const approvals = q.data;

  return (
    <DeskPage title="Agent approvals" sub={sub}>
      <div className="panel">
        <ul className="approval-list approval-list--wide">
          {approvals.length === 0 && (
            <li><EmptyState>Nothing to review — agents are behaving.</EmptyState></li>
          )}
          {approvals.map((a) => (
            <li key={a.id} className={"approval approval--" + a.risk}>
              <div className="approval__head">
                <Chip tone="ghost" size="sm">{a.agent}</Chip>
                <Chip tone={RISK_TONE[a.risk]} size="sm">{a.risk} risk</Chip>
                <span className="approval__time">requested {fmtTime(a.requested_at)}</span>
              </div>
              <div className="approval__title"><strong>{a.action}</strong> — {a.target}</div>
              <p className="approval__reason">{a.reason}</p>
              <div className="approval__actions">
                <button
                  className="btn btn--moss"
                  type="button"
                  onClick={() => decide.mutate({ id: a.id, decision: "approve" })}
                >
                  Approve
                </button>
                <button
                  className="btn btn--ghost"
                  type="button"
                  onClick={() => decide.mutate({ id: a.id, decision: "reject" })}
                >
                  Reject
                </button>
                <button className="btn btn--ghost" type="button">Ask for details</button>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </DeskPage>
  );
}
