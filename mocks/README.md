# crew.day — UI preview mocks

Disposable, hard-coded preview of the manager and employee UIs while
the real application hasn't been built. No DB, no auth, no real
business logic. The container runs as a non-root user (`crewday:10001`,
per `docs/specs/16`). Every mutation (tick a checklist item, approve an
expense, etc.) is an in-memory toggle that lives until restart.

The goal is to make `docs/specs/14-web-frontend.md` tangible — every
spec-listed route renders something, the design tokens match, the
vocabulary matches.

## Running

```bash
docker compose -f mocks/docker-compose.yml up -d --build
```

- Local: http://127.0.0.1:8100
- Public: https://dev.crew.day (via Pangolin / Traefik with
  badger auth; same wiring as `../fj2`)

## Topology (cd-g1cy)

The dev stack runs four containers (plus Mailpit) that together
serve the same single-origin shape we will ship in production:

```
browser ──▶ FastAPI (app-api :8000)  ── /api, /events, /healthz, …
   ▲                  │                  ───▶ real routers
   │                  └─ everything else
   │                     (HTTP fetch + ``vite-hmr`` WebSocket on /)
   │                                     ───▶ web-dev (Vite :5173)
   │                                                    │
   │                                                    └ ``/mocks/*`` ──▶
   │                                                      mocks-web-dev
   │                                                      (Vite :5173, --base /mocks/)
   │                                                                    │
   │                                                                    └ ``/mocks/api`` ──▶
   │                                                                      mocks-api (FastAPI)
   │
   └── 127.0.0.1:8100 (loopback) or dev.crew.day (Traefik)
```

Key points:

- The browser never talks to Vite directly. FastAPI is the only
  thing bound to host port 8100, and the only thing Traefik routes
  for un-prefixed `dev.crew.day` requests.
- The dev profile (`CREWDAY_PROFILE=dev`) turns on the Vite reverse
  proxy in `app/api/proxy.py` — HTTP for module loads, WebSocket
  upgrade for Vite's `vite-hmr` HMR socket.
- `web-dev` and `mocks-web-dev` Vite containers are joined to the
  compose network only; they have no host port and (for `web-dev`)
  no Traefik labels. `mocks-web-dev` keeps a higher-priority
  (`priority=100`) `Host(dev.crew.day) && PathPrefix(/mocks)`
  Traefik router so requests via the **public host**
  (`dev.crew.day/mocks/*`) go straight to it — they do **not** flow
  through FastAPI. On the **loopback front door**
  (`127.0.0.1:8100/mocks/*`) the request does take the longer path
  drawn above (FastAPI → web-dev → mocks-web-dev) because Traefik is
  not in the picture there. Both shapes preserve the single-origin
  invariant; the public-host shortcut keeps the public route from
  paying for two extra HTTP hops.

### Why this topology

Two reasons:

1. **Production parity** — prod will serve the compiled SPA and the
   JSON API from a single FastAPI process. Putting the dev stack
   behind the same single origin shakes out cookie / CSP / origin
   bugs before they hide in a shipped bundle.
2. **Live exercise of the WS HMR proxy** — the proxy code in
   `app/api/proxy.py` (cd-q1be HTTP + cd-354g WebSocket) used to be
   unit-tested only. With this topology, every save under
   `app/web/src/**/*.tsx` flows browser → FastAPI WS → Vite, so a
   regression on either side surfaces immediately as `[vite] server
   connection lost` in the browser console.

### Debugging HMR

If editing a `.tsx` no longer triggers `[vite] hot updated`:

1. Confirm the FastAPI front door is up: `curl -I http://127.0.0.1:8100/healthz` returns `200`.
2. Confirm the WebSocket leg works:
   ```bash
   curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
     -H "Sec-WebSocket-Version: 13" \
     -H "Sec-WebSocket-Key: $(openssl rand -base64 16)" \
     -H "Sec-WebSocket-Protocol: vite-hmr" \
     http://127.0.0.1:8100/
   ```
   Expect `HTTP/1.1 101 Switching Protocols`.
