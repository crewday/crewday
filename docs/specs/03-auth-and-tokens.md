# 03 — Authentication, sessions, tokens

## Principles

- **No passwords.** For anyone. Not for owners, not for workers.
- **Passkeys (WebAuthn)** are the only human credential.
- **Magic links** are only an enrollment mechanism — they register a
  passkey; they do not authenticate a session on their own.
- **Standalone agents** use long-lived, revocable, scope-limited API
  tokens. **Embedded agents** (§11) use delegated tokens that inherit
  the calling user's full permissions (see "Delegated tokens" below).
- The server never stores anything that can be replayed if the DB
  leaks: credentials are public keys, tokens are stored as argon2id
  hashes.
- Every enrollment, login, rotation, and revocation writes to the
  audit log (§02).

## Actors

- **User.** Every human is a `users` row (§02) with at least one
  `role_grants` row that authorises them somewhere. Authority comes
  from the grant, not from a `kind` column — a user may be an
  `owner` of one workspace, a `worker` in another, and a `client`
  on a single property in a third, simultaneously. All actions by
  humans — regardless of grant — log as `actor_kind = 'user'`;
  the grant under which the action was authorised is captured in
  `actor_grant_role` (§02 audit_log).
- **Agent.** Non-human. Standalone agents are identified by a scoped
  API token; never by a session. Embedded agents (§11) use **delegated
  tokens** that act as the creating user — their `actor_kind` in audit
  is `user`, with `agent_label` and `agent_conversation_ref` set so
  the row is clearly flagged as agent-executed.
- **System.** The worker process itself, when generating scheduled
  tasks, sending digests, polling iCal. No token — identified by a
  reserved `actor_id = "00000000000000000000000000"` in the audit log.

**"Manager", "employee", "client"** are **grant roles**, not user
kinds. Enrollment, passkey management, session shape, magic-link
flow, and break-glass recovery are identical across them; the
differences are entirely in what the user sees and can do once
authenticated (see §05 capability catalog and §02 role_grants).

## Enrollment flows

Enrollment is unified: the same REST endpoints, magic-link flow,
and passkey ritual enroll every user, regardless of which grants
they will hold. The only things that vary by grant_role are the
default magic-link TTL and whether break-glass codes are issued on
acceptance (see "Break-glass codes" below).

### Owner (first boot)

1. First-boot wizard runs once when the DB has no `users` rows. The
   CLI `miployees admin init --email owner@example.com` creates the
   workspace and emails the owner a **bootstrap magic link** valid for
   15 minutes. The wizard inserts a `users` row and a single
   `role_grants` row with
   `(scope_kind='workspace', scope_id=<ws>, grant_role='owner')`.
2. Owner clicks the link, chooses a display name and timezone,
   registers a passkey on their current device.
3. System generates **break-glass recovery codes** (8 codes, 10 chars
   Crockford base32, shown once, stored argon2id-hashed in
   `break_glass_code`). Owner must confirm "I wrote them down" before
   proceeding. Each code is single-use: a successful code
   redemption generates exactly one magic link (15-min TTL) and marks
   the code row `used_at = now()`. The consumed code is inert even if
   the resulting magic link expires unused — the owner must consume
   another code to get a fresh link.

### Additional users (invite)

- A user with an appropriate `users.invite` capability (owners and
  managers by default) invites another via
  `POST /api/v1/users/invite` with
  `{ email, display_name, grants: [ {scope_kind, scope_id, grant_role,
    binding_org_id?, capability_override?}, ... ],
     work_engagement?: {workspace_id, engagement_kind, ...},
     user_work_roles?: [ {workspace_id, work_role_id}, ... ] }`.
  One call creates (or re-uses, if `email` matches an existing row)
  the `users` row, inserts the requested grants, optionally adds the
  work engagement and work-role mappings, and emails a magic link.
- System emails a magic link (TTL depends on the primary grant:
  `owner` and `manager` grants default to 24 h; `worker`, `client`,
  and `guest` also 24 h). On acceptance, recipient registers a
  passkey.
- Users invited into an `owner` or `manager` grant also receive a
  set of break-glass codes on their first passkey registration.
  Lower-privilege grants do not — recovery for those users runs
  through an owner/manager-initiated re-issue of a magic link.

### Existing user, new grant

- When the invite's `email` matches an existing `users` row, no
  new user is created. The new `role_grants` rows are inserted and
  the user receives a one-shot **grant-activated** email — no magic
  link, no passkey re-registration, no break-glass regeneration.
  They just sign in with their existing passkey and the new scope
  appears in their workspace switcher.

### Additional passkeys

- Any logged-in user can add another passkey from their profile page.
  Up to 5 passkeys per user.
- Each passkey carries a user-editable `nickname` ("work phone",
  "wife's iPad").

