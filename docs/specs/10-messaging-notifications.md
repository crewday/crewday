# 10 — Messaging, notifications, webhooks

## Channels

v1 ships one human messaging channel plus the in-app agent surfaces:

1. **Email** — every out-message originated by a **human** (owner/
   manager-authored mention, notification, digest) goes here, and
   here only.

Cross-user messaging stays on email (§10) and task threads (§06).
The embedded agent conversations in the shared `.desk__agent` web
sidebar (both roles, desktop) and its mobile counterparts (worker
`/chat` page, manager bottom-dock drawer) are the only shipped chat
transports in v1. Off-app chat adapters (WhatsApp, SMS, Telegram)
are specified in §23 but **not enabled in shipped v1**; activation
is gated on an explicit adapter configuration plus, for WhatsApp,
template approval with the provider. OS-level push is **specified
now but delivered by the future native-app project** (see
"Agent-message delivery" below and §14 "Native wrapper readiness");
the push-token registration surface (§12 `/me/push-tokens`) is
reserved in the REST contract and returns `501 push_unavailable`
until the native app ships. Together, push + WhatsApp + email form
the fallback chain for agent-initiated outbound (next subsection).

## Email

### Provider

SMTP (RFC 5321). Config:

- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`
- `SMTP_SECURE` (`starttls` | `tls` | `none`)
- `MAIL_FROM`, `MAIL_REPLY_TO`

Provider-agnostic so the user can wire up Postmark, SES, their own
Postfix, or Resend via SMTP bridge.

### Template system

Jinja2 templates under `app/domain/messaging/templates/`. Notification
kinds (task assigned, daily digest, agent message, ...) live at the
top level as `<kind>.<channel>.j2` files (channels: `subject`,
`body_md`, `push`). Auth-flow templates (magic link, invite, recovery,
passkey reset, email change) live under the `auth/` subdirectory as
`auth/<name>.<channel>.j2` (channels: `subject`, `body_text` — auth
emails are plain-text only in v1). MJML for HTML notification bodies
is a future addition; v1 ships subject + Markdown body for
notifications and subject + plain text for auth flows.

The notification rendering helper is
`app.domain.messaging.notifications.Jinja2TemplateLoader` (autoescape
on, `StrictUndefined`); auth flows render through
`app.mail.auth_templates.render_auth_email` which uses a sibling
`jinja2.Environment` with autoescape off (auth bodies are plain-text
and HTML escaping would mangle URLs and mask copy in the inbox).

Email templates are **filesystem-resident** and authored in code;
they deliberately do **not** use the hash-self-seeded primitive
(§02). Operators change email copy by editing the template file and
redeploying, not through an admin UI — MJML is a build-time concern
and a per-variable override surface would hide layout bugs from
review. If a future deployment needs per-operator copy tweaks, the
right move is a curated variable set (subject, preheader, lead
paragraph) exposed *on top of* the filesystem template, not a
wholesale move to DB bodies.

**Locale-aware template resolution.** The system resolves templates
with locale fallback: it looks for `<kind>.<locale>.<channel>.j2`,
then strips a region tag (`fr-CA` → `fr`) and tries
`<kind>.<language>.<channel>.j2`, then falls back to the locale-free
`<kind>.<channel>.j2`. v1 ships only English defaults; the resolution
logic is in place from day one. All notification templates receive
`locale` in their Jinja context. Formatting helpers (`fmt_date`,
`fmt_money`, `fmt_number`) respect this parameter.

### Emails the system sends

The system sends three families of email. The taxonomy below is the
**code-aligned** view: each row names the canonical kind value as it
appears in code (snake_case, the on-disk template segment, and the
`email_opt_out.category` value the pre-send probe consults), with the
human-readable label in the description column.

#### §10.1 Routed via NotificationService

These flow through `NotificationService.notify()`
(`app/domain/messaging/notifications.py`), persist a `notification`
inbox row, fire the `notification.created` SSE event, and only emit
email when no matching `email_opt_out` row exists. The kind values
below ARE the `NotificationKind` enum — the enum and §10.1 are kept
in lockstep by a coherence test
(`tests/unit/messaging/test_notification_kind_spec_alignment.py`).

| kind value (`NotificationKind`) | description                                          | recipient                              | required? |
|---------------------------------|------------------------------------------------------|----------------------------------------|-----------|
| `task_assigned`                 | a task has been assigned to a user                    | assignee                               | opt-out   |
| `task_overdue`                  | task overdue alert                                    | assigned user + owners/managers        | opt-out   |
| `comment_mention`               | task comment `@mention`                               | mentioned user                         | opt-out   |
| `issue_reported`                | a user reported an issue                              | owners and managers                    | yes       |
| `issue_resolved`                | issue marked resolved (or `wont_fix`)                 | reporter                               | opt-out   |
| `expense_submitted`             | expense awaiting decision                             | owners and managers                    | yes       |
| `expense_approved`              | expense decision: approved (the submitter's branch of "expense decision") | submitting user            | yes       |
| `expense_rejected`              | expense decision: rejected (the submitter's other branch of "expense decision") | submitting user      | yes       |
| `approval_needed`               | a workflow needs an owner/manager decision (incl. agent approvals, availability overrides) | owners and managers | yes |
| `approval_decided`              | the matching `approval_needed` was decided            | requester                              | opt-out   |
| `payslip_issued`                | a payslip is ready                                    | work-engagement user                   | yes       |
| `stay_upcoming`                 | a stay is starting soon                               | owners + assigned workers              | opt-out   |
| `anomaly_detected`              | §11 anomaly heuristic fired                           | owners and managers                    | opt-out   |
| `agent_message`                 | agent reaching a human out-of-band (per "Agent-message delivery" fallback chain) | the targeted user | yes (fallback chain bottoms out here) |
| `daily_digest`                  | the per-recipient daily digest (covers both the owner/manager digest and the worker digest; the worker decides which body to render based on the recipient's grants) | each user with a grant in the workspace | opt-out |
| `privacy_export_ready`          | the §15 privacy access-export bundle the user requested via `POST /api/v1/me/export` is ready to download | the requesting user | yes |
| `webhook_auto_paused`           | a webhook subscription was auto-paused after its health window failed | owners and managers | opt-out |

Opt-outs are per-person, per-category, via a signed unsubscribe link
in the footer of each email. Required kinds (security-relevant or
legally equivalent) cannot be unsubscribed but throttle by priority.

#### §10.2 Direct-mail (auth path)

Authentication and identity-lifecycle emails do **not** go through
`NotificationService`. They render via
`app.mail.auth_templates.render_auth_email` (subject + plain-text
body, autoescape off) and dispatch directly through the configured
`Mailer`. They never write a `notification` inbox row, never fire
the SSE event, and are never opt-outable — losing one of these would
lock the user out.

Templates live under `app/domain/messaging/templates/auth/` as
`auth/<name>.<channel>.j2` files (channels: `subject`, `body_text`).
The set today:

| template name           | description                                                | recipient                       |
|-------------------------|------------------------------------------------------------|---------------------------------|
| `magic_link`            | passwordless sign-in / enrolment link                      | recipient                       |
| `recovery_new_link`     | new recovery link after the previous one expired           | recipient                       |
| `invite_accept`         | workspace invite acceptance link                           | invitee                         |
| `passkey_reset_notice`  | a manager / owner reset their passkey (notify the user)    | the user whose passkey reset    |
| `passkey_reset_worker`  | a worker passkey reset, sent to the worker                 | worker                          |
| `email_change_notice`   | confirmation request for an email change                   | new address                     |
| `email_change_confirmed`| email change applied                                       | new address                     |
| `email_change_revert`   | undo-window link sent to the previous address              | previous address                |

#### §10.3 Digest emails (idempotency: `digest_record`)

The daily digest worker is bookkept by the `digest_record` table
(one row per `(workspace_id, recipient_user_id, period_start, kind)`
tuple, see `_DIGEST_KIND_VALUES` in
`app/adapters/db/messaging/models.py`). The `digest_record` row is
the **idempotency / replay ledger**, not a separate delivery path:
the actual fan-out still goes through `NotificationService` under
the `daily_digest` kind documented in §10.1 above. A duplicate
worker tick reads the existing `digest_record` for the same
`period_start` and skips re-sending.

`_DIGEST_KIND_VALUES` (`daily`, `weekly`) names the cadence; weekly
rollups are reserved (column accepts the value, no worker emits it
in v1).

#### §10.4 Future kinds (spec-only, not yet emitted)

The §10 prose has carried these labels since early drafts; none are
emitted in code today and the `NotificationKind` enum deliberately
does not list them. They land via additive enum-widening migrations
when each feature ships, with a §10.1 row added in the same change.
Umbrella tracking: **cd-vtm12**.

| label (spec)                    | proposed kind value             | recipient                                                                  | required? | tracking |
|---------------------------------|---------------------------------|----------------------------------------------------------------------------|-----------|----------|
| iCal feed error                 | `ical_feed_error`               | owners and managers                                                        | yes       | cd-vtm12 |
| availability override pending   | (use existing `approval_needed`)| owners and managers                                                        | yes       | rolled into `approval_needed` (cd-0loc5) |
| pre-arrival task unassigned     | `pre_arrival_task_unassigned`   | owners and managers                                                        | yes       | cd-vtm12 |
| task primary unavailable        | `task_primary_unavailable`      | owners and managers                                                        | yes       | cd-vtm12 |
| holiday schedule impact         | `holiday_schedule_impact`       | affected users                                                             | opt-out   | cd-vtm12 |
| invoice reminder                | `invoice_reminder`              | client user (biller's client grant) or billing workspace's owners/managers | opt-out (per `invoice_reminders.enabled` cascade setting, §22) | cd-vtm12 |
| invoice reminder exhausted      | `invoice_reminder_exhausted`    | owners and managers of the billing workspace                               | yes       | cd-vtm12 |
| property_workspace invite       | `property_workspace_invite`     | owners of the recipient workspace (if addressed) or the invite link's opener | yes     | cd-vtm12 |

The "agent approval pending" / "agent approval decided" labels from
earlier drafts are subsumed by `approval_needed` / `approval_decided`
in §10.1 — the approvals subsystem (§11) is the one source of truth
for both the agent-driven and human-driven branches.

### `email_opt_out`

| field         | type    | notes                                          |
|---------------|---------|------------------------------------------------|
| id            | ULID PK |                                                |
| workspace_id  | ULID FK |                                                |
| user_id       | ULID FK |                                                |
| category      | text    | matches `email_delivery.template_key` family   |
| opted_out_at  | tstz    |                                                |
| source        | enum    | `unsubscribe_link | profile | admin`           |

Before sending, the worker checks for an `email_opt_out` row matching
`(workspace_id, user_id, category)` where `category` is the kind's
snake_case value (e.g. `task_assigned`, `comment_mention`). Required
categories — those marked "yes" in §10.1 (`payslip_issued`,
`expense_approved`, `expense_rejected`, `expense_submitted`,
`issue_reported`, `approval_needed`, `agent_message`) and the entire
auth path (§10.2: `magic_link`, recovery, invite, passkey reset,
email change) — are never suppressed even if a row exists; the row
is kept for audit but ignored for those kinds. A wildcard category
(`'*'`) suppresses every opt-outable kind for that user without
listing each one.

### Delivery tracking

```
email_delivery
├── id
├── to_person_id
├── to_email_at_send           # snapshot
├── template_key
├── context_snapshot_json
├── sent_at
├── provider_message_id
├── delivery_state             # queued | sent | delivered | bounced | complaint | failed
├── first_error
├── retry_count
└── inbound_linkage            # reply-tracking, if any
```

### Daily digests

Sent at 07:00 local time per recipient (their timezone), by the
worker. Retries if SMTP fails; skipped if no noteworthy content.

- **Owner/manager digest** — today's upcoming tasks, stays arriving/
  leaving, overdue tasks, open issues, pending approvals (incl.
  availability override requests), low-stock items, warranties/
  certificates expiring soon (within `assets.warranty_alert_days`,
  §21), iCal errors, anomalies, expenses awaiting review, **unassigned
  pre-arrival tasks** (pull-back failed), **upcoming public holidays
  with scheduling impact**.
- **Worker digest** — "Today you have X tasks", grouped by property,
  with a quick link to the PWA.

## In-app messaging

The in-app messaging surface is the **task-scoped agent thread**
(§06 "Task notes are the agent inbox"). A `comment` row is no
longer a free list of user comments — it is an **event in the log
of a workspace-agent-mediated conversation** scoped to that task.

Message kinds in the log: `user | agent | system`. The workspace
agent (§11) is a **full participant**: it reads every message as it
is posted, can summarise the thread on demand, answer questions
grounded in instructions (§07), and speak in the thread on delegation
("@agent remind Maria the linen press is below her required
temperature"). Owners and managers read and reply through the same
thread on the desktop chat surface (§14); workers read and reply
through the worker chat page. Human `@mentions` resolve to workspace
members and still trigger email fallback for offline recipients.

There are still no DMs and no group chats outside a task thread.
If a manager wants a free-form conversation, they use the right-
sidebar workspace agent (§14), whose actions are audited like any
other agent write.

### Agent-message delivery

When the agent (§11) needs to reach a human out-of-band — a pending
approval card, a proactive heads-up, a reply to an off-app question
— the delivery worker walks a **fixed fallback chain** per recipient
and stops at the first channel that is both **configured** and
**capable of a human-visible alert** for that user:

1. **Live web session (SSE).** If the recipient currently has an
   open `/chat` page or an active `.desk__agent` sidebar for the
   relevant workspace, the message arrives over SSE
   (`agent.message.appended`, §14) and no out-of-band alert is sent.
   The "live" window is a deployment-wide constant (default 30s):
   if the SSE client has been connected within that window, skip
   fan-out to the lower tiers.
2. **OS push (native app).** If the recipient has at least one
   **active** `user_push_token` row (§12 `/me/push-tokens`) whose
   `last_seen_at` is within the deployment-wide freshness window
   (default 60 days), enqueue a push-notification delivery to
   **every** active token for that user. Payload is a small
   envelope (
   `workspace_slug`, `chat_thread_ref`, the first ~140 chars of the
   message, a deep-link URL to `/w/<slug>/chat#<message_id>`).
   The server **never** ships the full message body in the push
   payload — the OS notification is a "you have a message"
   alert; the app retrieves the body over HTTPS when the user taps.
   A push delivery that fails or is silently dropped does **not**
   cascade to WhatsApp or email — push delivery receipts are
   unreliable and the chain's next steps are only triggered when
   a channel is *unconfigured*, not when it *failed*.
