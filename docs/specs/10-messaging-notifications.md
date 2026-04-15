# 10 — Messaging, notifications, webhooks

## Channels

v1 ships exactly **one** outbound messaging channel to humans:
**email**. Everything else (SMS, WhatsApp, push, Slack) is left as a
deliberate non-goal per the user's selection; §18 documents the seam.

## Email

### Provider

SMTP (RFC 5321). Config:

- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`
- `SMTP_SECURE` (`starttls` | `tls` | `none`)
- `MAIL_FROM`, `MAIL_REPLY_TO`

Provider-agnostic so the user can wire up Postmark, SES, their own
Postfix, or Resend via SMTP bridge.

### Template system

Jinja2 templates under `app/templates/email/`. MJML compiled at build
time into plain HTML. Every email is **both** HTML and plaintext. No
external CSS. Preheader text as a hidden first div.

### Emails the system sends

| event                          | to                      | required?     |
|--------------------------------|-------------------------|---------------|
| magic link (enrollment / recovery) | recipient           | yes           |
| daily manager digest           | each manager            | opt-out       |
| daily employee digest          | each employee           | opt-out       |
| task overdue alert             | assignee + manager      | opt-out       |
| task comment mention           | mentioned person        | opt-out       |
| issue reported                 | managers                | yes           |
| expense submitted              | managers                | yes           |
| expense decision               | submitting employee     | yes           |
| payslip issued                 | employee                | yes           |
| iCal feed error                | managers                | yes           |
| anomaly detected (§11)         | managers                | opt-out       |
| agent approval pending         | managers                | yes           |

Opt-outs are per-person, per-category, via a signed unsubscribe link
in the footer of each email. Required emails (security-relevant, or
legally equivalent) cannot be unsubscribed but throttle by priority.

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
├── delivery_state             # queued | sent | delivered | bounced | failed
├── first_error
├── retry_count
└── inbound_linkage            # reply-tracking, if any
```

### Daily digests

Sent at 07:00 local time per recipient (their timezone), by the
worker. Retries if SMTP fails; skipped if no noteworthy content.

- **Manager digest** — today's upcoming tasks, stays arriving/leaving,
  overdue tasks, open issues, pending approvals, low-stock items,
  iCal errors, anomalies, expenses awaiting review.
- **Employee digest** — "Today you have X tasks", grouped by property,
  with a quick link to the PWA.

## In-app messaging

Only one in-app messaging surface: **task comments** (§06).

Threaded per task, markdown, `@mentions`, email notification for
mentions. No DMs, no group chats, no presence. If managers want that,
they run WhatsApp.

## Issue reports

Employee taps **"Report an issue"** from a property/area context or
from a task:

```
issue_report
├── id
├── reported_by_employee_id
├── property_id / area_id      # either
├── task_id?                   # if raised from a task
├── title
├── description_md
├── severity                   # low | normal | high | urgent
├── state                      # open | in_progress | resolved | wont_fix
├── attachment_file_ids
├── converted_to_task_id       # when a manager escalates
├── resolution_note
├── resolved_at
└── resolved_by
```

Manager actions: convert to task (one click → creates a handyman task
linked back to the issue), change state, add notes. Employees see
state changes on their issue and can comment. Email to reporter on
resolution.

## Webhooks (outbound)

An agent or external system subscribes to events.

### Subscription

`POST /api/v1/webhooks`:

```json
{
  "name": "hermes-prod",
  "url": "https://hermes.example.com/miployees",
  "secret": "optional; system generates if omitted",
  "events": ["task.completed", "stay.upcoming"],
  "active": true
}
```

### Event catalog (v1)

```
person.*             created, updated, archived
task.*               created, assigned, started, completed, skipped,
                     cancelled, overdue
task_comment.*       created
stay.*               created, updated, upcoming, in_house, checked_out,
                     cancelled, conflict
instruction.*        created, published, archived
inventory.*          low_stock, movement
shift.*              opened, closed, adjusted, disputed
expense.*            submitted, approved, rejected, reimbursed
payroll.*            period_opened, period_locked, payslip_issued
issue.*              reported, updated, resolved
approval.*           pending, decided
ical.*               polled, error
```

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
- `X-Miployees-Signature: t=<unix>,v1=<hex HMAC-SHA256>`
  over `t.raw_body`; secret is the subscription's secret.
- `X-Miployees-Event`, `X-Miployees-Delivery`.

### Retries

- On 2xx → delivered.
- On non-2xx or timeout (10s): exponential backoff (1m, 5m, 30m, 2h,
  12h), up to 48h. Failures beyond that mark the subscription
  `unhealthy`; the manager is notified and the subscription is paused
  after 24h of sustained failure.

### Replay / backfill

`POST /api/v1/webhooks/{id}/replay {since, until, events[]}` replays
the matching events. Idempotency is the receiver's responsibility,
but every delivery carries a stable `delivery_id`.

## CLI (examples)

```
miployees webhooks add --name hermes --url https://… \
                       --events task.completed,stay.upcoming
miployees notifications test --to owner@example.com --template daily_digest
miployees issues list --property prop_… --state open
miployees comments post <task-id> "ping @maria re: linens"
```
