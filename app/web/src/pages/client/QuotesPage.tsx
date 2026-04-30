import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { ApiError, fetchJson } from "@/lib/api";
import { type ListEnvelope } from "@/lib/listResponse";
import { formatMoney } from "@/lib/money";
import { qk } from "@/lib/queryKeys";
import type { Me } from "@/types/api";

type ClientQuoteStatus = "sent" | "accepted" | "rejected" | "expired" | string;

interface ClientQuoteRow {
  id: string;
  organization_id: string;
  property_id: string;
  title: string;
  total_cents: number;
  currency: string;
  status: ClientQuoteStatus;
  sent_at: string | null;
  decided_at: string | null;
  accept_url: string | null;
}

interface ClientPortfolioRow {
  id: string;
  organization_id: string;
  organization_name: string | null;
  name: string;
  kind: string;
  address: string;
  country: string;
  timezone: string;
  default_currency: string | null;
}

type Decision = "accept" | "reject";

interface PendingApprovalResponse {
  status: "pending_approval";
  approval_request_id: string;
}

function isPendingApprovalResponse(value: unknown): value is PendingApprovalResponse {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { status?: unknown }).status === "pending_approval" &&
    typeof (value as { approval_request_id?: unknown }).approval_request_id === "string"
  );
}

function decidedLabel(iso: string | null): string {
  return iso ? new Date(iso).toLocaleDateString() : "-";
}

function statusTone(status: string): "moss" | "rust" | "sky" | "sand" {
  if (status === "accepted") return "moss";
  if (status === "rejected" || status === "expired") return "rust";
  if (status === "pending_approval") return "sand";
  return "sky";
}

function statusLabel(status: string): string {
  return status === "pending_approval" ? "pending approval" : status;
}

export default function ClientQuotesPage() {
  const queryClient = useQueryClient();
  const [pendingApprovalByQuote, setPendingApprovalByQuote] = useState<Record<string, string>>({});
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const portfolioQ = useQuery({
    queryKey: qk.clientPortfolio(),
    queryFn: async () => {
      const env = await fetchJson<ListEnvelope<ClientPortfolioRow>>("/api/v1/client/portfolio?limit=500");
      return env.data;
    },
    enabled: meQ.data?.role === "client",
  });
  const quotesQ = useQuery({
    queryKey: qk.clientQuotes(),
    queryFn: async () => {
      const env = await fetchJson<ListEnvelope<ClientQuoteRow>>("/api/v1/client/quotes?limit=500");
      return env.data;
    },
    enabled: meQ.data?.role === "client",
  });

  const propertiesById = useMemo(
    () => new Map((portfolioQ.data ?? []).map((property) => [property.id, property])),
    [portfolioQ.data],
  );

  const decide = useMutation({
    mutationFn: async (vars: { quote: ClientQuoteRow; decision: Decision }) => {
      const path =
        vars.decision === "accept"
          ? (vars.quote.accept_url ?? "/api/v1/billing/quotes/" + vars.quote.id + "/accept")
          : "/api/v1/billing/quotes/" + vars.quote.id + "/reject";
      try {
        return await fetchJson<ClientQuoteRow | PendingApprovalResponse>(path, { method: "POST" });
      } catch (error) {
        if (error instanceof ApiError && error.problem?.approval_request_id) {
          return {
            status: "pending_approval",
            approval_request_id: error.problem.approval_request_id,
          } satisfies PendingApprovalResponse;
        }
        throw error;
      }
    },
    onSuccess: (result, vars) => {
      if (isPendingApprovalResponse(result)) {
        setPendingApprovalByQuote((prev) => ({
          ...prev,
          [vars.quote.id]: result.approval_request_id,
        }));
        void queryClient.invalidateQueries({ queryKey: qk.approvals() });
      } else {
        setPendingApprovalByQuote((prev) => {
          const next = { ...prev };
          delete next[vars.quote.id];
          return next;
        });
      }
      void queryClient.invalidateQueries({ queryKey: qk.clientQuotes() });
      void queryClient.invalidateQueries({ queryKey: qk.workOrders() });
    },
  });

  if (meQ.isPending || (meQ.data?.role === "client" && (portfolioQ.isPending || quotesQ.isPending))) {
    return <DeskPage title="Quotes"><Loading /></DeskPage>;
  }
  if (meQ.isError || portfolioQ.isError || quotesQ.isError) {
    return <DeskPage title="Quotes"><div className="panel"><p className="muted">Failed to load quotes.</p></div></DeskPage>;
  }

  const quotes = meQ.data?.role === "client" ? (quotesQ.data ?? []) : [];

  return (
    <DeskPage
      title="Quotes"
      sub="Work orders awaiting your decision."
    >
      {quotes.length === 0 ? (
        <div className="panel">
          <p className="muted">No open quotes.</p>
        </div>
      ) : (
        <div className="panel">
          <table className="table">
            <thead>
              <tr><th>Property</th><th>Work order</th><th>Total</th><th>Status</th><th>Decided</th><th></th></tr>
            </thead>
            <tbody>
              {quotes.map((quote) => {
                const property = propertiesById.get(quote.property_id);
                const pendingApprovalId = pendingApprovalByQuote[quote.id];
                const visibleStatus = pendingApprovalId && quote.status === "sent" ? "pending_approval" : quote.status;
                return (
                  <tr key={quote.id}>
                    <td>{property?.name ?? quote.property_id}</td>
                    <td>
                      <strong>{quote.title}</strong>
                    </td>
                    <td className="table__mono">{formatMoney(quote.total_cents, quote.currency)}</td>
                    <td>
                      <Chip
                        size="sm"
                        tone={statusTone(visibleStatus)}
                      >
                        {statusLabel(visibleStatus)}
                      </Chip>
                    </td>
                    <td className="table__mono muted">{decidedLabel(quote.decided_at)}</td>
                    <td>
                      {quote.status === "sent" && !pendingApprovalId && (
                        <div className="row-actions">
                          <button
                            type="button"
                            className="btn btn--ghost btn--sm"
                            disabled={decide.isPending}
                            onClick={() => decide.mutate({ quote, decision: "reject" })}
                          >
                            Reject
                          </button>
                          <button
                            type="button"
                            className="btn btn--moss btn--sm"
                            disabled={decide.isPending}
                            onClick={() => decide.mutate({ quote, decision: "accept" })}
                          >
                            Accept
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {decide.error && (
        <div className="panel">
          <p className="muted">{decide.error instanceof Error ? decide.error.message : "Could not update quote."}</p>
        </div>
      )}
    </DeskPage>
  );
}
