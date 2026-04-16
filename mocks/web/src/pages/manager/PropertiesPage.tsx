import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Loading } from "@/components/common";
import type { Property, PropertyClosure, Stay } from "@/types/api";

interface StaysPayload {
  stays: Stay[];
  closures: PropertyClosure[];
}

export default function PropertiesPage() {
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const staysQ = useQuery({
    queryKey: qk.stays(),
    queryFn: () => fetchJson<StaysPayload>("/api/v1/stays"),
  });

  if (propsQ.isPending || staysQ.isPending) {
    return (
      <DeskPage title="Properties" actions={<button className="btn btn--moss">+ Add property</button>}>
        <Loading />
      </DeskPage>
    );
  }
  if (!propsQ.data || !staysQ.data) {
    return (
      <DeskPage title="Properties" actions={<button className="btn btn--moss">+ Add property</button>}>
        Failed to load.
      </DeskPage>
    );
  }

  const properties = propsQ.data;
  const stays = staysQ.data.stays;
  const closures = staysQ.data.closures;

  return (
    <DeskPage
      title="Properties"
      actions={<button className="btn btn--moss">+ Add property</button>}
    >
      <section className="grid grid--cards">
        {properties.map((p) => {
          const propStays = stays.filter((s) => s.property_id === p.id);
          const propClosures = closures.filter((c) => c.property_id === p.id);
          return (
            <article key={p.id} className="prop-card">
              <Link className="prop-card__link" to={"/property/" + p.id}>
                <div className={"prop-card__swatch prop-card__swatch--" + p.color}>
                  <span className="prop-card__kind">{p.kind.toUpperCase()}</span>
                </div>
                <div className="prop-card__body">
                  <h3 className="prop-card__name">{p.name}</h3>
                  <div className="prop-card__city">{p.city} · {p.timezone}</div>
                  <div className="prop-card__stats">
                    <span>{propStays.length} stays</span>
                    <span>·</span>
                    <span>{p.areas.length} areas</span>
                    {propClosures.length > 0 && (
                      <>
                        <span>·</span>
                        <span className="muted">
                          {propClosures.length} closure{propClosures.length > 1 ? "s" : ""}
                        </span>
                      </>
                    )}
                  </div>
                </div>
              </Link>
              <div className="prop-card__footer">
                <Link to={"/property/" + p.id} className="link">Overview</Link>
                <Link to={"/property/" + p.id + "/closures"} className="link link--muted">
                  Closures →
                </Link>
              </div>
            </article>
          );
        })}
      </section>
    </DeskPage>
  );
}