3. **WhatsApp (§23 binding).** If the recipient has no active push
   token and has an **active** `chat_channel_binding` for the
   WhatsApp adapter on this workspace, enqueue a WhatsApp message
   via §23. Requires the WhatsApp adapter to be enabled for the
   deployment and the user to have consented via the binding
   flow. Unaffected by push-delivery outcomes (see above).
4. **Email fallback.** No push token, no WhatsApp binding → email.
   The message body renders inline in the email with a "Open in
   app" button linking to `/w/<slug>/chat#<message_id>` that
   authenticates via the existing session cookie or, if absent,
   walks the user through a passkey-login challenge and lands
   them on the chat page with the thread pre-opened.

Email is the fallback of record: every user has an email, email is
always enabled, and we never silently swallow an agent message.
There is no separate "notification preferences" table — the fan-out
walks capability presence (push tokens exist, WhatsApp binding
exists) and stops at the first configured tier. A user who wants
to stop receiving push removes their device via `/me/push-tokens`
(from the app on sign-out, or from `/me` on the web); a user who
wants to stop WhatsApp unlinks the binding (§23). Email cannot be
fully disabled for agent messages the same way magic-link emails
cannot be disabled for security-relevant categories.

All four tiers share the same `chat_message` / `chat_thread`
substrate (§23) — the per-tier adapter is a delivery path, not a
separate conversation model. The message_id that lands in push, in
WhatsApp, and in email is the same id; a reply on any channel
threads into the same conversation. Opt-in is **implicit in the
capability**: a push token registered by the native app means push
is on; a WhatsApp binding means WhatsApp is on. No separate
"preferred channel" toggle — the chain is the preference.

