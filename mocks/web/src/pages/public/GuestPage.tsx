import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Loading } from "@/components/common";
import type { Asset, ChecklistItem, Property, Stay } from "@/types/api";

interface GuestPayload {
  stay: Stay | null;
  property: Property | null;
  guest_checklist: ChecklistItem[];
  guest_assets: Asset[];
}

// "Mon 18 Apr" — matches the Jinja `%a %d %b` formatting used by the
// legacy template so the printed page looks identical.
function fmtDay(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short" });
}

// "18 Apr" — used in the footer's link-expiry note.
function fmtDayMonth(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

export default function GuestPage() {
  // Token is decorative on the mock — the data behind /api/v1/guest is
  // fixed to GUEST_STAY_ID. In production this would key the lookup.
  useParams<{ token: string }>();

  const q = useQuery({
    queryKey: qk.guest(),
    queryFn: () => fetchJson<GuestPayload>("/api/v1/guest"),
  });

  if (q.isPending) {
    return (
      <div className="surface surface--guest">
        <main className="guest"><Loading /></main>
      </div>
    );
  }
  if (q.isError || !q.data || !q.data.stay || !q.data.property) {
    return (
      <div className="surface surface--guest">
        <main className="guest">
          <p className="muted">This guest link is no longer valid.</p>
        </main>
      </div>
    );
  }

  const { stay, property, guest_checklist, guest_assets } = q.data;

  return (
    <div className="surface surface--guest">
      <main className="guest">
        <header className="guest__hero">
          <span className="guest__eyebrow">Welcome to</span>
          <h1 className="guest__name">{property.name}</h1>
          <p className="guest__stay">
            {fmtDay(stay.check_in)} → {fmtDay(stay.check_out)} · {stay.guests} guests
          </p>
        </header>

        <section className="guest__grid">
          <article className="guest-card">
            <h2 className="guest-card__title">Wifi</h2>
            <dl className="guest-card__kv">
              <dt>Network</dt><dd className="mono">villa-sud-guest</dd>
              <dt>Password</dt><dd className="mono">houseparty-2026!</dd>
            </dl>
          </article>

          <article className="guest-card">
            <h2 className="guest-card__title">Access</h2>
            <p>Front door code: <code className="inline-code">★ 4 2 8 1 #</code></p>
            <p>Parking: the spot marked "3B" in the garage.</p>
            <p>If you get stuck: call Élodie, +33 6 01 02 03 04.</p>
          </article>

          <article className="guest-card">
            <h2 className="guest-card__title">House rules</h2>
            <ul className="guest-card__list">
              <li>Quiet hours: 22:00 – 08:00.</li>
              <li>No smoking indoors — the terrace is fine.</li>
              <li>Pets welcome, please keep them off the sofas.</li>
            </ul>
          </article>

          {guest_assets.length > 0 && (
            <article className="guest-card guest-card--wide">
              <h2 className="guest-card__title">Equipment</h2>
              <div className="guest-equipment">
                {guest_assets.map((a) => (
                  <div key={a.id} className="guest-asset-card">
                    <div className="guest-asset-card__name">{a.name}</div>
                    <div className="guest-asset-card__meta">
                      {[a.make, a.model].filter(Boolean).join(" ")}
                    </div>
                    {a.guest_instructions && (
                      <div className="guest-asset-card__instructions">{a.guest_instructions}</div>
                    )}
                  </div>
                ))}
              </div>
            </article>
          )}

          <article className="guest-card">
            <h2 className="guest-card__title">Trash &amp; recycling</h2>
            <p>
              Collection Mon &amp; Thu morning. Bins are in the courtyard — the
              green one is recycling, yellow is general, brown is organics.
            </p>
          </article>

          <article className="guest-card guest-card--wide">
            <h2 className="guest-card__title">Before you leave</h2>
            <p className="muted">A short checklist — nothing scary, just what we always ask.</p>
            <ul className="guest-checklist">
              {guest_checklist.length === 0 ? (
                <li className="muted">(No check-out notes for this stay.)</li>
              ) : (
                guest_checklist.map((item, idx) => (
                  <li key={idx}>
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
              <li>Host (Élodie): +33 6 01 02 03 04</li>
              <li>Ambulance: 15</li>
              <li>Police: 17</li>
            </ul>
          </article>
        </section>

        <footer className="guest__footer">
          <a
            className="btn btn--ghost"
            href={`mailto:hello@example.com?subject=Issue at ${property.name}`}
          >
            Report an issue
          </a>
          <span className="muted">
            Link expires {fmtDayMonth(stay.check_out)} · no login, no cookies.
          </span>
        </footer>
      </main>
    </div>
  );
}
