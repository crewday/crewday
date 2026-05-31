import { useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import DateTime from "@/components/DateTime";
import FileDropZone from "@/components/FileDropZone";
import { Chip, Loading } from "@/components/common";
import { fetchJson } from "@/lib/api";
import { formatDecimal, formatInteger } from "@/lib/numberFormat";
import { qk } from "@/lib/queryKeys";
import type {
  AssetDocument,
  DocumentExtraction,
  DocumentExtractionPage,
  DocumentKind,
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

interface DocumentUploadStripProps {
  scope: { kind: "asset" | "property"; id: string };
  onUploaded?: (documents: AssetDocument[]) => void;
}

interface QueuedDocument {
  id: string;
  file: File;
  kind: DocumentKind;
  title: string;
  notes: string;
}

const DOCUMENT_KIND_OPTIONS: { value: DocumentKind; label: string }[] = [
  { value: "manual", label: "Manual" },
  { value: "warranty", label: "Warranty" },
  { value: "invoice", label: "Invoice" },
  { value: "receipt", label: "Receipt" },
  { value: "photo", label: "Photo" },
  { value: "certificate", label: "Certificate" },
  { value: "contract", label: "Contract" },
  { value: "permit", label: "Permit" },
  { value: "insurance", label: "Insurance" },
  { value: "other", label: "Other" },
];

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

function defaultDocumentTitle(file: File): string {
  return file.name.replace(/\.[^.]+$/, "").trim() || file.name;
}

function scopePath(scope: DocumentUploadStripProps["scope"]): string {
  if (scope.kind === "property") return `/api/v1/properties/${scope.id}/documents`;
  return `/api/v1/assets/${scope.id}/documents`;
}

function scopeInvalidationKey(scope: DocumentUploadStripProps["scope"]) {
  if (scope.kind === "property") return qk.propertyDocuments(scope.id);
  return qk.asset(scope.id);
}

function uploadScopedDocument(
  scope: DocumentUploadStripProps["scope"],
  file: File,
  kind: DocumentKind,
  title: string,
  notes: string,
): Promise<AssetDocument> {
  const body = new FormData();
  body.append("category", kind);
  body.append("title", title.trim() || defaultDocumentTitle(file));
  body.append("notes_md", notes.trim());
  body.append("file", file);
  return fetchJson<AssetDocument>(scopePath(scope), { method: "POST", body });
}

function queuedDocumentId(file: File, index: number): string {
  return `${file.name}-${file.size}-${file.lastModified}-${index}-${Math.random().toString(36).slice(2)}`;
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
      if (doc.property_id) {
        queryClient.invalidateQueries({ queryKey: qk.propertyDocuments(doc.property_id), refetchType: "active" });
      }
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

export function DocumentUploadStrip({ scope, onUploaded }: DocumentUploadStripProps) {
  const queryClient = useQueryClient();
  const kindId = useId();
  const titleId = useId();
  const notesId = useId();
  const [kind, setKind] = useState<DocumentKind>("permit");
  const [title, setTitle] = useState("");
  const [notes, setNotes] = useState("");
  const [queued, setQueued] = useState<QueuedDocument[]>([]);
  const upload = useMutation({
    mutationFn: async (row: QueuedDocument) =>
      uploadScopedDocument(scope, row.file, row.kind, row.title, row.notes),
    onSuccess: async (document, row) => {
      setQueued((current) => current.filter((item) => item.id !== row.id));
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: qk.documents() }),
        queryClient.invalidateQueries({ queryKey: scopeInvalidationKey(scope) }),
      ]);
      onUploaded?.([document]);
    },
  });

  function queueFiles(files: File[]): void {
    setQueued((current) => [
      ...current,
      ...files.map((file, index) => ({
        id: queuedDocumentId(file, index),
        file,
        kind,
        title: title.trim() || defaultDocumentTitle(file),
        notes,
      })),
    ]);
    setTitle("");
    setNotes("");
  }

  function updateQueued(id: string, patch: Partial<Pick<QueuedDocument, "kind" | "title" | "notes">>): void {
    setQueued((current) => current.map((row) => (row.id === id ? { ...row, ...patch } : row)));
  }

  function removeQueued(id: string): void {
    setQueued((current) => current.filter((row) => row.id !== id));
  }

  return (
    <section className="document-upload" aria-label="Upload document">
      <div className="document-upload__fields">
        <label className="field document-upload__field" htmlFor={kindId}>
          <span>Kind</span>
          <select
            id={kindId}
            value={kind}
            onChange={(event) => setKind(event.target.value as DocumentKind)}
            disabled={upload.isPending}
          >
            {DOCUMENT_KIND_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </label>
        <label className="field document-upload__field" htmlFor={titleId}>
          <span>Title</span>
          <input
            id={titleId}
            type="text"
            aria-label="Document title"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Use filename"
            disabled={upload.isPending}
          />
        </label>
        <label className="field document-upload__field" htmlFor={notesId}>
          <span>Notes</span>
          <input
            id={notesId}
            type="text"
            aria-label="Document notes"
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
            placeholder="Optional"
            disabled={upload.isPending}
          />
        </label>
      </div>
      <FileDropZone
        title="Stage document"
        description="Choose a file, review its row, then upload it."
        inputLabel={scope.kind === "property" ? "Upload property document" : "Upload asset document"}
        disabled={upload.isPending}
        onFiles={queueFiles}
      />
      {queued.length > 0 ? (
        <div className="document-upload__queue" aria-label="Queued documents">
          {queued.map((row) => (
            <div key={row.id} className="document-upload__row">
              <span className="document-upload__file">
                <strong>{row.file.name}</strong>
                <span className="muted">{Math.max(1, Math.round(row.file.size / 1024))} KB</span>
              </span>
              <label className="field document-upload__field">
                <span>Kind</span>
                <select
                  aria-label={`Kind for ${row.file.name}`}
                  value={row.kind}
                  disabled={upload.isPending}
                  onChange={(event) => updateQueued(row.id, { kind: event.target.value as DocumentKind })}
                >
                  {DOCUMENT_KIND_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </label>
              <label className="field document-upload__field">
                <span>Title</span>
                <input
                  type="text"
                  aria-label={`Title for ${row.file.name}`}
                  value={row.title}
                  disabled={upload.isPending}
                  onChange={(event) => updateQueued(row.id, { title: event.target.value })}
                />
              </label>
              <label className="field document-upload__field">
                <span>Notes</span>
                <input
                  type="text"
                  aria-label={`Notes for ${row.file.name}`}
                  value={row.notes}
                  disabled={upload.isPending}
                  onChange={(event) => updateQueued(row.id, { notes: event.target.value })}
                />
              </label>
              <button
                className="btn btn--sm btn--moss"
                type="button"
                disabled={upload.isPending}
                onClick={() => upload.mutate(row)}
              >
                {upload.isPending ? "Uploading\u2026" : "Upload"}
              </button>
              <button
                className="btn btn--sm btn--ghost"
                type="button"
                disabled={upload.isPending}
                onClick={() => removeQueued(row.id)}
              >
                Remove
              </button>
            </div>
          ))}
        </div>
      ) : null}
      {upload.isError ? (
        <p className="form-error" role="alert">
          {upload.error instanceof Error ? upload.error.message : "Document upload failed."}
        </p>
      ) : null}
    </section>
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
