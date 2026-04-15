# 20 — Glossary

Terms used across the spec. Definitive form; if code or doc disagrees,
fix the offender.

- **Agent.** A non-human actor authenticated by an API token.
- **Approvable action.** A write that requires manager approval
  regardless of token scope (§11).
- **Area.** A subdivision of a property (kitchen, pool, Room 3).
- **Assignment.** The linkage of an employee (via roles) to a property
  and, per task, of an employee to a specific occurrence.
- **Audit log.** Append-only ledger of all state-changing actions.
- **Capability.** A per-employee feature flag (§05).
- **Completion.** Terminal state for a task; has evidence and an
  employee.
- **Evidence.** Artifact attached to a completion — photo, note, or
  tick-list snapshot.
- **Household.** The single tenant in a v1 deployment.
- **Instruction.** A standing SOP attached at global / property /
  area / link scope (§07).
- **Issue.** An employee-reported problem tracked with state and
  possibly converted to a task.
- **Magic link.** Single-use, signed URL used to enroll or recover a
  passkey.
- **Manager.** Human with elevated scope.
- **Model assignment.** The capability → model mapping (§11).
- **Passkey.** WebAuthn platform or roaming authenticator credential.
- **Pay period.** A date-bucket inside which shifts roll up into a
  payslip.
- **Payslip.** A computed pay document for one (employee, pay_period).
- **Property.** A managed physical place.
- **Role.** A named capability bundle (maid, cook, …).
- **Schedule.** Description of when tasks materialize (RRULE).
- **Session.** Browser-bound server-side record tied to a passkey.
- **Shift.** A clocked-in interval for an employee.
- **SKU / item.** An inventory entry per property.
- **Stay.** A reservation of a property by a guest for a date range.
- **Template (task template).** Reusable task definition.
- **Token.** API token; `mip_<keyid>_<secret>` on the wire.
- **Turnover.** The set of tasks generated on stay check-out.
- **Unavailable marker.** An iCal block that is not a stay (e.g.
  Airbnb "Not available").
- **Welcome link.** Tokenized public URL exposing the guest welcome
  page for a stay.
