# Browser UI Test Plan

This is the durable TODO list for proving crew.day's browser-visible
features in a real browser. It is intentionally broader than the current
implementation: specs remain the source of truth, while `app/web/` and
`mocks/web/` show what exists today.

Do not treat unchecked items as failures by default. Treat them as work
to automate, smoke manually, or explicitly mark out of scope for the
current release phase.

## Sources

- App specs: `docs/specs/00-overview.md` through `docs/specs/25-marketplace.md`.
- Site specs: `docs/specs-site/README.md` through `docs/specs-site/05-roadmap.md`.
- Production frontend: `app/web/src/App.tsx`, `app/web/src/pages/`, `app/web/src/components/`, `app/web/src/auth/`, `app/web/src/context/`, `app/web/src/lib/`.
- Mock frontend: `mocks/web/src/App.tsx`, `mocks/web/src/pages/`, `mocks/web/src/components/`.
- Site frontend code: not present in this worktree at the time this plan was written; site checks are derived from `docs/specs-site/`.

## How Agents Should Use This Plan

- Run browser checks against `http://127.0.0.1:8100`, not `https://dev.crew.day`.
- Bring up the app with `./scripts/dev-stack-up.sh` before browser smoke.
- Use Playwright browser evidence for anything visual, interactive, offline, passkey-like, camera-like, or responsive.
- Use `./scripts/agent-curl.sh` only to seed or inspect state around a browser journey, not as a replacement for the UI check.
- Capture screenshots under `.playwright-mcp/` when documenting a failure.
- Close the browser after Playwright work.
- When a checklist item exposes a product/spec ambiguity, stop and ask or file a Beads task before guessing.
- When a checklist item exposes an obvious bug, file a Beads task if it cannot be fixed in the same turn.

## Global Acceptance Gates

- [ ] Each canonical route renders without a blank screen, uncaught console error, or infinite loading state.
- [ ] Each protected route redirects unauthenticated visitors to login with a safe `next` target.
- [ ] Each role sees only the routes and nav items granted by its permissions.
- [ ] Each mutating form has loading, success, validation-error, server-error, and retry behavior.
- [ ] Each destructive or irreversible action has the confirmation required by the relevant spec.
- [ ] Each optimistic mutation rolls back or reconciles correctly when the server rejects it.
- [ ] SSE updates refresh or invalidate the visible page state without a full reload.
- [ ] Offline worker flows queue locally, survive reload where specified, replay when back online, and show pending state.
- [ ] Mobile, tablet, and desktop layouts work at representative widths: 390px, 768px, 1280px, and 1440px.
- [ ] Light, dark, and system theme render readable controls and native form chrome.
- [ ] `prefers-reduced-motion` suppresses nonessential animation.
- [ ] Keyboard-only navigation reaches every interactive element in visual order.
- [ ] Focus is trapped inside modals and returned to the opener when closed.
- [ ] Forms have accessible labels, not placeholder-only labels.
- [ ] Error messages are announced or placed near the failing control.
- [ ] Pseudolocale does not clip, overlap, or break primary workflows.
- [ ] No UI leaks full secrets, full bank details, full tokens, hidden PII, or cross-tenant identifiers.
- [ ] Audit-sensitive actions leave a visible audit trail where the UI exposes audit history.

## Test Data Matrix

- [ ] Workspace A: household owner/manager workspace with properties, areas, units, workers, schedules, stays, inventory, assets, expenses, and agent budget usage.
- [ ] Workspace B: second workspace for the same user to prove workspace switch and cross-tenant isolation.
- [ ] Worker user with only worker permissions.
- [ ] Manager user with normal manager permissions.
- [ ] Owner/root user with settings, permissions, token, and approval privileges.
- [ ] Client user with only client portal access.
- [ ] Deployment admin user with `/admin` access.
- [ ] Non-admin authenticated user attempting `/admin`.
- [ ] Archived user.
- [ ] User with one workspace.
- [ ] User with multiple workspaces.
- [ ] Trusted workspace and pre-verification workspace with tight signup/demo caps.
- [ ] Properties: single-unit home, multi-unit building, STR/vacation property, client-owned property, shared property.
- [ ] Schedules: one-off, weekly simple schedule, RRULE advanced schedule, paused schedule, ended schedule.
- [ ] Tasks: due today, overdue, completed, skipped, cancelled, personal, stay-generated, asset-action-generated, inventory-consuming.
- [ ] Evidence policies: none, optional photo, required photo, required checklist, inherited policy.
- [ ] Stays: future, current, checked-out, overlapping conflict, cancelled/removed iCal event.
- [ ] Inventory: low stock, out of stock, decimal quantity, transfer pending, stocktake in progress.
- [ ] Assets: active, retired, deleted, QR token valid, QR token revoked, document extraction pending/failed/done.
- [ ] Expenses: draft, pending approval, approved, rejected, reimbursed, foreign-currency, low-confidence scan.
- [ ] Agent budget: normal, warning threshold, at cap, paused.