3. Open `http://127.0.0.1:8100/` in a browser and look for
   `[vite] connected.` in the console — `[vite] server connection
   lost.` means the WS upgrade didn't reach Vite. Check
   `docker compose -f mocks/docker-compose.yml logs app-api` for
   `spa_vite_ws_proxy_failed` events.
4. Confirm `CREWDAY_VITE_DEV_URL` in the `app-api` environment
   matches the running `web-dev` service hostname (compose service
   name is the DNS name on the default network).

## Audience toggle

Top banner has **Employee · Manager** pills (sets a cookie and
redirects to that audience's home) and a **☾ / ☀** theme toggle
(light is primary, dark is manual per §14). There's also a link to
`/styleguide` for the component gallery.

## Route map (matches §14)

**Public / unauthenticated**

- `/login` — passkey sign-in
- `/recover` — self-service lost-device recovery (§03); managers
  / owners see a step-up break-glass code field
- `/accept/<token>` — click-to-accept invite (new user → passkey
  ceremony; existing user → Accept card)
- `/guest/<token>` — tokenized guest welcome page (wifi, access,
  check-out checklist from `guest_visible` items)

**Employee (mobile-first PWA)**

- `/today`, `/week`, `/task/<id>` (sticky CTA, conditional
  "Complete with photo", skip-with-reason modal, evidence note)
- `/issues/new`, `/expenses`, `/messages`, `/shifts`, `/me`

Bottom nav per §14: **Today · Week · Issues · Expenses · Me** plus a
capability-gated chat bubble (shown when `chat.assistant = true`).

**Manager (desktop-first)**

- `/dashboard` — stats, tasks, approvals, issues, leaves
- `/properties`, `/property/<id>`, `/property/<id>/closures`
- `/stays` — four-layer calendar (stays · turnover · closures · leave)
- `/employees`, `/employee/<id>`, `/employee/<id>/leaves`, `/leaves`
- `/templates`, `/schedules`, `/instructions`, `/instructions/<id>`,
  `/inventory`, `/pay`
- `/approvals`, `/expenses` (approvals queue — same URL as the
  employee, audience-dependent view per §14)
- `/audit`, `/webhooks`, `/llm`, `/settings`

**Dev / ops**

- `/styleguide`, `/healthz`, `/readyz`, `/metrics`

## Design tokens

Hand-written CSS keyed off semantic classes (per project CLAUDE.md —
no utility frameworks). Tokens follow §14:

- Palette: Paper `#FAF7F2` / Ink `#1F1A14` / Moss `#3F6E3B` / Rust
  `#B04A27` / Sand `#D9A441` / Sky `#4F7CA8` / Night `#F4EFE6` (dark).
- Typography: Fraunces (display, variable `opsz`), Inter Tight
  (body), JetBrains Mono (dev-facing).
- Grain texture at 3–6% on warm paper; moss-tinted shadows.
- Dark theme via `[data-theme="dark"]` lifted for AA contrast.
- `prefers-reduced-motion` respected; focus ring is a Moss 2px outline.

## Pangolin setup (one-time, if not already done)

The container joins the existing `traefik-proxy` network and
registers Traefik labels — same pattern as `../fj2`. For badger to
gate the domain, `dev.crew.day` needs to exist as a **resource**
in the Pangolin dashboard (target `crewday-app-api:8000` under the
flipped topology — cd-g1cy renamed the front-door container from
`crewday-mocks` to `crewday-app-api`). DNS is already CNAMEd to this
host.

## Files

- `app/main.py` — all FastAPI routes
- `app/mock_data.py` — the fake household (3 properties, 5 staff,
  7 tasks, 5 stays, ~45 other rows across instructions / inventory /
  leaves / payslips / audit / agents)
- `app/templates/` — Jinja2, three base layouts (`base`,
  `employee_base`, `manager_base`) sharing one design system
- `app/static/styles.css` — hand-written, ~900 lines, both themes
- `Dockerfile` — Python 3.12-slim, `USER crewday:crewday`
- `docker-compose.yml` — Traefik labels + `user: "10001:10001"`

## Removing

```bash
docker compose -f mocks/docker-compose.yml down
rm -rf mocks/
```
