import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import DateTime from "@/components/DateTime";
import { Chip, Loading } from "@/components/common";
import { fetchJson } from "@/lib/api";
import { formatDecimal, formatInteger } from "@/lib/numberFormat";
import { qk } from "@/lib/queryKeys";
import type {
  AssetDocument,
  DocumentExtraction,
  DocumentExtractionPage,
  FileExtractionStatus,
  PropertyColor,
} from "@/types/api";

interface DocumentLibraryAsset {
  id: string;
  name: string;
}

interface DocumentLibraryProperty {
  id: string;
  name: string;
  color: PropertyColor;
}

interface DocumentLibraryTableProps {
  documents: AssetDocument[];
  assetsById?: ReadonlyMap<string, DocumentLibraryAsset>;
  propertiesById?: ReadonlyMap<string, DocumentLibraryProperty>;
  showAsset?: boolean;
  showProperty?: boolean;
  showAmount?: boolean;
}

const WARN_CUTOFF = "2026-05-15";

const EXTRACTION_TONE: Record<FileExtractionStatus, "moss" | "rust" | "sand" | "ghost"> = {
  pending: "ghost",
  extracting: "ghost",
  succeeded: "moss",
  failed: "rust",
  unsupported: "sand",
  empty: "ghost",
};

const EXTRACTION_LABEL: Record<FileExtractionStatus, string> = {
  pending: "queued",
  extracting: "extracting\u2026",
  succeeded: "indexed",
  failed: "failed",
  unsupported: "unsupported",
  empty: "no text",
};

function fmtDate(iso: string | null): string {
  if (!iso) return "\u2014";
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

function fmtCents(cents: number | null, currency: string | null): string {
  if (cents == null) return "\u2014";
  return `${formatDecimal(cents / 100, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ${currency ?? "EUR"}`;
}

function fmtNumber(n: number | null | undefined): string {
  return formatInteger(n, { locale: "en-GB", fallback: "\u2014" });
}

function fmtExtractor(extractor: DocumentExtraction["extractor"]): string {
  return extractor ? extractor.replace("_", " ") : "\u2014";
}

function DocumentTextDisclosure({ documentId }: { documentId: string }) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(1);
  const pageQ = useQuery({
    queryKey: qk.documentExtractionPage(documentId, page),
    queryFn: () =>
      fetchJson<DocumentExtractionPage>(
        `/api/v1/documents/${documentId}/extraction/pages/${page}`,
      ),
    enabled: open,
  });

  return (
    <details
      className="extraction-text"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>Extracted text</summary>
      {pageQ.isPending ? (
        <Loading />
      ) : !pageQ.data ? (
        <p className="muted">Failed to load.</p>
      ) : (
        <div className="extraction-text__body">
          <pre>{pageQ.data.body}</pre>
          <div className="extraction-text__footer">
            <span className="mono muted">Page {pageQ.data.page}</span>
            {page > 1 ? (
              <button className="btn btn--ghost" type="button" onClick={() => setPage((p) => p - 1)}>
                Previous
              </button>
            ) : null}
            {pageQ.data.more_pages ? (
              <button className="btn btn--ghost" type="button" onClick={() => setPage((p) => p + 1)}>
                Next
              </button>
            ) : null}
          </div>
        </div>
      )}
    </details>
  );
}