## Public Auth And Onboarding

Routes and surfaces: `/login`, `/recover`, `/recover/enroll`, `/signup`, `/signup/verify`, `/signup/enroll`, `/auth/magic/:token`, `/accept/:token`, `/guest/:token`, `/w/:slug/guest/:token`, workspace picker/gate.

- [ ] Anonymous visit to `/` lands on login or workspace selection according to auth state.
- [ ] Login starts a passkey ceremony and disables duplicate submits while pending.
- [ ] Login handles browser without `PublicKeyCredential`.
- [ ] Login handles cancelled passkey ceremony.
- [ ] Login handles invalid credential and expired challenge.
- [ ] Login honors safe `next=` values after success.
- [ ] Login drops unsafe `next=` values, including non-admin users aimed at `/admin`.
- [ ] Logged-in non-admin visiting `/login?next=/admin/...` lands on role home, not admin.
- [ ] Signup hidden/closed state renders correctly when self-serve signup is disabled.
- [ ] Signup accepts valid email and workspace slug.
- [ ] Signup rejects invalid email, disposable domain, reserved slug, taken slug, homoglyph/confusable slug, and too-short slug.
- [ ] Signup Turnstile/CAPTCHA state renders when configured.
- [ ] Signup verify handles valid, expired, consumed, malformed, and wrong-purpose tokens.
- [ ] Signup enroll runs passkey create and then logs in or lands on the expected next page.
- [ ] Signup enroll handles unsupported passkeys and cancelled ceremonies.
- [ ] Invite accept handles new user path, existing user path, accepted token replay, expired token, malformed token, and workspace mismatch.
- [ ] Recovery request returns generic success copy regardless of whether the email exists.
- [ ] Recovery owner/manager path includes break-glass code where specified.
- [ ] Recovery enroll revokes old credentials and lands in normal post-login state.
- [ ] Archived user cannot proceed into workspace UI.
- [ ] Session expiry during a protected page redirects without losing an unsafe amount of UI state.
- [ ] Logout closes SSE and returns to public state.
- [ ] Guest welcome route shows active token contents.
- [ ] Guest welcome route handles expired, revoked, malformed, and wrong-workspace token states.

## Workspace, Shell, Navigation, And RBAC

- [ ] Single-workspace users auto-adopt that workspace.
- [ ] Multi-workspace users see a picker and can switch workspaces.
- [ ] Workspace switch updates route, visible data, SSE subscription, and cached query state.
- [ ] Cross-workspace deep link for an inaccessible workspace returns the correct not-found/redirect state.
- [ ] Worker mobile shell shows bottom tabs: Today, Schedule, Chat, My Expenses, Me.
- [ ] Worker desktop shell uses side nav and right-side agent rail rather than mobile bottom chat tab.
- [ ] Manager shell shows manager nav groups and the shared agent rail.
- [ ] Client shell shows only client routes.
- [ ] Admin shell is bare-host and never workspace-prefixed.
- [ ] Unauthorized manager routes are hidden from nav and blocked on direct URL.
- [ ] Non-admin authenticated user visiting `/admin` lands on role home.
- [ ] Admin user can navigate every admin route.
- [ ] Route wildcard redirects safely without a blank screen.
- [ ] Legacy routes `/week`, `/me/schedule`, `/bookings`, and `/shifts` redirect to `/schedule`.
- [ ] Workspace-prefixed aliases that exist in `App.tsx` work where promised.

## Profile, Preferences, Tokens, And Identity Self-Service

Route: `/me`.

