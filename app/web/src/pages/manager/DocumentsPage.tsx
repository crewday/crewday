import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import { Files } from "lucide-react";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { workspaceRouteForPathname } from "@/lib/workspaceRoutes";
import DeskPage from "@/components/DeskPage";
import { EmptyState, FilterChipGroup, Loading } from "@/components/common";
import DocumentLibraryTable from "./documents/DocumentLibrary";
import { fetchDocumentList } from "./documents/DocumentLibrary.lib";
import type {
  Asset,
  AssetDocument,
  DocumentKind,
  Property,
} from "@/types/api";

interface ListEnvelope<T> {
  data: T[];
}

function unwrapList<T>(payload: T[] | ListEnvelope<T>): T[] {
  return Array.isArray(payload) ? payload : payload.data;
}

async function fetchList<T>(path: string): Promise<T[]> {
  return unwrapList(await fetchJson<T[] | ListEnvelope<T>>(path));
}

export default function DocumentsPage() {
  // code-health: ignore[nloc] Documents page keeps filters, empty guidance, and extraction table together for one route.
  const { pathname } = useLocation();
  const [activeKind, setActiveKind] = useState<DocumentKind | "">("");
  const [activeProperty, setActiveProperty] = useState<string>("");

  const docsQ = useQuery({
    queryKey: qk.documents(),
    queryFn: () => fetchDocumentList("/api/v1/documents"),
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

  if (docsQ.data.length === 0) {
    return (
      <DeskPage title="Documents" sub={sub}>
        <section className="panel">
          <EmptyState
            icon={Files}
            title="No documents listed yet"
            copy="Documents are attached from the property or asset they belong to. Open a property for permits, contracts, and insurance, or open an asset for manuals, warranties, and invoices."
            variant="compact"
          >
            <p>
              <Link className="btn btn--moss" to={workspaceRouteForPathname(pathname, "/assets")}>Open assets</Link>{" "}
              <Link className="btn btn--ghost" to={workspaceRouteForPathname(pathname, "/properties")}>Open properties</Link>
            </p>
          </EmptyState>
        </section>
      </DeskPage>
    );
  }

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

        <DocumentLibraryTable
          documents={filtered}
          assetsById={assetsById}
          propertiesById={propsById}
        />
      </section>
    </DeskPage>
  );
}
