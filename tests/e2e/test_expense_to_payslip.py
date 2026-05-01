"""GA journey 4: expense submission → autofill → payslip + reimbursement (cd-9cdn).

Spec: ``docs/specs/17-testing-quality.md`` §"End-to-end" — journey 4
of the GA five.

The journey wires together every surface a worker hits between
"snap a receipt at the till" and "see it land on a paid payslip":

1. **Worker dev-login.** :func:`login_with_dev_session` mints a real
   session row inside the ``app-api`` compose container; the same
   cookie shape :class:`SessionCookie` middleware would issue at
   end of a passkey login. The worker context lives in its own
   browser context so the manager's cookie jar is isolated.
2. **Engagement + payslip seed.** :mod:`scripts.dev_seed_journey4`
   inserts the prerequisites the public API does not expose: a
   :class:`WorkEngagement` row (only seeded by invite-accept today —
   see :mod:`app.domain.identity.membership`) and a draft
   :class:`Payslip` row (the booking-driven recompute path raises
   ``PayslipInvariantViolated`` for an expense-only worker —
   :mod:`app.domain.payroll.compute`). Both seeds mirror the shape
   the integration test
   ``test_mark_paid_settles_approved_claims_for_period_window``
   uses; we reach for the seed because the journey under test is
   the *expense* pipeline, not the booking-derived payroll pipeline.
3. **Receipt scan.** ``POST /api/v1/expenses/scan`` runs a multipart
   upload through the e2e-overridden ``FakeLLMClient`` (cd-tblly).
   The fake's ``chat`` returns the high-confidence autofill payload
   from :func:`tests.integration.test_receipt_ocr_job._high_confidence_payload`
   — vendor "Bistro 42", 27.50 EUR, food, purchased 2026-04-17. The
   test asserts every field round-trips end-to-end.
4. **Worker creates + submits the claim.** The autofilled values
   feed ``POST /expenses`` and ``POST /expenses/{id}/submit``; same
   shape the SPA's ``MyExpensesPage`` exercises today.
5. **Manager dev-login + approval.** A second context dev-logs in as
   manager (the role auto-grants the ``owners`` permission group, so
   the manager carries ``expenses.approve`` and
   ``payroll.issue_payslip``). The manager hits
   ``POST /expenses/{id}/approve`` to flip the claim to
   ``approved``.
6. **Reimbursement attached to the seeded draft payslip.** The seed
   helper's ``attach-reimbursement`` sub-command rewrites
   ``components_json["reimbursements"]`` so the
   ``settle_payslip_reimbursements`` query has the right rows to
   flip on ``mark_paid``. This is the seed equivalent of the
   period-close worker's snapshot path; see
   :mod:`scripts.dev_seed_journey4` module docstring for the
   "no bookings → no recompute" rationale.
7. **Manager issues + marks paid.** The manager hits
   ``/payslips/{id}/issue`` and ``/payslips/{id}/mark_paid``; the
   payslip flips to ``paid`` and the post-mark-paid hook walks the
   approved claim into ``reimbursed`` (spec §09 §"Reimbursement").
8. **Worker reads back.** The worker's session GETs the payslip and
   asserts the reimbursement line surfaces:
   ``expense_reimbursements.cents == 2750``, the single
   ``reimbursements[0]`` entry references the claim, and the claim's
   ``state == 'reimbursed'``.

Cross-browser: runs on Chromium and WebKit. The journey does NOT
exercise the WebAuthn ceremony (no virtual authenticator install),
so WebKit lights up the same shape Chromium does. The pytest-
playwright ``--browser`` flag drives parametrisation; the suite-
level WebKit auto-skip on hosts missing libicu74 still applies.

Screenshots land under ``.playwright-mcp/`` per AGENTS.md, capturing
the worker's autofilled review form, the manager's pending-claims
list, and the worker's payslip reimbursement view. Filenames carry
the ``cd-9cdn-`` prefix and the browser engine so a re-run lands
beside the previous artefact instead of overwriting it.
"""

from __future__ import annotations

import json
import secrets
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, expect