- [ ] Profile renders personal details, role/workspace summary, locale, notification preferences, and settings available to the viewer.
- [ ] Theme segmented control persists light, dark, and system values.
- [ ] Avatar upload opens editor, crops, saves, updates visible avatar, and handles invalid file type/large file.
- [ ] Additional passkey registration succeeds.
- [ ] Last-passkey deletion is blocked or routed to recovery according to spec.
- [ ] Email change request sends verification, shows pending state, and handles conflict/recent-reenrollment errors.
- [ ] Email change verify succeeds only for the matching signed-in user.
- [ ] Email revert link works without an app session and shows clear success/failure copy.
- [ ] Personal access token panel creates a token, reveals plaintext once, copies it, hides after dismissal, lists masked tokens, rotates/revokes where supported, and shows token audit.
- [ ] Agent preferences save per-user approval mode and notification preferences.
- [ ] Chat channel binding card links, verifies, unlinks, and handles duplicate or invalid channel codes.

## Employee Today And Task Detail

Routes: `/today`, `/task/:tid`.

- [ ] Today shows due, overdue, upcoming, completed, empty, loading, and error states.
- [ ] Today quick-add creates a personal task with expected defaults.
- [ ] Today quick-complete applies optimistic UI, confirms, and reconciles from server/SSE.
- [ ] Today quick-complete queues offline and replays later.
- [ ] Today search/facets/filtering work for status, property, area, priority, assignee, and due window where present.
- [ ] Task detail loads title, due time, property/area, assignment, instructions, checklist, evidence, comments, and agent inbox.
- [ ] Required checklist items block completion until satisfied.
- [ ] Required evidence blocks completion until satisfied.
- [ ] Evidence upload supports camera/file chooser, preview, remove, retry, and server rejection.
- [ ] Completion with inventory effects shows the expected preview or warning.
- [ ] Skip requires a reason where specified and updates state.
- [ ] Cancellation is visible only to authorized roles and requires the specified confirmation.
- [ ] Checklist toggles are optimistic and roll back on failure.
- [ ] Task comments post, render pending state, handle moderation/rejection, and update via SSE.
- [ ] Agent/task note composer handles empty message, long message, attachments where supported, and retry.
- [ ] Task detail not-found and unauthorized states do not leak cross-tenant data.

## Employee Schedule, Time, Leave, And Bookings

Routes: `/schedule`, `/scheduler`, legacy redirects.

- [ ] Schedule renders phone day view and desktop agenda correctly.
- [ ] Infinite schedule loads past/future ranges without duplicate days.
- [ ] Today jump returns to current local property day.
- [ ] Day drawer shows tasks, bookings, leave, availability override, holidays, and closures in the right order.
- [ ] Availability rail reflects weekly pattern, overrides, leave, public holidays, and property closures.
- [ ] Worker can request leave with valid dates, reason, and optional partial-day state.
- [ ] Worker leave request rejects invalid date ranges and overlapping constraints.
- [ ] Worker can cancel own pending/future leave where allowed.
- [ ] Worker can request availability override, including the hybrid approval behavior for reductions.
- [ ] Booking amend handles overrun, underrun, required reason, pending approval, approved, rejected, and stale state.
- [ ] Future booking decline works and past booking decline is blocked.
- [ ] Ad-hoc booking propose dialog creates a pending booking with property/engagement/time/reason.
- [ ] Booking pending/approved/rejected states render in day drawer and history.
- [ ] Manager `/scheduler` filters by property, worker, role, and date range.
- [ ] Manager `/scheduler` warning markers show unassigned, unavailable, closure, holiday, and conflict states.
- [ ] Manager inline edit/reassign flow works or is explicitly unavailable.
- [ ] `/schedules` simple editor creates daily/weekly/monthly schedules.
- [ ] `/schedules` advanced RRULE editor validates RRULE/RDATE/EXDATE and previews occurrences.
- [ ] Schedule pause/resume, active range edit, and delete semantics match the spec.

## Employee Expenses

Route: `/my/expenses`.

