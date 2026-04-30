import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Loading } from "@/components/common";

interface ChecklistItem {
  id: string;
  label: string;
}

interface GuestAsset {
  id: string;
  name: string;
  guest_instructions_md: string;
  cover_photo_url: string | null;
}

interface GuestPayload {
  property_id: string;
  property_name: string;
  unit_id: string | null;
  unit_name: string | null;
  welcome: Record<string, unknown>;
  checklist: ChecklistItem[];
  assets: GuestAsset[];
  check_in_at: string;
  check_out_at: string;
  guest_name: string | null;
}

function fmtDay(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-GB", {
    weekday: "short",
    day: "2-digit",
    month: "short",
  });
}

function fmtDayMonth(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

function objectValue(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function stringValue(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

function welcomeText(
  welcome: Record<string, unknown>,
  keys: readonly string[],
): string | null {
  for (const key of keys) {
    const value = stringValue(welcome[key]);
    if (value) return value;
  }
  return null;
}

function lines(value: string | null): string[] {
  if (!value) return [];
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function wifiDetails(welcome: Record<string, unknown>) {
  const wifi = objectValue(welcome.wifi);
  return {
    ssid:
      stringValue(wifi?.ssid) ??
      welcomeText(welcome, ["wifi_ssid", "wifi_network", "ssid"]),
    password:
      stringValue(wifi?.password) ??
      welcomeText(welcome, ["wifi_password", "wifi_pass", "password"]),
  };
}

function accessLines(welcome: Record<string, unknown>): string[] {
  const access = objectValue(welcome.access);
  const pairs: [string, unknown][] = [
    ["Front door code", access?.door_code ?? welcome.door_code],
    ["Gate code", access?.gate_code ?? welcome.gate_code],
    ["Lockbox", access?.lockbox ?? welcome.lockbox],
    ["Parking", access?.parking ?? welcome.parking],
  ];
  const out = pairs.flatMap(([label, value]) => {
    const text = stringValue(value);
    return text ? [`${label}: ${text}`] : [];
  });
  const freeform =
    stringValue(welcome.access) ??
    welcomeText(welcome, ["access_info", "access_notes"]);
  return out.length > 0 ? out : lines(freeform);
}

function emergencyLines(welcome: Record<string, unknown>): string[] {
  const contacts = welcome.emergency_contacts;
  if (Array.isArray(contacts)) {
    return contacts.flatMap((raw) => {
      const contact = objectValue(raw);
      if (!contact) return [];
      const label = stringValue(contact.label);
      const name = stringValue(contact.name);
      const phone = stringValue(contact.phone_e164) ?? stringValue(contact.phone);
      const left = [label, name].filter(Boolean).join(" — ");
      if (left && phone) return [`${left}: ${phone}`];
      return left || phone ? [left || phone || ""] : [];
    });
  }
  return lines(welcomeText(welcome, ["emergency_contacts_md", "emergency"]));
}

export default function GuestPage() {
  const { token = "" } = useParams<{ token: string }>();

  const q = useQuery({
    queryKey: qk.guest(token),
    enabled: token.length > 0,
    queryFn: () =>
      fetchJson<GuestPayload>(`/api/v1/stays/welcome/${encodeURIComponent(token)}`),
  });

  if (token.length === 0) {
    return (
      <div className="surface surface--guest">
        <main className="guest">
          <p className="muted">This guest link is no longer valid.</p>
        </main>
      </div>
    );
  }
  if (q.isPending) {
    return (
      <div className="surface surface--guest">
        <main className="guest">
          <Loading />
        </main>
      </div>
    );
  }
  if (q.isError || !q.data) {
    return (
      <div className="surface surface--guest">
        <main className="guest">
          <p className="muted">This guest link is no longer valid.</p>
        </main>
      </div>
    );
  }

  const payload = q.data;
  const wifi = wifiDetails(payload.welcome);
  const access = accessLines(payload.welcome);
  const houseRules = lines(
    welcomeText(payload.welcome, ["house_rules_md", "house_rules"]),
  );
  const trash = lines(
    welcomeText(payload.welcome, ["trash_schedule_md", "trash_schedule"]),
  );
  const emergency = emergencyLines(payload.welcome);

  return (
    <div className="surface surface--guest">
      <main className="guest">
        <header className="guest__hero">
          <span className="guest__eyebrow">Welcome to</span>
          <h1 className="guest__name">{payload.unit_name ?? payload.property_name}</h1>
          <p className="guest__stay">
            {fmtDay(payload.check_in_at)} → {fmtDay(payload.check_out_at)} ·{" "}
            {payload.guest_name ? `Guest: ${payload.guest_name}` : "Guest stay"}
          </p>
        </header>

        <section className="guest__grid">
          <article className="guest-card">
            <h2 className="guest-card__title">Wifi</h2>
            <dl className="guest-card__kv">
              <dt>Network</dt>
              <dd className="mono">{wifi.ssid ?? "Ask your host"}</dd>
              <dt>Password</dt>
              <dd className="mono">{wifi.password ?? "Ask your host"}</dd>
            </dl>
          </article>

          <article className="guest-card">
            <h2 className="guest-card__title">Access</h2>
            {access.length === 0 ? (
              <p>Access details will be shared by your host.</p>
            ) : (
              access.map((line) => <p key={line}>{line}</p>)
            )}
          </article>

          <article className="guest-card">
            <h2 className="guest-card__title">House rules</h2>
            <ul className="guest-card__list">
              {houseRules.length === 0 ? (
                <li>No house rules have been added yet.</li>
              ) : (
                houseRules.map((rule) => <li key={rule}>{rule}</li>)
              )}
            </ul>
          </article>

          {payload.assets.length > 0 && (
            <article className="guest-card guest-card--wide">
              <h2 className="guest-card__title">Equipment</h2>
              <div className="guest-equipment">
                {payload.assets.map((asset) => (
                  <div key={asset.id} className="guest-asset-card">
                    <div className="guest-asset-card__name">{asset.name}</div>
                    {asset.cover_photo_url && (
                      <div className="guest-asset-card__meta">
                        Photo guide available
                      </div>
                    )}
                    {asset.guest_instructions_md && (
                      <div className="guest-asset-card__instructions">
                        {asset.guest_instructions_md}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </article>
          )}

          <article className="guest-card">
            <h2 className="guest-card__title">Trash &amp; recycling</h2>
            {trash.length === 0 ? (
              <p>Trash and recycling instructions have not been added yet.</p>
            ) : (
              trash.map((line) => <p key={line}>{line}</p>)
            )}
          </article>

          <article className="guest-card guest-card--wide">
            <h2 className="guest-card__title">Before you leave</h2>
            <p className="muted">
              A short checklist — nothing scary, just what we always ask.
            </p>
            <ul className="guest-checklist">
              {payload.checklist.length === 0 ? (
                <li className="muted">(No check-out notes for this stay.)</li>
              ) : (
                payload.checklist.map((item) => (
                  <li key={item.id}>
                    <span className="checklist__box" aria-hidden="true"></span>
                    <span>{item.label}</span>
                  </li>
                ))
              )}
            </ul>
          </article>

          <article className="guest-card">
            <h2 className="guest-card__title">Emergency contacts</h2>
            <ul className="guest-card__list">
              {emergency.length === 0 ? (
                <li>Contact your host for urgent help.</li>
              ) : (
                emergency.map((line) => <li key={line}>{line}</li>)
              )}
            </ul>
          </article>
        </section>

        <footer className="guest__footer">
          <a
            className="btn btn--ghost"
            href={`mailto:hello@example.com?subject=Issue at ${
              payload.unit_name ?? payload.property_name
            }`}
          >
            Report an issue
          </a>
          <span className="muted">
            Link expires {fmtDayMonth(payload.check_out_at)} · no login, no
            cookies.
          </span>
        </footer>
      </main>
    </div>
  );
}