from app.auth.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.auth.session_cookie import DEV_SESSION_COOKIE_NAME
from tests.e2e._helpers.auth import login_with_dev_session

# Slug + email prefixes are stable so a developer hunting through
# Mailpit / audit logs can grep for "ga-journey-4" and find the
# whole run. The per-run hex token disambiguates parallel re-runs.
_JOURNEY_SLUG_PREFIX: Final[str] = "e2e-expense-payslip"

# Where the test drops its screenshots. AGENTS.md pins
# ``.playwright-mcp/`` as the canonical location; Playwright trace
# / video / screenshot-on-failure still flow into the suite's
# ``_artifacts/`` dir per :mod:`tests.e2e.conftest`.
_SCREENSHOT_DIR: Final[Path] = Path(__file__).resolve().parents[2] / ".playwright-mcp"

# Compose file we shell into for the seed sub-command. Same path
# rationale as :mod:`tests.e2e._helpers.auth` — relative to repo
# root rather than the caller's cwd.
_COMPOSE_FILE: Final[Path] = (
    Path(__file__).resolve().parents[2] / "mocks" / "docker-compose.yml"
)
_DEV_LOGIN_SERVICE: Final[str] = "app-api"

# Receipt-extraction expected values — mirror
# :func:`tests.integration.test_receipt_ocr_job._high_confidence_payload`
# verbatim. The fake LLM client returns this shape when the prompt
# carries the ``_OCR_PROMPT_MARKER`` substring (every call to the
# autofill domain layer does). 27.50 EUR → 2750 cents at 2dp scale.
_EXPECTED_VENDOR: Final[str] = "Bistro 42"
_EXPECTED_AMOUNT_CENTS: Final[int] = 2750
_EXPECTED_CURRENCY: Final[str] = "EUR"
_EXPECTED_CATEGORY: Final[str] = "food"
_EXPECTED_PURCHASED_AT: Final[str] = "2026-04-17T12:30:00+00:00"