Notification timing is the user's device's job (OS-level do-not-
disturb, WhatsApp's own mute). The product does not carry its own
quiet-hours window. Per-binding `PAUSE <duration>` still works for
ad-hoc silence (§23). Per-workspace daily-cap controls still apply.

**v1 scope note.** Tier 1 (SSE) and tier 4 (email) ship in v1.
Tier 2 (push) ships when the native-app project lights up and
operator-level FCM/APNS credentials are provisioned; until then
`POST /me/push-tokens` returns `501 push_unavailable` and the
delivery worker skips the push tier. Tier 3 (WhatsApp) ships when
§23 adapters are enabled on the deployment. The fallback chain
above is **authoritative for v1** — it simply collapses to tier 1
→ tier 4 while the intermediate tiers are off.

**Web push registration surface (v1).** Browsers register a
`PushSubscription` via
`POST /w/<slug>/api/v1/messaging/notifications/push/subscribe`
(body matches the `PushSubscription.toJSON()` shape:
`{endpoint, keys:{p256dh, auth}, ua?}`) and unregister via
`POST /w/<slug>/api/v1/messaging/notifications/push/unsubscribe`
(body `{endpoint}`). Both are self-scoped — the caller
registers / un-registers only their own device. The SPA's service
worker fetches the workspace's VAPID public key from
`GET /w/<slug>/api/v1/messaging/notifications/push/vapid-key`
before calling `pushManager.subscribe`; the server caches this
value in-process for 5 minutes to cut repeat DB hits during
sign-in.

`PushSubscription.endpoint` URLs are validated against a fixed
allow-list of mainline web-push provider origins (`fcm.googleapis.com`,
`updates.push.services.mozilla.com`, `web.push.apple.com`) to
dodge SSRF amplification through the eventual delivery worker.
Non-`https://` schemes, URLs with userinfo, explicit non-443
ports, and `#fragment` segments are rejected at registration time
with `422 endpoint_scheme_invalid` / `422 endpoint_not_allowed`.
Query strings are accepted (some providers emit `?auth=...`).

The VAPID keypair lives in `workspace.settings_json` under
`messaging.push.vapid_public_key` (public) and the corresponding
private key — provisioned per workspace so a multi-tenant
deployment can rotate keys independently. The registration router
returns `503 vapid_not_configured` until the operator sets the
public key. Rotation is a CLI-driven operation and lands with the
push delivery worker (out of scope for cd-0bnz).

See §23 for the shared `chat_message` / `chat_thread` substrate and
§15 for the privacy rules that apply to off-app tiers.

### Auto-translation

When a user writes a message in a language other than the workspace's
`default_language` (§02), the agent:

1. Detects the language of the inbound message (`llm_call` with
   capability `chat.detect_language`).
2. Stores **both** the detected original message **and** a machine-
   translated copy in the workspace default language on the
   `comment` row:

   ```
   comment.body_md                # translated copy (workspace default lang)
   comment.body_md_original       # as written
   comment.language_original      # BCP-47, detected
   comment.translation_llm_call_id
   ```

3. Owners and managers see the workspace-default-language copy by
   default, with a **toggle** on the message to reveal the original.
   Workers see their own original plus the auto-translated copy if an
   owner or manager replies in a different language.

Agent-originated outbound messages are generated directly in the
user's `languages[0]` (§05) when known, falling back to the workspace
default. No second translation is stored for those — the message is
written once in the target language and the provenance is the
`llm_call` row.

See §18 for the broader translation policy.

## Issue reports

Any user taps **"Report an issue"** from a property/area context or
from a task:

```
issue
├── id
├── workspace_id
├── reported_by_user_id
├── property_id / area_id      # either
├── task_id?                   # if raised from a task
├── title
├── description_md
├── severity                   # low | normal | high | urgent
├── category                   # damage | broken | supplies | safety | other
├── state                      # open | in_progress | resolved | wont_fix
├── attachment_file_ids        # ULID[]; each id references `file` (§02)
├── converted_to_task_id       # when an owner or manager escalates
├── resolution_note
├── resolved_at
├── resolved_by
├── created_at / updated_at
└── deleted_at
```

Owner/manager actions: convert to task (one click → creates a handyman
task linked back to the issue), change state, add notes. Reporters see
state changes on their issue and can comment. Email to reporter on
resolution.

## Webhooks (outbound)

An agent or external system subscribes to events.

### Subscription

`POST /api/v1/webhooks`:

```json
{
  "name": "hermes-prod",
  "url": "https://hermes.example.com/crewday",
  "secret": "optional; system generates if omitted",
  "events": ["task.completed", "stay.upcoming"],
  "active": true
}
```

Subscription URL validation rejects blank URLs and non-HTTP(S)
schemes. It intentionally does not apply the shared SSRF fetch guard:
manager-configured internal destinations are trusted-role behavior per
§15 "SSRF", not an external attacker path.

### Event catalog (v1)

```
user.*               created, updated, archived, reinstated
role_grant.*         granted, revoked, updated
work_engagement.*    created, updated, archived, reinstated,
                     engagement_kind_changed
task.*               created, assigned, updated, started, completed,
                     complete_superseded, skipped, cancelled, overdue,
                     unassigned_pre_arrival, primary_unavailable
task_comment.*       created
stay.*               created, updated, upcoming, in_house, checked_out,
                     cancelled, conflict
stay_lifecycle_rule.* created, updated, deleted
stay_task_bundle.*   created, completed, cancelled
instruction.*        created, published, archived
inventory.*          low_stock, movement, stock_drift
chat_channel_binding.* created, verified, revoked, link_expired
chat_message.*       received, sent, delivered, failed
chat_thread.*        opened, archived
booking.*            scheduled, completed, amended, cancelled,
                     no_show, reassigned, declined,
                     pending_approval, approved, rejected
expense.*            submitted, approved, rejected, reimbursed
payroll.*            period_opened, period_locked, period_paid,
                     payslip_issued, payslip_paid,
                     payslip_destination_snapshotted,
                     payout_manifest_accessed
payout_destination.* created, updated, archived, verified
work_engagement_default_destination.* set, cleared
issue.*              reported, updated, resolved
approval.*           pending, decided
ical.*               polled, error
asset.*              created, updated, condition_changed,
                     status_changed, deleted, restored
asset_action.*       created, updated, performed,
                     schedule_linked, deleted
asset_document.*     created, updated, deleted, expiring
leave.*              requested, approved, rejected, decided
availability_override.* created, approved, rejected
public_holiday.*     created, updated, deleted
property_closure.*   created, updated, deleted
organization.*       created, updated, archived
client_rate.*        created, updated, archived
work_order.*         created, state_changed, accept_quote,
                     cancelled, deleted
quote.*              submitted, accepted, rejected, superseded, decided
vendor_invoice.*     submitted, approved, rejected, paid, voided,
                     proof_uploaded, reminder_sent, reminder_exhausted
booking_billing.*    resolved
property_workspace_invite.* created, accepted, rejected, revoked, expired
agent_preference.*   updated, cleared  (§11)
exchange_rate.*      refreshed, failed, overridden  (§09)
```

The `manager.*` and `employee.*` event families from earlier drafts
are replaced by `user.*`, `role_grant.*`, and `work_engagement.*`.
Subscribers that previously watched `manager.*` or `employee.*`
should update to `user.*`; `role_grant.*` covers permission lifecycle
and `work_engagement.*` covers employment/pay-pipeline lifecycle.

### Envelope

```json
{
  "event": "task.completed",
  "delivered_at": "…",
  "delivery_id": "whd_01J…",
  "data": { … event-specific payload … }
}
```

Headers:
- `X-Crewday-Signature: t=<unix>,v1=<hex>` (Stripe-style; cd-q885)
  where `<unix>` is the unix epoch second the signature was minted
  and `<hex>` is the lowercase hex encoding of
  `HMAC-SHA256(subscription_secret, f"{t}.{request_body}")`. The
  signing payload is the unix timestamp, a literal `.`, and the raw
  request body bytes; no trailing newline, no whitespace
  normalisation. Receivers MUST verify `abs(now - t) <= 300` (5
  minutes) to block replay; a 30-second clock-skew tolerance is
  recommended for receivers that can't guarantee perfect sync.
  Header names are case-insensitive (RFC 7230 §3.2); the canonical
  on-the-wire form is `X-Crewday-Signature`.
- `X-Crewday-Event`, `X-Crewday-Delivery`.

### Secret rotation

A workspace webhook secret is rotated via
`crewday admin webhook rotate --workspace <slug>`
(§13). The command mints a new secret, stamps it as `current`,
and holds the previous secret in a `previous` slot for **24
hours**. During that window each outbound delivery is sent
**twice** in a single HTTP POST by presenting the signature
computed under both secrets — concatenated as
`X-Crewday-Signature: t=<unix>,v1=<hex_current>,v1=<hex_previous>`;
compliant receivers accept a POST if either `v1=...` value
matches their configured secret. After 24 hours the
`previous` slot is discarded and only `current` signs;
receivers still holding the old secret fail with
`401 signature_mismatch` until they rotate too. The rotation
verb is a first-class CLI entry in §13. The cd-q885 dispatcher
ships the single-secret path; the rotation surface lands as a
follow-up.

### Retries

- On 2xx → `webhook_delivery.status='succeeded'`.
- On 4xx other than 408 / 429 → permanent failure. The row flips to
  `webhook_delivery.status='dead_lettered'` immediately, with no
  further attempts; one `audit.webhook_delivery.dead_lettered` row
  lands. The receiver said "no", not "try again later".
- On 408 / 429 / 5xx / network error / timeout (10 s) → transient.
  The retry schedule is `[0s, 30s, 5m, 1h, 6h, 24h]` — six attempts
  total (cd-q885). Each transient response stamps
  `next_attempt_at = last_attempted_at + RETRY_SCHEDULE_SECONDS[attempt]`
  and the dispatcher's 30 s tick refires the row when the window
  opens. After the 6th attempt the row dead-letters with audit; the
  full delivery window is ~31 h.
- A subscription whose last 24 h of deliveries are all non-2xx is
  marked `unhealthy` and **paused** (no new deliveries enqueued) once
  at least 3 deliveries exist in that window. The deployment knobs are
  `webhook_health_window_h` (default `24`) and
  `webhook_health_min_deliveries` (default `3`). The manager is
  notified and one `audit.webhook_subscription.auto_paused` row lands
  with the system actor, workspace id, pause reason, and threshold
  values. A manager or a token with `messaging:write` can call
  `POST /webhooks/{id}/enable` to resume; enabling re-opens the queue
  and clears `paused_reason` / `paused_at` but does not replay the
  dropped deliveries (use `/replay` for that).

### Delivery log retention

`webhook_delivery` rows are retained for 90 days by default
(configurable; see §02 operational-log retention).

### Replay / backfill

`POST /api/v1/webhooks/{id}/replay {since, until, events[]}` replays
the matching events. Idempotency is the receiver's responsibility,
but every delivery carries a stable `delivery_id`.

## Webhooks (inbound)

Inbound webhooks are endpoints we **receive** from external
systems — primarily email delivery providers (SES, SendGrid,
Postmark) reporting bounces, complaints, and opens. Every
inbound webhook MUST **verify the sending provider's
signature** before the handler reads any payload field:

- **AWS SES via SNS** — validate the SNS signature against the
  pinned `SigningCertURL` set (AWS SES SigningCertURL hosts);
  reject any message whose `SigningCertURL` does not match.
- **SendGrid signed events** — verify
  `X-Twilio-Email-Event-Webhook-Signature` with the per-
  workspace configured public key.
- **Postmark, Mailgun, etc.** — use each provider's documented
  signing scheme; store the shared secret in
  `secret_envelope`.

Unsigned or mis-signed payloads return `401 signature_missing`
(for "no signature header") or `401 signature_invalid` (for
"header present, HMAC mismatch"), write
`audit.webhook_inbound.signature_rejected`, and raise an
operator alert. **If a provider does not support signing, the
integration does not ship** — v1 does not accept unauthenticated
inbound webhooks under any feature flag.

The `POST /webhooks/email/bounce` route is the only inbound
webhook in v1; future inbound integrations (calendar-provider
push, payout-provider status) follow the same rule.

**Note on iCal polling.** iCal feed polling (§04) is **not** an
inbound webhook — it is outbound HTTP initiated by our worker,
and falls under the §04 "SSRF guard" rules rather than this
section's signing rules.

## CLI (examples)

```
crewday webhooks add --name hermes --url https://… \
                       --events task.completed,stay.upcoming
crewday notifications test --to owner@example.com --template daily_digest
crewday issues list --property prop_… --state open
crewday comments post <task-id> "ping @maria re: linens"
```