function ExtractionDisclosure({ doc }: { doc: AssetDocument }) {
  // code-health: ignore[nloc] Extraction disclosure keeps retry invalidation and extraction detail layout together.
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const extractionQ = useQuery({
    queryKey: qk.documentExtraction(doc.id),
    queryFn: () => fetchJson<DocumentExtraction>(`/api/v1/documents/${doc.id}/extraction`),
    enabled: open,
  });
  const retry = useMutation({
    mutationFn: () => fetchJson<void>(`/api/v1/documents/${doc.id}/extraction/retry`, { method: "POST" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: qk.documents(), refetchType: "active" });
      queryClient.invalidateQueries({ queryKey: qk.documentExtraction(doc.id), refetchType: "active" });
      queryClient.invalidateQueries({ queryKey: qk.documentExtractionPages(doc.id), refetchType: "active" });
      if (doc.asset_id) {
        queryClient.invalidateQueries({ queryKey: qk.asset(doc.asset_id), refetchType: "active" });
      }
    },
  });

  return (
    <details
      className="extraction-disclosure"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary aria-label={`${doc.title} extraction details`}>
        <Chip tone={EXTRACTION_TONE[doc.extraction_status]} size="sm">
          {EXTRACTION_LABEL[doc.extraction_status]}
        </Chip>
      </summary>
      <div className="extraction-disclosure__body">
        {extractionQ.isPending ? (
          <Loading />
        ) : !extractionQ.data ? (
          <p className="muted">Failed to load.</p>
        ) : (
          <>
            <dl className="extraction-disclosure__grid">
              <div>
                <dt>Extractor</dt>
                <dd>{fmtExtractor(extractionQ.data.extractor)}</dd>
              </div>
              <div>
                <dt>Pages</dt>
                <dd>{fmtNumber(extractionQ.data.page_count)}</dd>
              </div>
              <div>
                <dt>Tokens</dt>
                <dd>{fmtNumber(extractionQ.data.token_count)}</dd>
              </div>
              <div>
                <dt>Extracted</dt>
                <dd><DateTime value={extractionQ.data.extracted_at} showTime /></dd>
              </div>
            </dl>
            {extractionQ.data.has_secret_marker ? (
              <p className="extraction-disclosure__warning">
                Extraction found a value that looks like a password or access code. The agent will not see the original; you may want to re-upload a less sensitive version.
              </p>
            ) : null}
            {extractionQ.data.last_error ? (
              <p className="muted">Last error: {extractionQ.data.last_error}</p>
            ) : null}
            <DocumentTextDisclosure documentId={doc.id} />
            <div className="extraction-disclosure__actions">
              <button
                className="btn btn--ghost"
                type="button"
                disabled={retry.isPending}
                onClick={() => retry.mutate()}
              >
                {retry.isPending ? "Retrying\u2026" : "Retry"}
              </button>
              {retry.isError ? <span className="muted">Retry failed.</span> : null}
            </div>
          </>
        )}
      </div>
    </details>
  );
}

export default function DocumentLibraryTable({
  documents,
  assetsById,
  propertiesById,
  showAsset = Boolean(assetsById),
  showProperty = Boolean(propertiesById),
  showAmount = true,
}: DocumentLibraryTableProps) {
  return (
    <table className="table document-library">
      <thead>
        <tr>
          <th>Title</th>
          <th>Kind</th>
          {showProperty ? <th>Property</th> : null}
          {showAsset ? <th>Asset</th> : null}
          <th>Size</th>
          <th>Expires</th>
          {showAmount ? <th>Amount</th> : null}
          <th>Extraction</th>
        </tr>
      </thead>
      <tbody>
        {documents.map((doc) => {
          const prop = propertiesById?.get(doc.property_id);
          const asset = doc.asset_id ? assetsById?.get(doc.asset_id) : null;
          const expiresSoon = doc.expires_on != null && doc.expires_on < WARN_CUTOFF;
          return (
            <tr key={doc.id} className={expiresSoon ? "row--warn" : ""}>
              <td>
                <strong>{doc.title}</strong>
                <span className="table__sub">{doc.filename}</span>
              </td>
              <td><Chip tone="ghost" size="sm">{doc.kind}</Chip></td>
              {showProperty ? (
                <td>{prop ? <Chip tone={prop.color} size="sm">{prop.name}</Chip> : <span className="muted">{"\u2014"}</span>}</td>
              ) : null}
              {showAsset ? (
                <td>{asset ? asset.name : <span className="muted">{"\u2014"}</span>}</td>
              ) : null}
              <td className="mono muted">{doc.size_kb} KB</td>
              <td>{fmtDate(doc.expires_on)}</td>
              {showAmount ? <td>{fmtCents(doc.amount_cents, doc.amount_currency)}</td> : null}
              <td><ExtractionDisclosure doc={doc} /></td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