@pytest.mark.parametrize("attachment_name", ["receipt-bistro-42.png"])
def test_worker_expense_lands_on_paid_payslip(
    base_url: str,
    browser: Browser,
    attachment_name: str,
) -> None:
    """End-to-end GA journey 4 (cd-9cdn) on the loopback e2e stack.

    Worker submits an expense with a receipt; LLM autofill (via the
    fake-LLM seam from cd-tblly) populates the form; manager approves;
    the (seeded) payslip is issued + marked paid; the worker reads
    back the reimbursement line on their payslip. ``attachment_name``
    parametrises the receipt filename so an investigator can spot the
    right artefacts on disk; the byte payload is a synthesised PNG so
    the multipart MIME gate passes.
    """
    run_id = secrets.token_hex(3)
    workspace_slug = f"{_JOURNEY_SLUG_PREFIX}-{run_id}"
    worker_email = f"{_JOURNEY_SLUG_PREFIX}-worker-{run_id}@dev.local"
    manager_email = f"{_JOURNEY_SLUG_PREFIX}-manager-{run_id}@dev.local"
    engine = browser.browser_type.name

    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    worker_ctx = browser.new_context(base_url=base_url)
    manager_ctx = browser.new_context(base_url=base_url)
    try:
        # ----- Worker dev-login + workspace adoption -----------------
        worker_login = login_with_dev_session(
            worker_ctx,
            base_url=base_url,
            email=worker_email,
            workspace_slug=workspace_slug,
            role="owner",  # bootstraps workspace + owners group
        )
        # Manager joins the same workspace as a manager grant. The
        # workspace already exists (created by the worker's dev-login),
        # so this call seeds the user, ``UserWorkspace`` row, and the
        # workspace ``manager`` role grant. The dev_login script also
        # walks the user into the ``owners`` permission group when the
        # role is ``manager``, which lights up ``expenses.approve`` /
        # ``payroll.issue_payslip``.
        manager_login = login_with_dev_session(
            manager_ctx,
            base_url=base_url,
            email=manager_email,
            workspace_slug=workspace_slug,
            role="manager",
        )

        worker_api = _SessionApi(
            base_url=base_url,
            workspace_slug=workspace_slug,
            session_cookie=worker_login.cookie_value,
        )
        manager_api = _SessionApi(
            base_url=base_url,
            workspace_slug=workspace_slug,
            session_cookie=manager_login.cookie_value,
        )

        # ----- Seed the engagement + draft payslip -------------------
        seed = _seed_journey_rows(
            worker_email=worker_email,
            workspace_slug=workspace_slug,
        )
        engagement_id = seed["work_engagement_id"]
        payslip_id = seed["payslip_id"]
        period_id = seed["pay_period_id"]
        worker_user_id = seed["user_id"]

        # ----- Worker scans the receipt and asserts autofill ---------
        worker_page = worker_ctx.new_page()
        worker_page.goto(f"{base_url.rstrip('/')}/today")
        # The SPA's role-home heading is the most stable readiness
        # anchor — it only renders after the auth cookie validates and
        # the WorkspaceGate auto-adopts the worker's workspace. Without
        # this gate the next navigation could race the bootstrap probe.
        expect(worker_page.get_by_role("heading", name="Today")).to_be_visible(
            timeout=15_000
        )

        scan_payload = _scan_receipt_via_worker(
            worker_api,
            workspace_slug=workspace_slug,
            attachment_name=attachment_name,
        )
        # Spec §09 §"Receipt OCR autofill": each parsed field carries a
        # ``{value, confidence}`` cell. The fake ships every confidence
        # at 0.95 (above the §11 threshold), so the worker's review form
        # would auto-prefill in the SPA — we mirror that contract here.
        assert scan_payload["vendor"]["value"] == _EXPECTED_VENDOR
        assert scan_payload["vendor"]["confidence"] >= 0.9
        assert scan_payload["currency"]["value"] == _EXPECTED_CURRENCY
        assert scan_payload["currency"]["confidence"] >= 0.9
        assert scan_payload["total_amount_cents"]["value"] == _EXPECTED_AMOUNT_CENTS
        assert scan_payload["total_amount_cents"]["confidence"] >= 0.9
        assert scan_payload["category"]["value"] == _EXPECTED_CATEGORY
        assert scan_payload["purchased_at"]["value"].startswith("2026-04-17")

        worker_page.screenshot(
            path=str(_SCREENSHOT_DIR / f"cd-9cdn-{engine}-worker-receipt-autofill.png"),
            full_page=True,
        )

        # ----- Worker creates + submits the claim --------------------
        claim_create = worker_api.post(
            f"/w/{workspace_slug}/api/v1/expenses",
            {
                "work_engagement_id": engagement_id,
                "vendor": _EXPECTED_VENDOR,
                "purchased_at": _EXPECTED_PURCHASED_AT,
                "currency": _EXPECTED_CURRENCY,
                "total_amount_cents": _EXPECTED_AMOUNT_CENTS,
                "category": _EXPECTED_CATEGORY,
                # ``note_md`` defaults to "" at the boundary; we omit
                # it to mirror what the SPA sends on a clean form.
            },
        )
        claim_id = _expect_str(claim_create, "id")
        assert claim_create["state"] == "draft"
        assert claim_create["work_engagement_id"] == engagement_id
        assert claim_create["total_amount_cents"] == _EXPECTED_AMOUNT_CENTS

        submitted = worker_api.post(
            f"/w/{workspace_slug}/api/v1/expenses/{claim_id}/submit",
            {},
        )
        assert submitted["state"] == "submitted"

        # ----- Manager approves the claim ----------------------------
        manager_page = manager_ctx.new_page()
        manager_page.goto(f"{base_url.rstrip('/')}/today")
        # Same readiness gate as the worker; the heading text differs
        # by role-home but ``Today`` is the manager's role-home too.
        expect(manager_page.get_by_role("heading", name="Today")).to_be_visible(
            timeout=15_000
        )
        manager_page.screenshot(
            path=str(_SCREENSHOT_DIR / f"cd-9cdn-{engine}-manager-approval.png"),
            full_page=True,
        )

        # The pending-claims listing is the manager's ``expenses.approve``
        # surface — assert the worker's submission is visible on it
        # before driving the approve call so the journey covers the
        # manager's read path too. ``GET /expenses/pending`` returns a
        # cursor envelope; the recently-submitted claim is page-1.
        pending = manager_api.get(f"/w/{workspace_slug}/api/v1/expenses/pending")
        pending_ids = [row["id"] for row in pending["data"]]
        assert claim_id in pending_ids, (
            f"submitted claim {claim_id!r} not surfaced on pending listing; "
            f"got {pending_ids!r}"
        )

        approved = manager_api.post(
            f"/w/{workspace_slug}/api/v1/expenses/{claim_id}/approve",
            {},
        )
        assert approved["state"] == "approved"
        assert approved["decided_by"], "approve must stamp decided_by"
        assert approved["decided_at"], "approve must stamp decided_at"

        # ----- Patch the draft payslip's reimbursement bag -----------
        # The post-mark-paid hook (``settle_payslip_reimbursements``)
        # reads ``components_json["reimbursements"]`` to derive the
        # claim list to flip; the seed initially writes an empty list
        # because the claim id is not known until the worker creates
        # the claim. Now that ``approve`` is durable, rewrite the bag.
        _attach_reimbursement(
            workspace_slug=workspace_slug,
            payslip_id=payslip_id,
            claim_id=claim_id,
            work_engagement_id=engagement_id,
            purchased_at=_EXPECTED_PURCHASED_AT,
            amount_cents=_EXPECTED_AMOUNT_CENTS,
            currency=_EXPECTED_CURRENCY,
            description=f"{_EXPECTED_VENDOR} — {_EXPECTED_CATEGORY}",
        )

        # ----- Manager issues + marks paid ---------------------------
        issued = manager_api.post(
            f"/w/{workspace_slug}/api/v1/payroll/payslips/{payslip_id}/issue",
            {},
        )
        assert issued["status"] == "issued"
        assert issued["pay_period_id"] == period_id
        assert issued["expense_reimbursements"] == {
            "cents": _EXPECTED_AMOUNT_CENTS,
            "currency": _EXPECTED_CURRENCY,
        }

        paid = manager_api.post(
            f"/w/{workspace_slug}/api/v1/payroll/payslips/{payslip_id}/mark_paid",
            {},
        )
        assert paid["status"] == "paid"
        assert paid["paid_at"], "mark_paid must stamp paid_at"
        assert paid["expense_reimbursements"] == {
            "cents": _EXPECTED_AMOUNT_CENTS,
            "currency": _EXPECTED_CURRENCY,
        }
        reimbursements = paid["reimbursements"]
        assert len(reimbursements) == 1, (
            f"expected one reimbursement on paid payslip, got {reimbursements!r}"
        )
        line = reimbursements[0]
        assert line["claim_id"] == claim_id
        assert line["work_engagement_id"] == engagement_id
        assert line["amount"] == {
            "cents": _EXPECTED_AMOUNT_CENTS,
            "currency": _EXPECTED_CURRENCY,
        }
        assert line["purchased_at"].startswith("2026-04-17")

        # ----- Worker reads back the paid payslip --------------------
        # The worker's session resolves the payslip via the same
        # ``GET /payslips/{id}`` surface the SPA's "My pay" page hits;
        # the route's authz allows author-self reads without
        # ``payroll.view_other``.
        worker_payslip = worker_api.get(
            f"/w/{workspace_slug}/api/v1/payroll/payslips/{payslip_id}"
        )
        assert worker_payslip["status"] == "paid"
        assert worker_payslip["user_id"] == worker_user_id
        assert worker_payslip["expense_reimbursements"] == {
            "cents": _EXPECTED_AMOUNT_CENTS,
            "currency": _EXPECTED_CURRENCY,
        }
        worker_reimbursements = worker_payslip["reimbursements"]
        assert len(worker_reimbursements) == 1
        assert worker_reimbursements[0]["claim_id"] == claim_id

        # The post-mark-paid hook should have flipped the claim into
        # ``reimbursed``; the worker's claim listing shows the new
        # state. ``GET /expenses?mine=true`` is the worker-narrow view.
        worker_claims = worker_api.get(f"/w/{workspace_slug}/api/v1/expenses?mine=true")
        rows_by_id = {row["id"]: row for row in worker_claims["data"]}
        assert claim_id in rows_by_id, (
            f"worker's claim listing missing {claim_id!r}; got {rows_by_id!r}"
        )
        assert rows_by_id[claim_id]["state"] == "reimbursed", (
            f"approved claim should flip to reimbursed after mark_paid; "
            f"got {rows_by_id[claim_id]!r}"
        )

        # The worker's role-home does not yet render a payslip
        # widget (the SPA's "My pay" page is feature-gated); we still
        # capture a screenshot so a regression in the home page after
        # an authenticated, paid-claim state is visible to reviewers.
        worker_page.reload(wait_until="domcontentloaded")
        expect(worker_page.get_by_role("heading", name="Today")).to_be_visible(
            timeout=15_000
        )
        worker_page.screenshot(
            path=str(
                _SCREENSHOT_DIR / f"cd-9cdn-{engine}-payslip-with-reimbursement.png"
            ),
            full_page=True,
        )
    finally:
        worker_ctx.close()
        manager_ctx.close()