- [ ] Manual expense entry validates merchant, date, amount, currency, category, engagement, and notes.
- [ ] Receipt upload supports camera/file, progress, retry, remove, and unsupported file error.
- [ ] Scan flow moves through upload, processing, review, and submitted phases.
- [ ] Scan autofill shows confidence states and leaves low-confidence fields blank with review prompt.
- [ ] Agent follow-up question prompt accepts an answer and updates fields.
- [ ] Draft save, submit, and submit failure states are visible.
- [ ] Offline expense draft/receipt queue survives navigation/reload where specified.
- [ ] Recent expenses list shows draft, pending, approved, rejected, reimbursed, and empty states.
- [ ] Owed-to-you/pending reimbursement panel matches the canonical endpoint once route drift is resolved.
- [ ] Worker cannot see another worker's expenses via direct URL or altered identifiers.

## Employee Chat, Issues, Assets, And History

Routes: `/chat`, `/issues/new`, `/asset/scan`, `/asset/scan/:token`, `/asset/:aid`, `/history`.

- [ ] Worker chat full-screen route renders conversation history.
- [ ] Worker chat sends message, shows pending state, receives response, and handles budget-at-cap refusal.
- [ ] Worker chat persists through navigation and reload according to conversation rules.
- [ ] Worker chat auto-translation behavior is visible for non-default worker language.
- [ ] Issue form validates title, location, severity/category where present, description, and attachment.
- [ ] Issue submission creates the expected task/approval item and links back to detail.
- [ ] Asset scan handles camera permission denied, no camera, valid QR, invalid QR, deleted asset, and login-required state.
- [ ] Manual asset token entry works.
- [ ] Employee asset detail shows safe fields only, open tasks/actions, documents allowed to worker, and report issue flow.
- [ ] History shows completed tasks, skipped tasks, bookings, reimbursements, and empty state.
- [ ] History filters and pagination/infinite loading work without duplicating rows.

## Manager Dashboard And Approval Queues

Routes: `/dashboard`, `/approvals`, `/expenses`.

- [ ] Dashboard shows overdue work, staffing gaps, upcoming stays, approvals, budget usage, and operational summary.
- [ ] Dashboard quick actions navigate or mutate; no visible action is a no-op.
- [ ] Approval queue filters by pending, approved, rejected, expired, type, requester, and date.
- [ ] Approval detail expands enough context for safe approval.
- [ ] Approve/reject action requires rationale where specified and updates via optimistic UI/SSE.
- [ ] Expired approval cannot be approved.
- [ ] Agent-generated approval cards render source, proposed action, diff/summary, risk markers, and audit link.
- [ ] Expense approval queue shows scan confidence, receipt preview, claimant, engagement, amount, currency, and reimbursement state.
- [ ] Manager can approve, reject with reason, request clarification, and mark reimbursed where allowed.
- [ ] Expense approval handles stale exchange rate/manual rate blocking.

## Manager Properties, Units, Areas, Closures, And Sharing

Routes: `/properties`, `/property/:pid`, `/property/:pid/closures`.

- [ ] Property list renders cards/table, filters, search, empty, loading, and error states.
- [ ] Create property validates name, timezone, kind, address, client org, and defaults.
- [ ] Edit property updates visible detail and settings cascade.
- [ ] Property detail overview shows units/areas/stays/instructions/assets/settings/sharing according to property kind.
- [ ] Single-unit property hides or simplifies unit UI.
- [ ] Multi-unit property supports unit list, add, edit, archive, and welcome overrides.
- [ ] Areas are auto-seeded and can be edited where allowed.
- [ ] Sharing tab invites another workspace/client, shows pending/active/revoked shares, and revokes access.
- [ ] Client assignment tab links/unlinks client organization safely.
- [ ] Settings override panel saves, resets to inherited, and shows effective value source.
- [ ] Closure list shows date ranges, reason, effect on schedule, and affected tasks/bookings.
- [ ] Closure create rejects invalid ranges and overlapping conflicts where specified.
- [ ] Closure delete/cancel requires confirmation and updates schedules via SSE.
- [ ] Property detail tabs/actions are either functional or explicitly disabled. Known follow-up: `cd-54y3w`.

## Manager People, Roles, Permissions, And Leave

Routes: `/employees`, `/employee/:eid`, `/employee/:eid/leaves`, `/user/:eid/leaves`, `/leaves`, `/permissions`.

