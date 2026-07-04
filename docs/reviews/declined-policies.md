# Declined hygiene policies

Low-priority hygiene items that were reviewed and deliberately **not**
applied, with the reason. Keep entries one line each; link the Beads
task that raised them.

- **cd-o70sz item 5 — `app/domain/llm/consent.py` inline `Table`:** kept. The real seam (`app.domain.agent.preferences.read_workspace_upstream_pii_consent`) needs a full `WorkspaceContext` and routes through the ORM tenant filter, whereas `load_consent_set(session, workspace_id: str)` takes a bare workspace id and the inline core `Table` intentionally bypasses the filter on the LLM outbound hot path; wiring the seam means changing the signature across ~8 call sites and coupling them to an active tenant filter (risking `TenantFilterMissing` on worker/agnostic paths) — a behavior-changing refactor, not a P4 swap.
- **cd-o70sz item 7 — `Workspace.updated_at` `server_default=CURRENT_TIMESTAMP`:** kept. `CURRENT_TIMESTAMP` is portable (both SQLite and Postgres accept it as a DDL default) and is load-bearing: both production signup paths (`app/auth/signup.py:190`, `:1076`) and the test factories create `Workspace(...)` with only `created_at`, relying on the default for `updated_at`. Dropping it would NOT-NULL-violate every signup and dozens of fixtures; aligning to the sibling "no default + always write `updated_at`" convention is a non-tiny cross-cutting change out of P4 scope.