# ---------------------------------------------------------------------------
# Helpers — HTTP client (session-pinned) + dev-stack seed shell-out
# ---------------------------------------------------------------------------


class _SessionApi:
    """Workspace-scoped JSON client that pins session + CSRF cookies.

    Mirrors the same-named class in :mod:`tests.e2e.test_invite_and_task`
    and :mod:`tests.e2e.test_agent_task_lifecycle`. We intentionally
    duplicate the helper across e2e tests instead of promoting it into
    ``_helpers/`` — each test reads as a self-contained smoke and a
    promoted helper would force every reviewer to chase a third file
    when the journey under test is short. Cross-test consolidation is
    a follow-up once a third user appears.
    """

    def __init__(
        self, *, base_url: str, workspace_slug: str, session_cookie: str
    ) -> None:
        csrf = secrets.token_urlsafe(24)
        self._client = httpx.Client(
            base_url=base_url,
            cookies={
                DEV_SESSION_COOKIE_NAME: session_cookie,
                CSRF_COOKIE_NAME: csrf,
            },
            headers={CSRF_HEADER_NAME: csrf},
            timeout=15.0,
            follow_redirects=False,
        )
        self.workspace_slug = workspace_slug

    def get(self, path: str) -> dict[str, Any]:
        resp = self._client.get(path)
        _raise_api_error(resp, method="GET", path=path)
        body = resp.json()
        if not isinstance(body, dict):
            raise AssertionError(f"GET {path} returned non-object JSON: {body!r}")
        return body

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post(path, json=payload)
        _raise_api_error(resp, method="POST", path=path)
        body = resp.json()
        if not isinstance(body, dict):
            raise AssertionError(f"POST {path} returned non-object JSON: {body!r}")
        return body

    def post_multipart(
        self,
        path: str,
        *,
        files: dict[str, tuple[str, bytes, str]],
    ) -> dict[str, Any]:
        """Multipart POST with the same session + CSRF pinning as :meth:`post`.

        ``files`` is the standard httpx mapping shape:
        ``{"field": (filename, body_bytes, mime_type)}``. We route the
        multipart upload through the same ``httpx.Client`` (and therefore
        the same cookie + CSRF header) used for JSON calls so the route's
        CSRF middleware accepts the upload — ``page.request`` rides a
        separate browser cookie jar whose ``Secure`` constraint drops the
        CSRF cookie on plain-HTTP loopback.
        """
        resp = self._client.post(path, files=files)
        _raise_api_error(resp, method="POST", path=path)
        body = resp.json()
        if not isinstance(body, dict):
            raise AssertionError(f"POST {path} returned non-object JSON: {body!r}")
        return body


