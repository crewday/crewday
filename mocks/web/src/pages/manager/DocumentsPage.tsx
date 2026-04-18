import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, FilterChipGroup, Loading } from "@/components/common";
import type {
  Asset,
  AssetDocument,
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
  extracting: "extracting…",
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
  return (cents / 100).toFixed(2) + " " + (currency ?? "EUR");
}

export default function DocumentsPage() {
  const [activeKind, setActiveKind] = useState<DocumentKind | "">("");
  const [activeProperty, setActiveProperty] = useState<string>("");

  const docsQ = useQuery({
    queryKey: qk.documents(),
    queryFn: () => fetchJson<AssetDocument[]>("/api/v1/documents"),
  });
  const assetsQ = useQuery({
    queryKey: qk.assets(),
    queryFn: () => fetchJson<Asset[]>("/api/v1/assets"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
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
                    <Chip tone={EXTRACTION_TONE[doc.extraction_status]} size="sm">
                      {EXTRACTION_LABEL[doc.extraction_status]}
                    </Chip>
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
