import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { fetchJson, resolveApiPath } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, FilterChipGroup, Loading } from "@/components/common";
import { AssetIcon } from "@/components/AssetIcon";
import { ASSET_CONDITION_TONE, ASSET_STATUS_TONE } from "@/lib/tones";
import type { Asset, AssetType, Property } from "@/types/api";

interface ListEnvelope<T> {
  data: T[];
}

function unwrapList<T>(payload: T[] | ListEnvelope<T>): T[] {
  return Array.isArray(payload) ? payload : payload.data;
}

async function fetchList<T>(path: string): Promise<T[]> {
  return unwrapList(await fetchJson<T[] | ListEnvelope<T>>(path));
}

function setSearchParam(
  current: URLSearchParams,
  key: string,
  value: string,
): URLSearchParams {
  const next = new URLSearchParams(current);
  if (value) {
    next.set(key, value);
  } else {
    next.delete(key);
  }
  return next;
}

function QrSheetButton({
  category,
  propertyId,
}: {
  category: string;
  propertyId: string;
}) {
  const params = new URLSearchParams();
  if (category) params.set("category", category);
  if (propertyId) params.set("property_id", propertyId);
  const suffix = params.toString() ? "?" + params.toString() : "";
  return (
    <button
      className="btn"
      onClick={() =>
        window.open(
          resolveApiPath("/api/v1/assets/qr-sheet" + suffix),
          "_blank",
          "noopener,noreferrer",
        )
      }
    >
      Print QR labels
    </button>
  );
}

export default function AssetsPage() {
  // code-health: ignore[nloc] Assets page is query plus filterable card/table composition with shared controls.
  const [searchParams, setSearchParams] = useSearchParams();
  const activeCategory = searchParams.get("category") ?? "";
  const activeProperty = searchParams.get("property_id") ?? "";

  const assetsQ = useQuery({
    queryKey: qk.assets(),
    queryFn: () => fetchList<Asset>("/api/v1/assets"),
  });
  const typesQ = useQuery({
    queryKey: qk.assetTypes(),
    queryFn: () => fetchList<AssetType>("/api/v1/asset_types"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchList<Property>("/api/v1/properties"),
  });

  const sub = "Tracked equipment and appliances across all properties.";
  const actions = (
    <>
      <QrSheetButton category={activeCategory} propertyId={activeProperty} />
      <button className="btn btn--moss">+ New asset</button>
    </>
  );

  if (assetsQ.isPending || typesQ.isPending || propsQ.isPending) {
    return <DeskPage title="Assets" sub={sub} actions={actions}><Loading /></DeskPage>;
  }
  if (!assetsQ.data || !typesQ.data || !propsQ.data) {
    return <DeskPage title="Assets" sub={sub} actions={actions}>Failed to load.</DeskPage>;
  }

  const typesById = new Map(typesQ.data.map((t) => [t.id, t]));
  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));

  const categories = Array.from(new Set(typesQ.data.map((t) => t.category)));

  const filtered = assetsQ.data.filter((a) => {
    if (activeProperty && a.property_id !== activeProperty) return false;
    if (activeCategory) {
      const at = a.asset_type_id ? typesById.get(a.asset_type_id) : null;
      if (!at || at.category !== activeCategory) return false;
    }
    return true;
  });

  const categoryOptions = categories.map((cat) => ({ value: cat, label: cat }));
  const propertyOptions = propsQ.data.map((p) => ({
    value: p.id,
    label: p.name,
    tone: p.color,
  }));

  return (
    <DeskPage title="Assets" sub={sub} actions={actions}>
      <section className="panel">
        <FilterChipGroup
          value={activeCategory}
          onChange={(value) =>
            setSearchParams(setSearchParam(searchParams, "category", value))
          }
          options={categoryOptions}
        />
        <FilterChipGroup
          value={activeProperty}
          onChange={(value) =>
            setSearchParams(setSearchParam(searchParams, "property_id", value))
          }
          allLabel="All properties"
          options={propertyOptions}
        />

        <table className="table">
          <thead>
            <tr>
              <th>Asset</th>
              <th>Type</th>
              <th>Property</th>
              <th>Area</th>
              <th>Condition</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((a) => {
              const at = a.asset_type_id ? typesById.get(a.asset_type_id) : null;
              const prop = propsById.get(a.property_id);
              const makeLine = [a.make, a.model].filter(Boolean).join(" ");
              return (
                <tr key={a.id}>
                  <td>
                    <Link to={"/asset/" + a.id} className="link asset-name-link">
                      {at && <AssetIcon name={at.icon_name} />}
                      <strong>{a.name}</strong>
                    </Link>
                    {makeLine && <span className="table__sub">{makeLine}</span>}
                  </td>
                  <td>{at?.name ?? <span className="muted">--</span>}</td>
                  <td>{prop && <Chip tone={prop.color} size="sm">{prop.name}</Chip>}</td>
                  <td>{a.area ?? <span className="muted">--</span>}</td>
                  <td><Chip tone={ASSET_CONDITION_TONE[a.condition]} size="sm">{a.condition.replace("_", " ")}</Chip></td>
                  <td><Chip tone={ASSET_STATUS_TONE[a.status]} size="sm">{a.status.replace("_", " ")}</Chip></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </DeskPage>
  );
}