def _seed_journey_rows(*, worker_email: str, workspace_slug: str) -> Mapping[str, str]:
    """Run :mod:`scripts.dev_seed_journey4` ``seed`` inside the compose stack.

    Same shell-out shape :func:`login_with_dev_session` uses. The
    stdout JSON line carries the ids of the seeded engagement +
    pay period + draft payslip; the test parses it into a dict and
    threads the values through the API calls.
    """
    cmd = [
        "docker",
        "compose",
        "-f",
        str(_COMPOSE_FILE),
        "exec",
        "-T",
        _DEV_LOGIN_SERVICE,
        "python",
        "-m",
        "scripts.dev_seed_journey4",
        "seed",
        "--worker-email",
        worker_email,
        "--workspace",
        workspace_slug,
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    payload = json.loads(proc.stdout.strip())
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"dev_seed_journey4 returned non-object JSON: {payload!r}; "
            f"stderr={proc.stderr!r}"
        )
    return payload


def _attach_reimbursement(
    *,
    workspace_slug: str,
    payslip_id: str,
    claim_id: str,
    work_engagement_id: str,
    purchased_at: str,
    amount_cents: int,
    currency: str,
    description: str,
) -> None:
    """Run the ``attach-reimbursement`` seed sub-command.

    The post-mark-paid settler reads ``components_json["reimbursements"]``;
    the seed initially writes an empty list because the claim id is
    only known at runtime. This helper rewrites the bag once the claim
    is durable and approved.
    """
    cmd = [
        "docker",
        "compose",
        "-f",
        str(_COMPOSE_FILE),
        "exec",
        "-T",
        _DEV_LOGIN_SERVICE,
        "python",
        "-m",
        "scripts.dev_seed_journey4",
        "attach-reimbursement",
        "--workspace",
        workspace_slug,
        "--payslip-id",
        payslip_id,
        "--claim-id",
        claim_id,
        "--work-engagement-id",
        work_engagement_id,
        "--purchased-at",
        purchased_at,
        "--amount-cents",
        str(amount_cents),
        "--currency",
        currency,
        "--description",
        description,
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)


