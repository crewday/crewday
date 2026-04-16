import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type { Asset, AssetCondition, AssetStatus, AssetType, Property } from "@/types/api";

const CONDITION_TONE: Record<AssetCondition, "moss" | "sand" | "rust"> = {
  new: "moss",
  good: "moss",
  fair: "sand",
  poor: "rust",
  needs_replacement: "rust",
};

const STATUS_TONE: Record<AssetStatus, "moss" | "sand" | "rust" | "ghost"> = {
  active: "moss",
  in_repair: "sand",
  decommissioned: "ghost",
  disposed: "rust",
};

export default function AssetsPage() {
  const [activeCategory, setActiveCategory] = useState("");
  const [activeProperty, setActiveProperty] = useState("");

  const assetsQ = useQuery({
    queryKey: qk.assets(),
    queryFn: () => fetchJson<Asset[]>("/api/v1/assets"),
  });
  const typesQ = useQuery({
    queryKey: qk.assetTypes(),
    queryFn: () => fetchJson<AssetType[]>("/api/v1/asset_types"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  const sub = "Tracked equipment and appliances across all properties.";
  const actions = <button className="btn btn--moss">+ New asset</button>;

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

  return (
    <DeskPage title="Assets" sub={sub} actions={actions}>
      <section className="panel">
        <div className="desk-filters">
          <span
            className={"chip chip--ghost chip--sm" + (activeCategory === "" ? " chip--active" : "")}
            onClick={() => setActiveCategory("")}
          >
            All
          </span>
          {categories.map((cat) => (
            <span
              key={cat}
              className={"chip chip--ghost chip--sm" + (activeCategory === cat ? " chip--active" : "")}
              onClick={() => setActiveCategory(cat)}
            >
              {cat}
            </span>
          ))}
        </div>
        <div className="desk-filters">
          <span
            className={"chip chip--ghost chip--sm" + (activeProperty === "" ? " chip--active" : "")}
            onClick={() => setActiveProperty("")}
          >
            All properties
          </span>
          {propsQ.data.map((p) => (
            <span
              key={p.id}
              className={"chip chip--" + p.color + " chip--sm" + (activeProperty === p.id ? " chip--active" : "")}
              onClick={() => setActiveProperty(p.id)}
            >
              {p.name}
            </span>
          ))}
        </div>

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
                    <Link to={"/asset/" + a.id} className="link">
                      {at ? at.icon + " " : ""}<strong>{a.name}</strong>
                    </Link>
                    {makeLine && <span className="table__sub">{makeLine}</span>}
                  </td>
                  <td>{at?.name ?? <span className="muted">--</span>}</td>
                  <td>{prop && <Chip tone={prop.color} size="sm">{prop.name}</Chip>}</td>
                  <td>{a.area ?? <span className="muted">--</span>}</td>
                  <td><Chip tone={CONDITION_TONE[a.condition]} size="sm">{a.condition.replace("_", " ")}</Chip></td>
                  <td><Chip tone={STATUS_TONE[a.status]} size="sm">{a.status.replace("_", " ")}</Chip></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </DeskPage>
  );
}
