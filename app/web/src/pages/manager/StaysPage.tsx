import { useQuery } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { fetchJson } from "@/lib/api";
import { type ListEnvelope } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type {
  Employee,
  Leave,
  Property,
  PropertyClosure,
  Stay,
} from "@/types/api";

interface StaysPayload {
  stays: Stay[];
  closures: PropertyClosure[];
  leaves: Leave[];
}

interface ReservationPayload {
  id: string;
  property_id: string;
  check_in: string;
  check_out: string;
  guest_name: string | null;
  guest_count: number | null;
  status: string;
  source: string;
}

interface LeaveListPayload {
  id: string;
  user_id: string;
  starts_on: string;
  ends_on: string;
  category: string;
  note_md: string | null;
  approved_at: string | null;
}

const STAY_TONE: Record<Stay["status"], "sky" | "moss" | "ghost" | "rust" | "sand"> = {
  tentative: "sand",
  confirmed: "sky",
  in_house: "moss",
  checked_out: "ghost",
  cancelled: "rust",
};

const DOW = ["M", "T", "W", "T", "F", "S", "S"];

function fmtAbbrevDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", {
    weekday: "short",
    day: "2-digit",
    month: "short",
  });
}

function mapStatus(status: string): Stay["status"] {
  if (status === "cancelled") return "cancelled";
  if (status === "scheduled") return "confirmed";
  if (status === "checked_in") return "in_house";
  if (status === "completed") return "checked_out";
  if (status === "tentative" || status === "confirmed" || status === "in_house" || status === "checked_out") {
    return status;
  }
  return "confirmed";
}

function mapSource(source: string): Stay["source"] {
  if (source === "api") return "manual";
  if (source === "gcal") return "google_calendar";
  if (source === "manual" || source === "airbnb" || source === "vrbo" || source === "booking" || source === "google_calendar" || source === "ical") {
    return source;
  }
  return "ical";
}

function dateOnly(iso: string): string {
  return iso.slice(0, 10);
}

function mapReservation(row: ReservationPayload): Stay {
  return {
    id: row.id,
    property_id: row.property_id,
    guest_name: row.guest_name ?? "Guest",
    source: mapSource(row.source),
    check_in: dateOnly(row.check_in),
    check_out: dateOnly(row.check_out),
    guests: row.guest_count ?? 0,
    status: mapStatus(row.status),
  };
}

function mapLeaveCategory(kind: string): Leave["category"] {
  if (kind === "vacation" || kind === "sick" || kind === "personal" || kind === "bereavement" || kind === "other") {
    return kind;
  }
  return "other";
}

function mapLeave(row: LeaveListPayload): Leave {
  return {
    id: row.id,
    employee_id: row.user_id,
    starts_on: dateOnly(row.starts_on),
    ends_on: dateOnly(row.ends_on),
    category: mapLeaveCategory(row.category),
    note: row.note_md ?? "",
    approved_at: row.approved_at,
  };
}

async function fetchStaysPayload(): Promise<StaysPayload> {
  const [reservations, leaves] = await Promise.all([
    fetchJson<ListEnvelope<ReservationPayload>>("/api/v1/stays/reservations?limit=500"),
    fetchJson<ListEnvelope<LeaveListPayload>>("/api/v1/user_leaves?approved=true&limit=500"),
  ]);
  return {
    stays: reservations.data.map(mapReservation),
    closures: [],
    leaves: leaves.data.map(mapLeave),
  };
}