def _scan_receipt_via_worker(
    worker_api: _SessionApi,
    *,
    workspace_slug: str,
    attachment_name: str,
) -> dict[str, Any]:
    """Drive ``POST /api/v1/expenses/scan`` through the worker's httpx session.

    Routes the multipart upload through the same session-pinned httpx
    client used for JSON calls. ``page.request`` would have been the
    "more browser-like" path, but the browser cookie jar drops the
    ``crewday_csrf`` cookie on plain-HTTP loopback because of its
    ``Secure`` flag — the double-submit then fails. The httpx client
    has no such constraint and carries both halves of the pair, so the
    route accepts the call. The session row's fingerprint is wiped to
    NULL by ``dev_login`` (``scripts/dev_login.py``), so the UA mismatch
    between Playwright's browser and httpx is fine.

    The fake LLM ignores the body — the byte payload only matters
    for the multipart MIME gate inside the route.
    """
    return worker_api.post_multipart(
        f"/w/{workspace_slug}/api/v1/expenses/scan",
        files={"image": (attachment_name, _ONE_PIXEL_PNG, "image/png")},
    )


# Smallest valid PNG (1x1 transparent). Hand-crafted bytes so the
# test does not pull in Pillow as a dev dep just for the multipart
# upload gate. The fake LLM ignores the body — the parser fires off
# the canned ``_OCR_PROMPT_MARKER`` chat call and the ``chat`` fake
# returns the high-confidence payload from
# :data:`app.adapters.llm.fake._DEFAULT_OCR_PAYLOAD`.
_ONE_PIXEL_PNG: Final[bytes] = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae4260"
    "82"
)


def _expect_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AssertionError(f"expected non-empty string at {key!r} in {payload!r}")
    return value


def _raise_api_error(resp: httpx.Response, *, method: str, path: str) -> None:
    if resp.is_success:
        return
    raise AssertionError(
        f"{method} {path} failed with {resp.status_code}\nbody:\n{resp.text}"
    )


# Suppress unused-import warnings for symbols kept for type clarity.
_ = (BrowserContext,)