### Re-enrollment side-effects

When an owner or manager re-issues a magic link to a user ("lost
phone" / "lost device" paths below), accepting the link and
registering a fresh passkey:

1. Revokes **all existing passkeys** for that user (the new one is
   written after revocation in the same transaction).
2. Revokes **all active sessions** for that user; they must log in
   again on every previously-signed-in browser.
3. For users who hold an `owner` or `manager` grant anywhere,
   regenerates the break-glass code set (old codes invalidated).
   Users who hold only `worker` / `client` / `guest` grants have no
   code set to regenerate.

All three events land in the audit log under `auth.reenroll`.

## Login

- `/login` renders a single button: **"Continue with passkey"**.
- WebAuthn conditional UI (`mediation: "conditional"`) is used so
  browsers that support it prompt silently from the username field;
  browsers that do not fall back to the button.
- Passkey credential ID discovers the user — we do not ask for email.
- Successful assertion issues a session cookie.

## Sessions

- Session cookie: `__Host-miployees_session`.
- Flags: `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, no `Domain`.
- Value: opaque random 192-bit token → hashed row in `sessions` table.
- Lifetime: 7 days for users whose highest active grant on any
  scope is `owner` or `manager`; 30 days for everyone else
  (configurable). Recomputed on login, not mid-session — a user who
  gains a manager grant mid-session keeps the longer lifetime until
  their next login. Refreshed on each request after half its
  lifetime has elapsed.
- CSRF: Authenticated SPA requests carry a double-submit token
  (`miployees_csrf` cookie + `X-CSRF` header) for every non-GET. Same
  origin is enforced by `SameSite=Lax` for initial navigation.

## API tokens

### Creation

- Any user with the `users.invite` grant-capability (owners and
  managers by default) creates a token via the UI or
  `POST /api/v1/auth/tokens`:
  ```json
  {
    "name": "hermes-scheduler",
    "scopes": ["tasks:read", "tasks:write", "stays:read"],
    "expires_at": "2027-01-01T00:00:00Z",
    "note": "nightly scheduling agent"
  }
  ```
- Response shows the **plaintext token once**; never again.
- Token format: `mip_<key_id>_<secret>` where `key_id` is a public
  ULID and `secret` is 256 bits of base32. Only the argon2id hash of
  the secret is stored; the key_id is stored in the clear so that
  every request can be O(1) located.

### Delegated tokens

A **delegated token** is created by a logged-in user and inherits
**all permissions** of that user for as long as the user's account is
active and unarchived. This is the mechanism the embedded chat
agents (§11) use to act on behalf of their user.

```json
POST /api/v1/auth/tokens
{
  "name": "chat-agent",
  "delegate": true,
  "expires_at": "2026-05-16T00:00:00Z",
  "note": "Embedded agent for desktop sidebar"
}
```

Key properties:

- `delegate_for_user_id`: ULID of the creating user — set from the
  session. Not caller-supplied.
- `scopes`: **empty**. Permission checks resolve against the
  delegating user's `role_grants` (and the work-role narrowing rules
  in §05), not against explicit token scopes. If the user's grants
  change (new grant added, existing grant revoked, property
  reassignment), the delegated token's effective permissions change
  immediately.
- If the delegating user is archived, globally deactivated, or loses
  every non-revoked grant, requests with the token return `401`
  with a clear message.
- A delegated token can only be created by a **passkey session** — it
  cannot be created by another token (no transitive delegation).
- Default TTL: **30 days** (shorter than the 90-day default for scoped
  tokens). A workspace-level setting can raise it, with the same
  noisy warning as for scoped tokens.
- Revocation: the delegating user can revoke their own delegated
  tokens; any user with the `users.revoke_grant` grant-capability
  (owners and managers by default) in any scope that the delegating
  user is active in can revoke that user's delegated tokens.

**`api_token` columns for delegation:**

| column                | type   | notes                                     |
|-----------------------|--------|-------------------------------------------|
| `delegate_for_user_id` | ULID? | nullable; references `users.id`           |

When null the token is a classic scoped token (backward
compatible). When set, it is a delegated token; the
`actor_kind` in audit for requests using the token is `user`, with
`actor_id = delegate_for_user_id`, `agent_label = api_token.name`,
and the optional `agent_conversation_ref` header propagated in.

### Scopes

Fine-grained, resource-scoped verbs. A standalone agent should be
issued the narrowest set possible. **Delegated tokens ignore scopes
entirely** — permissions are resolved from the delegating user's
access.

- `tasks:{read,write,complete}`
- `users:{read,write}`            (identity, grants, engagements)
- `properties:{read,write}`
- `stays:{read,write}`
- `inventory:{read,write,adjust}`
- `time:{read,write}`
- `expenses:{read,write,approve}`
- `payroll:{read,run}`
- `instructions:{read,write}`
- `messaging:{read,write}`
- `llm:{read,call}` — `call` required to execute model calls chargeable
  to the workspace
- `admin:{impersonate,rotate,purge}` — rare; requires approval of
  another owner or manager before first use (see §11 approval workflow)

`*:read` implied by `*:write`. `admin:*` implies nothing else — it is a
narrow escape hatch.

### Usage

`Authorization: Bearer mip_<key_id>_<secret>`

- 401 on absent or malformed token.
- 403 on insufficient scope (with `WWW-Authenticate: error="insufficient_scope"
  scope="tasks:write"`).

### Revocation and rotation

- Any user with the `users.revoke_grant` grant-capability (owners
  and managers by default) in the token's home workspace can revoke
  any token in that workspace; scoped tokens and their own delegated
  tokens are always revocable by the creator. Revocation takes effect
  within 5 seconds (token cache TTL).
- Tokens can be rotated in place: the old secret hash is kept alongside
  the new for a configurable overlap (default 1h), so long-running
  agents can reload without downtime.
- A **per-token audit log view** is available in the UI: every request,
  method, path, response status, and `audit_correlation_id` link.

### Guardrails

- Tokens cannot create tokens unless scope `admin:rotate` is granted.
- Tokens cannot accept their own `admin:*` approval (§11).
- Scoped tokens default to 90 days TTL if `expires_at` is omitted;
  delegated tokens default to 30 days. A workspace-level setting can
  raise either default to "never" but emits a noisy warning in the UI.
- Delegated tokens cannot create other delegated tokens (no transitive
  delegation). A delegated token cannot outlive its delegating user's
  account — archiving the user effectively revokes all their delegated
  tokens.
- IP allow-lists optional per token (CIDR, comma-separated). Violations
  log and 403.

## Recovery paths

| Situation                                  | Recovery path                                                  |
|--------------------------------------------|----------------------------------------------------------------|
| Worker/client lost phone                   | Any user with `users.revoke_grant` on a shared scope (owners and managers by default) clicks "re-issue magic link" on the user's profile; current passkeys are revoked on registration. |
| Owner/manager lost only device, has backup code  | Enter recovery code → magic link emailed → register passkey; one backup code is burnt. |
| Owner/manager lost device + all backup codes, another owner or manager exists on a shared scope | That peer re-issues a magic link to their email. |
| Last owner/manager locked out completely         | **Host-CLI recovery only in v1.** Stop service, run `miployees admin recover --email ...` on the host, which emits a one-time magic link to stdout. Operator must have shell access to the deployment host. Hosted / SaaS recovery flows (support escalation, out-of-band identity verification) are **out of scope for v1** — see §19. |
| Email address wrong / changed              | An owner/manager on a shared scope updates email on the user's profile; next magic link goes to the new one. Since email is globally unique (§02), the change fails if another `users` row already holds that address. |

## Break-glass codes

```
break_glass_code
├── id                   ULID PK
├── workspace_id         ULID FK
├── user_id              ULID FK
├── hash                 argon2id digest of the code
├── hash_params          argon2id parameters (for upgrade)
├── created_at           tstz
├── used_at              tstz?  (null until redeemed)
└── consumed_magic_link_id ULID?  populated on redemption
```

Redemption: the user (whose codes were issued because they hold an
`owner` or `manager` grant) submits the plaintext code to
`POST /auth/magic/consume` with their email. On success the code's
`used_at` is set, a fresh `magic_link` is issued (15-min TTL), and its
id is stored in `consumed_magic_link_id`. A used code is inert even
if the resulting magic link expires unused.

## Magic link format

- URL: `https://<host>/auth/magic/{token}`
- `token` is an `itsdangerous` signed blob: `{ purpose, subject_id,
  jti, exp }` signed with the workspace's magic-link key.
- Single use (`jti` recorded on successful consumption).
- Open attempts after consumption or expiry show a polite re-request
  page, and rate-limit the offending IP (§15).

## WebAuthn specifics

- RP ID: configured hostname (e.g. `ops.example.com`).
- User verification: `required` (matches "passkey" semantics).
- Authenticator attachment: `platform` preferred, `cross-platform`
  allowed (YubiKey for owners/managers).
- Resident keys (discoverable credentials): `preferred`.
- Attestation: `none` — we trust the browser's RP ID binding.
- Algorithms: ES256 (`-7`), RS256 (`-257`) for broader iOS/Android
  support.

## Privacy

- We store only: credential ID, public key, sign count, AAGUID,
  transport hints, nickname, last_used_at.
- We never store a device fingerprint beyond AAGUID (which only
  identifies the authenticator model).
- `last_used_at` is visible only to the owning user.