- [ ] Employees list shows active/archived workers, roles, properties, engagement kind, and search/filter states.
- [ ] Invite employee creates pending invite and copies/sends invite link.
- [ ] Employee detail tabs show profile, roles, property assignments, schedule/availability, documents, pay, audit, and settings.
- [ ] Employee archive/reinstate follows confirmation and hides/shows access correctly.
- [ ] Manager passkey reset/recovery support follows spec and audit behavior.
- [ ] Work role create/edit/archive works and respects starter roles.
- [ ] Property work role assignment affects scheduling and task assignment visibility.
- [ ] Leave inbox shows pending requests with worker, dates, conflicts, and coverage context.
- [ ] Manager approves/rejects leave and worker schedule updates via SSE.
- [ ] Manager cross-user leave edit uses manager-scoped endpoints and cannot enumerate via `/me` paths.
- [ ] Permissions page shows groups, rules, privacy tab, and who-can-do-this explainer.
- [ ] Permission rule create/edit/delete previews affected users and blocks root-only governance changes.
- [ ] Permission changes immediately affect nav/route access after refresh or SSE invalidation.

## Manager Tasks, Templates, Instructions, And Knowledge Base

Routes: `/templates`, `/instructions`, `/instructions/:iid`, planned `/kb`.

- [ ] Task template list supports search, filter, create, edit, duplicate, archive, and empty states.
- [ ] Checklist template editor supports required/optional items, ordering, and validation.
- [ ] Evidence policy source is visible and inherited values can be overridden/reset.
- [ ] Template schedule linking works with generated task preview.
- [ ] Instructions list supports scope filters, version status, search, and archive.
- [ ] Instruction markdown editor saves draft, previews rendered markdown, publishes a new version, and preserves prior versions.
- [ ] Instruction scope/link picker supports global, property, area, task template, asset type, and asset where specified.
- [ ] Worker task detail resolves and displays the correct instruction set with badges.
- [ ] Bulk archive/rescope operations require confirmation and show partial failure state.
- [ ] KB search mixes instructions and documents where specified, with read-only worker access.

## Manager Inventory

Route: `/inventory`.

- [ ] Item table renders localized decimal quantities and units.
- [ ] Inventory filters by property, area, category, low stock, vendor, and archived/deleted.
- [ ] Create/edit item validates SKU/barcode uniqueness, reorder threshold, unit, property, vendor, and initial quantity.
- [ ] Delete/archive/restore follows confirmation and preserves movement history.
- [ ] Row drawer shows movement ledger, reason taxonomy, actor, task/stay link, and running balance.
- [ ] Adjustment flow requires reason and note where specified.
- [ ] Restock flow creates movement and optional task/vendor link.
- [ ] Transfer between properties handles source/destination quantities and pending/complete state if implemented.
- [ ] Stocktake starts, walks items, records counted quantities, shows variance, commits, abandons, and handles concurrent item changes.
- [ ] Barcode scan finds item, handles unknown barcode, and offers create/link flow.
- [ ] Low-stock report, burn-rate report, production report, shrinkage report, and vendor report render and export if specified.
- [ ] Task completion consuming inventory updates visible stock via SSE.

## Manager Assets And Documents

Routes: `/assets`, `/asset/:aid`, `/asset_types`, `/documents`.

- [ ] Asset type catalog lists system and workspace-custom types.
- [ ] Asset type create/edit/archive validates required fields and seeded immutability.
- [ ] Asset list filters by property, type, status, condition, warranty/expiry, and search.
- [ ] Asset create/edit validates identity, type, property/area, purchase metadata, status, condition, and guest visibility.
- [ ] Asset detail shows QR, actions, documents, TCO, replacement forecast, audit, and related tasks.
- [ ] Print/download QR works without leaking deleted/revoked tokens.
- [ ] Recurring asset action activation creates schedule and future tasks.
- [ ] One-off perform action records completion and updates `last_performed_at`.
- [ ] Asset QR valid/deleted/revoked/not-found flows route correctly for logged-in and anonymous users.
- [ ] Document list filters by asset, property, type, expiry, extraction status, and search.
- [ ] Document upload validates file type/size and shows progress.
- [ ] Document extraction pending/done/failed/retry states render.
- [ ] Extracted text/details disclosure is visible only to authorized users.
- [ ] Expiring warranty/manual/invoice alerts surface where specified.
- [ ] Guest welcome page shows only guest-visible assets/documents.

## Manager Stays, Guests, And iCal

