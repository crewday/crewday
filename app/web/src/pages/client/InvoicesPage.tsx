import { useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { type ListEnvelope } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { formatMoney } from "@/lib/money";
import type { Me } from "@/types/api";

interface ClientInvoiceRow {
  id: string;
  organization_id: string;
  invoice_number: string;
  issued_at: string;
  due_at: string | null;
  total_cents: number;
  currency: string;
  status: "draft" | "submitted" | "approved" | "rejected" | "paid" | "voided" | string;
  proof_of_payment_file_ids: string[];
  pdf_url: string | null;
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

interface VendorInvoiceProofResponse {
  id: string;
  status: string;
  proof_of_payment_file_ids: string[];
}

function statusTone(status: string): "moss" | "sky" | "ghost" {
  return status === "paid" ? "moss" : status === "approved" ? "sky" : "ghost";
}

function invoiceDate(iso: string | null): string {
  return iso ?? "-";
}

function firstPropertyName(
  rows: ClientPortfolioRow[],
  organizationId: string,
): string | null {
  return rows.find((row) => row.organization_id === organizationId)?.name ?? null;
}

function ProofUploadButton({
  invoice,
  disabled,
  onUpload,
}: {
  invoice: ClientInvoiceRow;
  disabled: boolean;
  onUpload: (invoice: ClientInvoiceRow, file: File) => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  return (
    <>
      <input
        ref={inputRef}
        className="sr-only"
        type="file"
        accept="application/pdf,image/jpeg,image/png,image/webp"
        aria-label={`Upload proof for ${invoice.invoice_number}`}
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          event.currentTarget.value = "";
          if (file) onUpload(invoice, file);
        }}
      />
      <button
        type="button"
        className="btn btn--ghost btn--sm"
        onClick={() => inputRef.current?.click()}
        disabled={disabled}
      >
        Upload proof
      </button>
    </>
  );
}

// §22 — vendor invoices billed to one of the user's binding orgs
// (the orgs they hold a `client` grant for in the active workspace).
// Clients can upload proof-of-payment (appends to
// `proof_of_payment_file_ids`) but cannot mark anything paid — the
// workspace pushes funds and owns the paid bookkeeping flag.
export default function ClientInvoicesPage() {
  const qc = useQueryClient();
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const enabled = meQ.data?.role === "client";
  const invoicesQ = useQuery({
    queryKey: qk.clientInvoices(),
    queryFn: async () => {
      const env = await fetchJson<ListEnvelope<ClientInvoiceRow>>("/api/v1/client/invoices?limit=500");
      return env.data;
    },
    enabled,
  });
  const portfolioQ = useQuery({
    queryKey: qk.clientPortfolio(),
    queryFn: async () => {
      const env = await fetchJson<ListEnvelope<ClientPortfolioRow>>("/api/v1/client/portfolio?limit=500");
      return env.data;
    },
    enabled,
  });

  const uploadProof = useMutation({
    mutationFn: async ({ invoice, file }: { invoice: ClientInvoiceRow; file: File }) => {
      const body = new FormData();
      body.append("file", file);
      return fetchJson<VendorInvoiceProofResponse>(`/api/v1/billing/vendor-invoices/${invoice.id}/proof`, {
        method: "POST",
        body,
      });
    },
    onSuccess: (result, vars) => {
      qc.setQueryData<ClientInvoiceRow[]>(qk.clientInvoices(), (prev) =>
        prev?.map((row) =>
          row.id === vars.invoice.id
            ? {
                ...row,
                status: result.status,
                proof_of_payment_file_ids: result.proof_of_payment_file_ids,
              }
            : row,
        ),
      );
      qc.invalidateQueries({ queryKey: qk.clientInvoices() });
    },
  });

  if (meQ.isPending || (enabled && (invoicesQ.isPending || portfolioQ.isPending))) {
    return <DeskPage title="Invoices"><Loading /></DeskPage>;
  }
  if (meQ.isError || invoicesQ.isError || portfolioQ.isError) {
    return <DeskPage title="Invoices"><div className="panel"><p className="muted">Failed to load invoices.</p></div></DeskPage>;
  }

  const invoices = enabled ? (invoicesQ.data ?? []) : [];
  const portfolio = portfolioQ.data ?? [];

  return (
    <DeskPage
      title="Invoices"
      sub="Vendor invoices billed to your organization. Upload proof of payment once you've settled one — your agency will reconcile from their bank feed."
    >
      {invoices.length === 0 ? (
        <div className="panel">
          <p className="muted">No invoices billed to you yet.</p>
        </div>
      ) : (
        <div className="panel">
          <table className="table">
            <thead>
              <tr>
                <th>Invoice</th>
                <th>Property</th>
                <th>Total</th>
                <th>Status</th>
                <th>Billed</th>
                <th>Due</th>
                <th>Proof</th>
                <th>Reminder</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {invoices.map((v) => (
                <tr key={v.id}>
                  <td>{v.invoice_number}</td>
                  <td>{firstPropertyName(portfolio, v.organization_id) ?? v.organization_id}</td>
                  <td className="table__mono">{formatMoney(v.total_cents, v.currency)}</td>
                  <td>
                    <Chip
                      size="sm"
                      tone={statusTone(v.status)}
                    >
                      {v.status}
                    </Chip>
                  </td>
                  <td className="table__mono">{invoiceDate(v.issued_at)}</td>
                  <td className="table__mono muted">{invoiceDate(v.due_at)}</td>
                  <td>
                    {v.proof_of_payment_file_ids.length > 0 ? (
                      <Chip size="sm" tone="moss">
                        {v.proof_of_payment_file_ids.length} uploaded
                      </Chip>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td className="table__mono muted">—</td>
                  <td>
                    {v.status === "approved" ? (
                      <ProofUploadButton
                        invoice={v}
                        disabled={uploadProof.isPending}
                        onUpload={(invoice, file) => uploadProof.mutate({ invoice, file })}
                      />
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {uploadProof.error && (
        <div className="panel">
          <p className="muted">{uploadProof.error instanceof Error ? uploadProof.error.message : "Could not upload proof."}</p>
        </div>
      )}
    </DeskPage>
  );
}
