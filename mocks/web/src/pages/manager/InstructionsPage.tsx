import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { INSTRUCTION_SCOPE_TONE } from "@/lib/tones";
import type { Instruction, Property } from "@/types/api";

function fmtUpdated(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

function preview(body: string): string {
  return body.length > 180 ? body.slice(0, 180) + "…" : body;
}

export default function InstructionsPage() {
  const instrQ = useQuery({
    queryKey: qk.instructions(),
    queryFn: () => fetchJson<Instruction[]>("/api/v1/instructions"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  const sub = "The house knowledge base. Global rules, property quirks, area-specific tips. Staff see the ones that apply to their task.";
  const actions = <button className="btn btn--moss">+ New instruction</button>;

  if (instrQ.isPending || propsQ.isPending) {
    return <DeskPage title="Instructions" sub={sub} actions={actions}><Loading /></DeskPage>;
  }
  if (!instrQ.data || !propsQ.data) {
    return <DeskPage title="Instructions" sub={sub} actions={actions}>Failed to load.</DeskPage>;
  }

  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));
  const instructions = instrQ.data;
  const countBy = (scope: Instruction["scope"]): number =>
    instructions.filter((i) => i.scope === scope).length;

  const scopeLabel = (i: Instruction): string => {
    if (i.scope === "global") return "House-wide";
    const propName = i.property_id ? propsById.get(i.property_id)?.name ?? "" : "";
    if (i.scope === "property") return propName;
    return propName + (i.area ? " · " + i.area : "");
  };

  return (
    <DeskPage title="Instructions" sub={sub} actions={actions}>
      <section className="panel">
        <div className="desk-filters">
          <span className="chip chip--ghost chip--sm chip--active">All</span>
          <span className="chip chip--ghost chip--sm">Global · {countBy("global")}</span>
          <span className="chip chip--ghost chip--sm">Property · {countBy("property")}</span>
          <span className="chip chip--ghost chip--sm">Area · {countBy("area")}</span>
        </div>

        <ul className="kb-list">
          {instructions.map((i) => (
            <li key={i.id} className="kb-item">
              <Link to={"/instructions/" + i.id} className="kb-item__main">
                <div className="kb-item__head">
                  <h3 className="kb-item__title">{i.title}</h3>
                  <Chip tone={INSTRUCTION_SCOPE_TONE[i.scope]} size="sm">{scopeLabel(i)}</Chip>
                </div>
                <p className="kb-item__preview">{preview(i.body_md)}</p>
                <div className="kb-item__meta">
                  {i.tags.map((t) => (
                    <Chip key={t} tone="ghost" size="sm">#{t}</Chip>
                  ))}
                  <span className="mono muted">v{i.version} · updated {fmtUpdated(i.updated_at)}</span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </DeskPage>
  );
}