export default function StaysPage() {
  const dataQ = useQuery({
    queryKey: qk.stays(),
    queryFn: fetchStaysPayload,
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const empsQ = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });
  if (dataQ.isPending || propsQ.isPending || empsQ.isPending) {
    return <DeskPage title="Stays"><Loading /></DeskPage>;
  }
  if (!dataQ.data || !propsQ.data || !empsQ.data) {
    return <DeskPage title="Stays">Failed to load.</DeskPage>;
  }

  const { stays, closures, leaves } = dataQ.data;
  const properties = propsQ.data;
  const propsById = new Map(properties.map((p) => [p.id, p]));
  const empsById = new Map(empsQ.data.map((e) => [e.id, e]));
  const today = new Date();
  const todayDay = today.getDate();

  const days: number[] = [];
  for (let d = 1; d <= 30; d += 1) days.push(d);

  return (
    <DeskPage
      title="Stays"
      sub="Imported from Airbnb, VRBO, and direct bookings. Four layers: stays, turnover bundles, closures, employee leave."
      actions={<button className="btn btn--moss">Import iCal</button>}
      overflow={[{ label: "Add stay", onSelect: () => undefined }]}
    >
      <div className="panel">
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Guest</th>
              <th>Property</th>
              <th>Source</th>
              <th>Check-in</th>
              <th>Check-out</th>
              <th>Guests</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {stays.map((s) => {
              const p = propsById.get(s.property_id);
              return (
                <tr key={s.id}>
                  <td><strong>{s.guest_name}</strong></td>
                  <td>{p && <Chip tone={p.color} size="sm">{p.name}</Chip>}</td>
                  <td><Chip tone="ghost" size="sm">{s.source}</Chip></td>
                  <td className="mono">{fmtAbbrevDate(s.check_in)}</td>
                  <td className="mono">{fmtAbbrevDate(s.check_out)}</td>
                  <td>{s.guests}</td>
                  <td><Chip tone={STAY_TONE[s.status]} size="sm">{s.status.replace("_", " ")}</Chip></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head">
          <h2>April 2026 — calendar</h2>
          <div className="cal-legend">
            <span className="cal-legend__item">
              <span className="swatch-dot swatch-dot--moss" />Villa Sud
            </span>
            <span className="cal-legend__item">
              <span className="swatch-dot swatch-dot--sky" />Apt 3B
            </span>
            <span className="cal-legend__item">
              <span className="swatch-dot swatch-dot--rust" />Chalet Cœur
            </span>
            <span className="cal-legend__item">
              <span className="swatch-dot swatch-dot--turnover" />Turnover
            </span>
            <span className="cal-legend__item">
              <span className="swatch-dot swatch-dot--closed" />Closure
            </span>
            <span className="cal-legend__item">
              <span className="swatch-dot swatch-dot--leave" />Leave
            </span>
          </div>
        </header>

        <div className="cal-wide">
          <div className="cal-wide__headers">
            <div className="cal-wide__corner">April</div>
            {days.map((d) => {
              const dow = DOW[(d - 1 + 2) % 7];
              const cls =
                "cal-wide__header" + (d === todayDay ? " cal-wide__header--today" : "");
              return (
                <div key={d} className={cls}>
                  <span className="cal-wide__dow">{dow}</span>
                  <span className="cal-wide__num">{d}</span>
                </div>
              );
            })}
          </div>

          {properties.map((p) => (
            <div key={p.id} className="cal-wide__row">
              <div className="cal-wide__label">
                <Chip tone={p.color} size="sm">{p.name}</Chip>
              </div>
              {days.map((d) => (
                <div key={d} className="cal-wide__cell">
                  {stays.map((s) => {
                    if (s.property_id !== p.id) return null;
                    const ci = new Date(s.check_in).getDate();
                    const co = new Date(s.check_out).getDate();
                    const nodes: ReactElement[] = [];
                    if (ci <= d && d <= co) {
                      nodes.push(
                        <span
                          key={s.id + "-bar"}
                          className={"cal-bar cal-bar--" + p.color}
                          title={s.guest_name + " (" + s.source + ")"}
                        >
                          {d === ci ? s.guest_name.split(" ")[0] : ""}
                        </span>,
                      );
                    }
                    if (co === d) {
                      nodes.push(
                        <span
                          key={s.id + "-turn"}
                          className="cal-bar cal-bar--turnover"
                          title={"Turnover — " + s.guest_name}
                        />,
                      );
                    }
                    return nodes.length > 0 ? <>{nodes}</> : null;
                  })}
                  {closures.map((c) => {
                    if (c.property_id !== p.id) return null;
                    const cs = new Date(c.starts_on).getDate();
                    const ce = new Date(c.ends_on).getDate();
                    if (cs <= d && d <= ce) {
                      return (
                        <span
                          key={c.id}
                          className="cal-bar cal-bar--closed"
                          title={"Closure: " + c.reason}
                        />
                      );
                    }
                    return null;
                  })}
                </div>
              ))}
            </div>
          ))}

          <div className="cal-wide__row cal-wide__row--leaves">
            <div className="cal-wide__label">
              <Chip tone="ghost" size="sm">Employee leave</Chip>
            </div>
            {days.map((d) => (
              <div key={d} className="cal-wide__cell">
                {leaves.map((lv) => {
                  const start = new Date(lv.starts_on);
                  if (start.getMonth() !== 3) return null;
                  const ls = start.getDate();
                  const le = new Date(lv.ends_on).getDate();
                  if (ls <= d && d <= le) {
                    const emp = empsById.get(lv.employee_id);
                    return (
                      <span
                        key={lv.id}
                        className="cal-bar cal-bar--leave"
                        title={(emp ? emp.name : "") + " — " + lv.category}
                      >
                        {d === ls && emp ? emp.avatar_initials : ""}
                      </span>
                    );
                  }
                  return null;
                })}
              </div>
            ))}
          </div>
        </div>
      </div>
    </DeskPage>
  );
}
