import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, FilterChipGroup, Loading } from "@/components/common";
import type {
  Asset,
  AssetDocument,
  DocumentExtraction,
  DocumentExtractionPage,
  DocumentKind,
  FileExtractionStatus,
  Property,
} from "@/types/api";

const KIND_ICON: Record<DocumentKind, string> = {
  manual: "\u{1F4D6}",
  warranty: "\u{1F6E1}\uFE0F",
  invoice: "\u{1F9FE}",
  receipt: "\u{1F9FE}",
  photo: "\u{1F4F7}",
  certificate: "\u{1F4DC}",
  contract: "\u{1F4DD}",
  permit: "\u{1F4CB}",
  insurance: "\u{1F3E6}",
  other: "\u{1F4C4}",
};

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

interface ListEnvelope<T> {
  data: T[];
}

function unwrapList<T>(payload: T[] | ListEnvelope<T>): T[] {
  return Array.isArray(payload) ? payload : payload.data;
}

async function fetchList<T>(path: string): Promise<T[]> {
  return unwrapList(await fetchJson<T[] | ListEnvelope<T>>(path));
}

function fmtDate(iso: string | null): string {
  if (!iso) return "\u2014";
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

function fmtCents(cents: number | null, currency: string | null): string {
  if (cents == null) return "\u2014";
  return (cents / 100).toFixed(2) + " " + (currency ?? "EUR");
}

function fmtNumber(n: number | null | undefined): string {
  return n == null ? "\u2014" : n.toLocaleString("en-GB");
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
                <dd>{fmtDate(extractionQ.data.extracted_at)}</dd>
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

export default function DocumentsPage() {
  const [activeKind, setActiveKind] = useState<DocumentKind | "">("");
  const [activeProperty, setActiveProperty] = useState<string>("");

  const docsQ = useQuery({
    queryKey: qk.documents(),
    queryFn: () => fetchList<AssetDocument>("/api/v1/documents"),
  });
  const assetsQ = useQuery({
    queryKey: qk.assets(),
    queryFn: () => fetchList<Asset>("/api/v1/assets"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchList<Property>("/api/v1/properties"),
  });

  const sub = "Manuals, warranties, invoices, and permits across all properties.";

  if (docsQ.isPending || assetsQ.isPending || propsQ.isPending) {
    return <DeskPage title="Documents" sub={sub}><Loading /></DeskPage>;
  }
  if (!docsQ.data || !assetsQ.data || !propsQ.data) {
    return <DeskPage title="Documents" sub={sub}>Failed to load.</DeskPage>;
  }

  const assetsById = new Map(assetsQ.data.map((a) => [a.id, a]));
  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));

  const kinds = Array.from(new Set(docsQ.data.map((d) => d.kind)));

  const filtered = docsQ.data.filter((d) => {
    if (activeKind && d.kind !== activeKind) return false;
    if (activeProperty && d.property_id !== activeProperty) return false;
    return true;
  });

  return (
    <DeskPage title="Documents" sub={sub}>
      <section className="panel">
        <FilterChipGroup
          value={activeKind}
          onChange={setActiveKind}
          options={kinds.map((k) => ({ value: k, label: k }))}
        />
        <FilterChipGroup
          value={activeProperty}
          onChange={setActiveProperty}
          allLabel="All properties"
          options={propsQ.data.map((p) => ({ value: p.id, label: p.name, tone: p.color }))}
        />

        <table className="table">
          <thead>
            <tr>
              <th>Title</th>
              <th>Kind</th>
              <th>Property</th>
              <th>Asset</th>
              <th>Size</th>
              <th>Expires</th>
              <th>Amount</th>
              <th>Extraction</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((doc) => {
              const prop = propsById.get(doc.property_id);
              const asset = doc.asset_id ? assetsById.get(doc.asset_id) : null;
              const expiresSoon = doc.expires_on != null && doc.expires_on < WARN_CUTOFF;
              return (
                <tr key={doc.id} className={expiresSoon ? "row--warn" : ""}>
                  <td>
                    <strong>{doc.title}</strong>
                    <span className="table__sub">{doc.filename}</span>
                  </td>
                  <td>
                    <span className="doc-thumb">{KIND_ICON[doc.kind]}</span>{" "}
                    <Chip tone="ghost" size="sm">{doc.kind}</Chip>
                  </td>
                  <td>{prop && <Chip tone={prop.color} size="sm">{prop.name}</Chip>}</td>
                  <td>{asset ? asset.name : <span className="muted">{"\u2014"}</span>}</td>
                  <td className="mono muted">{doc.size_kb} KB</td>
                  <td>{fmtDate(doc.expires_on)}</td>
                  <td>{fmtCents(doc.amount_cents, doc.amount_currency)}</td>
                  <td>
                    <ExtractionDisclosure doc={doc} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </DeskPage>
  );
}