Routes: `/stays`, guest routes.

- [ ] Stays list/calendar renders by property/unit, date range, source, and status.
- [ ] Manual stay create/edit validates unit, check-in, check-out, guest name visibility, and overlap.
- [ ] iCal feed add validates URL, provider, unit mapping, SSRF-blocked targets, and duplicate feeds.
- [ ] Probe/test feed shows success, unsupported provider, unreachable, auth, malformed, and SSRF errors.
- [ ] Poll-once updates stays and conflict markers.
- [ ] Removed/cancelled remote stay follows lifecycle rules and does not orphan visible task bundles.
- [ ] Stay task bundle preview/generation shows before-checkin, during-stay, after-checkout tasks.
- [ ] Pull-back scheduling creates visible warnings when ideal date is unavailable.
- [ ] Guest welcome link create/copy/revoke works.
- [ ] Guest welcome page shows property/unit welcome merge, check-out checklist, visible assets, safe instructions, and privacy-safe guest data.

## Manager Payroll, Bookings, And Exports

Route: `/pay`.

- [ ] Pay page summarizes period, workers, bookings, amendments, expenses, reimbursements, and totals.
- [ ] Period close flow blocks when required approvals or stale rates remain.
- [ ] Payslip preview/PDF contains only safe payout destination display stubs.
- [ ] CSV export works and matches visible filters.
- [ ] Salaried, hourly, contractor, and agency-supplied engagement kinds display correctly.
- [ ] Booking amendments approved after period close follow the specified correction behavior.
- [ ] Worker-visible pay/reimbursement view does not expose other workers.

## Manager Organizations, Clients, Vendors, Work Orders, Quotes, And Invoices

Routes: `/organizations`, client routes, future work-order surfaces.

- [ ] Organization list supports client, supplier, both, archived, search, and filters.
- [ ] Organization create/edit validates legal name, display name, contact, payout destination display stub, and tax fields where present.
- [ ] Property client linking affects portfolio/client portal visibility.
- [ ] Work engagement kind is visible in employee/client/property contexts.
- [ ] Client rates and user rates render effective rates and priority/override source.
- [ ] Work order create/edit/state transitions work where implemented.
- [ ] Quote draft, submit for approval, approve, send to client, accept, decline, and expire states work.
- [ ] Vendor invoice upload/OCR/autofill/approval/reject/payment states work where implemented.
- [ ] Approval-gated money actions show agent/source context and audit.
- [ ] Client portal cannot mutate manager-only state.

## Manager Settings, Integrations, Audit, Webhooks, And API Tokens

Routes: `/settings`, `/webhooks`, `/tokens`, `/audit`, `/chat/channels`.

- [ ] Settings page lists workspace settings with inherited/effective source.
- [ ] Settings save/reset handles validation, conflict, permission denied, and SSE refresh.
- [ ] Agent usage widget shows percentage-only workspace budget state and at-cap behavior.
- [ ] Workspace API token create validates name/scopes, reveals plaintext once, copies, masks later, and records audit.
- [ ] Token rotate/revoke works and disables old token.
- [ ] Token request log filters by token, actor, scope, status, and date.
- [ ] Webhook list shows health, last delivery, enabled/disabled, and subscribed events.
- [ ] Webhook create/edit validates URL, events, name, and secret handling.
- [ ] Webhook test sends event and shows success/failure detail.
- [ ] Webhook secret rotate reveals/copies the new secret once.
- [ ] Webhook delivery drawer shows headers, attempts, response status/body excerpt, and retry where allowed.
- [ ] Audit log filters by actor, action, entity, date, and workspace scope.
- [ ] Audit log detail redacts PII and shows before/after safely.
- [ ] Chat channels page links/verifies/unlinks web/off-app bindings according to v1/deferred status.
- [ ] Provider display stubs render instead of full tokens/secrets.

## Admin Surface

Routes: `/admin`, `/admin/dashboard`, `/admin/chat-gateway`, `/admin/llm`, `/admin/agent-docs`, `/admin/usage`, `/admin/workspaces`, `/admin/signups`, `/admin/settings`, `/admin/admins`, `/admin/audit`.

