"""Unit tests for :mod:`app.api.v1.approvals` (cd-9ghv).

The tests in this package exercise the HITL approvals consumer router
in isolation — TestClient + in-memory SQLite + the full schema — so
the auth-gating credential matrix, the cross-tenant 404 envelope,
the dispatcher 503, and the audit/event side effects are covered
without a stack-up of integration plumbing.

End-to-end coverage (real DB, real worker) lives in
``tests/integration/worker/test_approval_ttl.py`` for the TTL sweep
and ``tests/domain/agent/test_approval.py`` for the service-layer
state machine. This package only verifies the HTTP seam.
"""
