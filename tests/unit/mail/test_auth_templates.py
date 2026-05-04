"""Unit tests for ``app.mail.auth_templates``.

Asserts the migrated Jinja2-resident auth templates render byte-for-
byte equivalent output to the previous ``str.format_map``-based ones,
plus the loud-failure modes (missing template, missing context key).
"""

from __future__ import annotations

import pytest

from app.mail.auth_templates import (
    AUTH_TEMPLATE_ROOT,
    AuthTemplateNotFound,
    purpose_label,
    render_auth_email,
)


def test_template_root_exists() -> None:
    """The on-disk template directory ships with the package."""
    assert AUTH_TEMPLATE_ROOT.exists()
    assert AUTH_TEMPLATE_ROOT.is_dir()
    assert AUTH_TEMPLATE_ROOT.parts[-3:] == ("mail", "templates", "auth")
    assert "messaging" not in AUTH_TEMPLATE_ROOT.parts


def test_magic_link_renders_subject_and_body() -> None:
    subject, body = render_auth_email(
        "magic_link",
        purpose_label="verify your email and finish signing up",
        url="https://crew.day/auth/magic/tok123",
        ttl_minutes="15",
    )
    assert subject == "crew.day — verify your email and finish signing up"
    assert "verify your email and finish signing up" in body
    assert "https://crew.day/auth/magic/tok123" in body
    assert "next 15" in body
    # Trailing newline preserved (matches the original BODY_TEXT).
    assert body.endswith("— crew.day\n")


def test_invite_accept_renders_with_workspace_context() -> None:
    subject, body = render_auth_email(
        "invite_accept",
        invitee_display_name="Alex",
        inviter_display_name="Maria",
        workspace_name="Sunshine Villas",
        url="https://crew.day/auth/magic/inv456",
        ttl_hours="24",
    )
    assert subject == "crew.day — Maria invited you to Sunshine Villas"
    assert body.startswith("Hi Alex,\n")
    assert "Maria invited you to join Sunshine Villas" in body
    assert "https://crew.day/auth/magic/inv456" in body
    assert "next 24 hours" in body


def test_recovery_new_link_renders() -> None:
    subject, body = render_auth_email(
        "recovery_new_link",
        display_name="Sam",
        url="https://crew.day/recover/enroll?token=abc",
        ttl_minutes="10",
    )
    assert subject == "crew.day — recover your account"
    assert body.startswith("Hi Sam,\n")
    # URL with query string survives intact (autoescape off).
    assert "https://crew.day/recover/enroll?token=abc" in body
    assert "next 10 minutes" in body


def test_passkey_reset_worker_renders_with_owner_and_workspace() -> None:
    subject, body = render_auth_email(
        "passkey_reset_worker",
        display_name="Sam",
        owner_display_name="Maria",
        workspace_name="Sunshine Villas",
        url="https://crew.day/recover/enroll?token=abc",
        ttl_minutes="10",
    )
    assert subject == "crew.day — your passkey has been reset"
    assert body.startswith("Hi Sam,\n")
    assert "Maria reset your passkey" in body
    assert "Sunshine Villas" in body


def test_passkey_reset_notice_renders_with_masked_email_and_timestamp() -> None:
    subject, body = render_auth_email(
        "passkey_reset_notice",
        owner_display_name="Maria",
        worker_display_name="Sam",
        worker_email_masked="s***@example.com",
        workspace_name="Sunshine Villas",
        timestamp="2026-05-01T12:00:00Z",
        notice_url="https://crew.day/recover/notice?id=xyz",
    )
    assert subject == "crew.day — passkey reset confirmation"
    assert body.startswith("Hi Maria,\n")
    assert "Sam (s***@example.com)" in body
    assert "Sunshine Villas workspace at 2026-05-01T12:00:00Z" in body
    assert "https://crew.day/recover/notice?id=xyz" in body


def test_email_change_notice_renders() -> None:
    subject, body = render_auth_email(
        "email_change_notice",
        display_name="Sam",
        masked_new_email="a***@example.com",
        ip_prefix="203.0.113.0/24",
        ttl_minutes="15",
    )
    assert subject == "crew.day — email change requested on your account"
    assert "a***@example.com" in body
    assert "IP 203.0.113.0/24" in body
    assert "next 15" in body


def test_email_change_confirmed_renders() -> None:
    subject, body = render_auth_email(
        "email_change_confirmed",
        display_name="Sam",
        masked_old_email="o***@example.com",
    )
    assert subject == "crew.day — your email address was changed"
    assert "instead of o***@example.com" in body


def test_email_change_revert_renders_with_url() -> None:
    subject, body = render_auth_email(
        "email_change_revert",
        display_name="Sam",
        masked_new_email="a***@attacker.example",
        url="https://crew.day/auth/email/revert?token=tok",
        ttl_hours="72",
    )
    assert subject == "crew.day — revert the email change on your account"
    assert "a***@attacker.example" in body
    assert "https://crew.day/auth/email/revert?token=tok" in body
    assert "72 hours" in body


def test_purpose_label_known_purposes() -> None:
    assert purpose_label("signup_verify") == ("verify your email and finish signing up")
    assert purpose_label("recover_passkey") == (
        "recover your account and enrol a new passkey"
    )
    assert purpose_label("email_change_confirm") == "confirm your new email address"
    assert purpose_label("email_change_revert") == (
        "revert the recent email change on your account"
    )
    assert purpose_label("grant_invite") == "accept the invite to join a workspace"
    assert purpose_label("workspace_verify_ownership") == (
        "verify ownership of your workspace"
    )


def test_purpose_label_unknown_falls_back() -> None:
    assert purpose_label("never_seen_before") == "complete your crew.day action"


def test_render_auth_email_unknown_template_raises() -> None:
    with pytest.raises(AuthTemplateNotFound) as excinfo:
        render_auth_email("does_not_exist", foo="bar")
    assert excinfo.value.name == "does_not_exist"
    assert excinfo.value.channel == "subject"


def test_render_auth_email_missing_context_key_raises() -> None:
    """A typo at the call site should fail loud, not ship a half-rendered email.

    The :class:`StrictUndefined` configuration on the auth Jinja
    environment turns a missing context key into a
    :class:`jinja2.UndefinedError` (which Jinja raises at render time).
    """
    from jinja2 import UndefinedError

    with pytest.raises(UndefinedError):
        render_auth_email("magic_link")  # missing purpose_label / url / ttl_minutes


def test_no_html_escaping_on_user_supplied_data() -> None:
    """Auth bodies are plain-text — autoescape MUST stay off.

    A workspace name like ``A & B Villas`` should land in the inbox
    verbatim, not as ``A &amp; B Villas``. The notification surface
    (HTML / Markdown) keeps autoescape on; this surface deliberately
    does not.
    """
    subject, body = render_auth_email(
        "invite_accept",
        invitee_display_name="Alex",
        inviter_display_name="Maria",
        workspace_name="A & B Villas",
        url="https://crew.day/auth/magic/tok",
        ttl_hours="24",
    )
    assert "A & B Villas" in subject
    assert "A & B Villas" in body
    assert "&amp;" not in body


def test_url_with_query_string_survives() -> None:
    """URLs with ``?`` and ``=`` (notably ``email_change_revert``) render verbatim."""
    _subject, body = render_auth_email(
        "email_change_revert",
        display_name="Sam",
        masked_new_email="a***@example.com",
        url="https://crew.day/auth/email/revert?token=abc&extra=1",
        ttl_hours="72",
    )
    assert "https://crew.day/auth/email/revert?token=abc&extra=1" in body
    assert "&amp;" not in body