- [ ] `/admin` redirects to `/admin/dashboard` for deployment admin.
- [ ] Admin dashboard shows deployment health, recent audit, usage, and operational warnings.
- [ ] LLM providers list/create/edit/test handles missing key, invalid key, disabled provider, and pricing sync.
- [ ] LLM model catalog and provider-model pricing show capability inheritance and override source.
- [ ] Capability assignment chain reorder/add/remove works and validates cycles/empty capability.
- [ ] Prompt library drawer lists prompts, versions/hashes, and safe preview.
- [ ] Recent calls feed filters by capability, workspace, provider, model, status, and date.
- [ ] Agent docs CRUD/search/preview works and respects deployment scope.
- [ ] Admin chat gateway provider table shows display stubs, webhook URL, verify token copy, test state, and override state.
- [ ] Usage page shows per-workspace spend/cap/pause, cap adjust, pause/unpause, and at-cap state.
- [ ] Workspaces page lists, filters, trusts/untrusts, archives/restores, and handles optimistic rollback.
- [ ] Signups page shows burst/repeat/IP/pre-verification quota signals and operator actions where specified.
- [ ] Admin settings toggles self-serve signup policy and deployment key/value settings with validation.
- [ ] Admin admins page manages deployment admin membership and permission rules.
- [ ] Admin audit page filters deployment-scope events and redacts secrets.

## Client Portal

Routes: `/portfolio`, `/billable_hours`, `/quotes`, `/invoices`.

- [ ] Client portfolio shows only properties linked to the client organization.
- [ ] Client portfolio property detail/read-only rota hides worker private fields.
- [ ] Billable hours filters by property, period, work order, worker role where allowed, and export state.
- [ ] Quote list and detail show draft/sent/accepted/declined/expired states.
- [ ] Client can accept quote with required confirmation.
- [ ] Client can decline quote with optional reason where specified.
- [ ] Invoice list and detail show open/paid/overdue/disputed states.
- [ ] Client proof upload validates file type/size and shows progress/failure/success.
- [ ] Client cannot reach manager/admin/worker routes by direct URL.

## Agent, LLM, Chat Gateway, Notifications, And Approvals

- [ ] Agent sidebar appears on desktop manager/worker shells where specified.
- [ ] Worker mobile `/chat` is the first-class chat entry.
- [ ] Manager bottom-dock/drawer behavior works on mobile if implemented.
- [ ] Admin agent surface is contextual to admin pages.
- [ ] Agent conversation persists across route navigation.
- [ ] Conversation compaction/older history loading is visible where specified.
- [ ] Agent action card shows proposed action, risk, required approval, and affected records.
- [ ] Approval-gated agent action cannot execute before approval.
- [ ] Approval reject returns a useful message to the agent/user.
- [ ] Approval expiry disables action.
- [ ] Per-user approval mode affects whether actions auto-run, ask, or refuse.
- [ ] Agent at-cap budget state refuses with visible copy and does not mutate data.
- [ ] Agent paused workspace state refuses with visible copy.
- [ ] Worker-side chat auto-translation shows translated/default/original toggles according to spec.
- [ ] Notification inbox or visible notification surfaces update via SSE.
- [ ] Email opt-out/profile controls affect visible notification settings.
- [ ] Web push registration/unregistration works where enabled and degrades where push is unavailable.

## SSE, Offline, PWA, And Realtime Coherence

- [ ] SSE connects only after authentication/workspace context is available.
- [ ] SSE disconnects on logout and workspace switch.
- [ ] SSE reconnect/backoff state does not spam requests.
- [ ] Last-event-id resumes without duplicating visible events.
- [ ] Query invalidations update task, schedule, expense, approval, inventory, and chat pages.
- [ ] Typing state clears on disconnect and timeout.
- [ ] Offline banner appears when browser is offline.
- [ ] Worker today/schedule/task detail remain usable from cached data.
- [ ] Completion queue persists across reload.
- [ ] Five offline completions replay in order and reconcile conflicts.
- [ ] Failed replay leaves actionable retry/error state.
- [ ] App manifest, icons, service worker/update prompt, installability, and standalone display mode work where PWA is enabled.

## Internationalization, Locale, Currency, And Time

- [ ] UI locale resolution follows user, workspace, browser, then default chain.
- [ ] Pseudolocale expands strings without clipping key surfaces.
- [ ] Dates/times display in property-local time with UTC-rest correctness.
- [ ] Cross-timezone user/property schedule views show expected day boundaries.
- [ ] Currency values format according to locale and currency.
- [ ] Multi-currency expense states show conversion/manual-rate/stale-rate behavior.
- [ ] Formal documents use document locale where visible in browser/PDF preview.
- [ ] Right-to-left content in user-entered fields does not break layout, even though RTL UI is out of v1 scope.

## Demo Mode

Routes and entrypoints from app demo spec and site embed contract.

- [ ] First demo visit mints a demo workspace and redirects to the scenario landing route.
- [ ] Returning demo visitor resumes the same workspace until expiry.
- [ ] Scenario switch creates or selects the correct seeded scenario.
- [ ] Persona switch changes visible role and permissions without leaking prior persona-only routes.
- [ ] `start=` is validated against per-scenario allowlist and never accepts `/w/`-prefixed paths.
- [ ] Invalid/unknown `start=` falls back to scenario default with no UI break.
- [ ] Demo disabled integrations return stub success or specified unavailable state.
- [ ] Demo budget cap state appears in agent UI.
- [ ] Demo abuse caps/GC expiry show clear session-expired or restart state.
- [ ] Iframe embedding works only from allowed site origin and retains passkey/cookie compatibility specified for demo.
- [ ] Demo data is visibly synthetic and does not expose personal seed data.

## Public Site And Suggestion Box

The `site/` implementation is absent in this worktree; these checks are derived from `docs/specs-site/`.

- [ ] `/` landing renders hero, feature bands, scenario picker/demo cell, and footer.
- [ ] Primary CTA scrolls to `#try-it`.
- [ ] Signup CTA links to the app signup URL.
- [ ] `/why-crewday`, `/for-owners`, `/for-agencies`, `/for-housekeepers`, `/pricing`, `/changelog`, `/legal/terms`, `/legal/privacy`, and `/404` render.
- [ ] `/for-*` pages preselect and hide the persona axis as specified.
- [ ] Scenario picker defaults to villa-owner and first intent.
- [ ] Changing persona resets intent.
- [ ] Picker state updates hash and reloads from hash.
- [ ] Demo cell uses video by default and fixed aspect ratio prevents CLS.
- [ ] `prefers-reduced-motion` suppresses autoplay and swap animation.
- [ ] Try-it-live swaps video for iframe only after user click.
- [ ] Iframe sandbox attributes and CSP allow only demo origin.
- [ ] Site pages set no app session cookies and serve no `/w/<slug>` routes.
- [ ] Site Lighthouse/accessibility/performance budgets pass for routes named in spec.
- [ ] `/suggest` public board renders without auth.
- [ ] `/suggest` unauthenticated form panel shows login CTA only.
- [ ] App `GET /feedback-redirect` lands on `/suggest?t=<token>`.
- [ ] Site consumes token once, sets `__Host-suggest_session`, and strips `t=` via redirect.
- [ ] Expired/tampered/replayed token shows clear invalid-token state.
- [ ] Authenticated suggestion form validates body length, category chip, and optional notify email.
- [ ] Submit success lands on `/suggest/thanks`.
- [ ] Duplicate/rate-limit/banned states render without exposing user/workspace identity.
- [ ] Suggestion board search/filter/sort/pagination render visible clusters.
- [ ] Cluster detail shows summary, count, recent reformulated titles, lifecycle, response, and workspace-count line when authenticated.
- [ ] Vote widget requires auth and enforces one vote per user hash.
- [ ] Hidden/rejected submissions never render verbatim body publicly.

## Known Follow-Up Beads

- `cd-noi8g`: Resolve expense pending reimbursement route naming drift.
- `cd-sfiod`: Clarify site demo chat intent copy against shipped chat model.
- `cd-54y3w`: Make property detail visible actions real or explicitly disabled.
- `cd-da4fo`: Audit manager pages for visible no-op overflow actions.

## Completion Tracking Convention

When converting a checklist item into an automated browser test:

- [ ] Add the test file path beside the item.
- [ ] Add the seed fixture or setup command used by the test.
- [ ] Add the last successful command and date only after running it.
- [ ] If the item is intentionally not automatable, mark it as manual and name the evidence expected.
- [ ] If the item is out of scope for v1, link the governing spec section or Beads task.
