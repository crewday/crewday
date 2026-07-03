"""Auth-flow email rendering — Jinja2 file-resident templates.

Auth templates live under :mod:`app.mail.templates` (subdirectory
``auth/``); this module is the thin rendering helper the auth and
identity layers call.

On-disk convention (matches §10 "Email template system"):

* ``app/mail/templates/auth/<name>.subject.j2`` — subject line.
* ``app/mail/templates/auth/<name>.body_text.j2`` —
  plaintext body.
* ``app/mail/templates/auth/<name>.body_html.j2`` —
  optional HTML alternative body.

Locale-aware variants (``<name>.<locale>.<channel>.j2``) are not in
use today for auth flows. If a future revision ships localised auth
copy, this renderer can grow the same fallback chain as notification
templates without importing the messaging domain.

Autoescape is **disabled** for the subject and plaintext body channels:
magic-link URLs, masked email addresses, and ``&`` characters must
render exactly as text recipients see them. Optional
``body_html.j2`` templates render through a separate environment with
autoescape on, so user-controlled names and masked addresses cannot
inject markup into the HTML alternative part.

Public surface:

* :func:`render_auth_email` — render ``(subject, body_text, body_html)``
  for the named template with the supplied context. ``body_html`` is
  ``None`` when the template has no HTML alternative.
* :func:`purpose_label` — magic-link purpose → human-readable phrase
  ("verify your email and finish signing up", "recover your account",
  ...), resolved through the gettext catalog at the requested locale.
  The map holds catalog keys, not English copy, so the phrases live in
  ``app/i18n/locales`` alongside every other backend-notification
  string. v1 ships English only; a missing locale falls back to
  ``en-US`` inside :func:`app.i18n.t`.

See ``docs/specs/10-messaging-notifications.md`` §"Email template
system" and ``docs/specs/03-auth-and-tokens.md``.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any, Final

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from jinja2 import TemplateNotFound as _JinjaTemplateNotFound

from app.i18n import t

__all__ = [
    "AUTH_TEMPLATE_ROOT",
    "AuthTemplateNotFound",
    "purpose_label",
    "render_auth_email",
]


# Absolute path to the auth template subdirectory. Exposed so tests
# can assert the on-disk layout without reimplementing the join.
AUTH_TEMPLATE_ROOT: Path = Path(__file__).resolve().parent / "templates/auth"


class AuthTemplateNotFound(LookupError):
    """A requested auth template (subject or body_text) is missing on disk.

    Loud failure — a typo at the call site or a renamed template should
    surface immediately, not silently send an empty subject. The error
    message names the file the loader expected so the operator can grep.
    """

    def __init__(self, *, name: str, channel: str) -> None:
        self.name = name
        self.channel = channel
        super().__init__(
            f"No auth template found for name={name!r} channel={channel!r}; "
            f"expected {AUTH_TEMPLATE_ROOT}/{name}.{channel}.j2 to exist."
        )


@cache
def _env_text() -> Environment:
    """Return a process-wide Jinja2 environment for text auth templates.

    Cached because the environment is stateless once configured — every
    render call shares the compiled-template cache. Tests that point
    at a different directory build their own environment; callers in
    production go through this default.

    ``StrictUndefined`` ensures a missing context key raises at render
    time instead of silently emitting an empty string — a typo in the
    caller's keyword arg should fail fast, not ship a broken email.

    ``autoescape=False`` because auth bodies are plain-text. See module
    docstring for the rationale.

    ``keep_trailing_newline=True`` preserves the final newline the
    on-disk templates carry, matching the original :class:`str` body
    constants byte-for-byte.
    """
    return Environment(
        loader=FileSystemLoader(str(AUTH_TEMPLATE_ROOT)),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


@cache
def _env_html() -> Environment:
    """Return a process-wide Jinja2 environment for HTML auth templates."""
    return Environment(
        loader=FileSystemLoader(str(AUTH_TEMPLATE_ROOT)),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def render_auth_email(
    name: str,
    /,
    **context: Any,
) -> tuple[str, str, str | None]:
    """Return ``(subject, body_text, body_html)`` for auth template ``name``.

    Subject is right-stripped of trailing newline so the rendered value
    is one line (Jinja's ``keep_trailing_newline`` adds one to match
    the body's storage shape; the subject header has no use for it).

    Raises :class:`AuthTemplateNotFound` when either the subject or
    plaintext body file is absent on disk. The HTML body is optional.
    """
    text_env = _env_text()
    try:
        subject_template = text_env.get_template(f"{name}.subject.j2")
    except _JinjaTemplateNotFound as exc:
        raise AuthTemplateNotFound(name=name, channel="subject") from exc
    try:
        body_template = text_env.get_template(f"{name}.body_text.j2")
    except _JinjaTemplateNotFound as exc:
        raise AuthTemplateNotFound(name=name, channel="body_text") from exc
    try:
        body_html_template = _env_html().get_template(f"{name}.body_html.j2")
    except _JinjaTemplateNotFound:
        body_html_template = None

    subject = subject_template.render(**context).rstrip("\n")
    body_text = body_template.render(**context)
    body_html = (
        None if body_html_template is None else body_html_template.render(**context)
    )
    return subject, body_text, body_html


# Magic-link purpose → gettext catalog key. The subject AND body
# templates both interpolate the resolved phrase via ``purpose_label``;
# the phrases themselves live in ``app/i18n/locales`` so they are
# translation-ready without a code change (v1 ships English only).
_PURPOSE_LABEL_KEYS: Final[dict[str, str]] = {
    "signup_verify": "auth.magic_link.purpose.signup_verify",
    "recover_passkey": "auth.magic_link.purpose.recover_passkey",
    "email_change_confirm": "auth.magic_link.purpose.email_change_confirm",
    "email_change_revert": "auth.magic_link.purpose.email_change_revert",
    "grant_invite": "auth.magic_link.purpose.grant_invite",
    "workspace_verify_ownership": "auth.magic_link.purpose.workspace_verify_ownership",
}

# Fallback key for an unknown purpose — a future purpose added without
# updating the map still resolves to a sane generic phrase.
_PURPOSE_LABEL_FALLBACK_KEY: Final[str] = "auth.magic_link.purpose.fallback"


def purpose_label(purpose: str, *, locale: str | None = None) -> str:
    """Return the human-readable phrase for a magic-link ``purpose``.

    Resolved through the gettext catalog at ``locale`` (the notification
    locale). ``locale=None`` resolves to ``en-US`` inside
    :func:`app.i18n.t`, and any locale without a translation falls back
    to the ``en-US`` catalog — so v1's English output is unchanged.

    Unknown purposes fall back to a generic phrase rather than raising
    so a future purpose added without updating the map still produces a
    sane email. Callers already validate ``purpose`` at the domain
    layer; a typo there lands elsewhere.
    """
    key = _PURPOSE_LABEL_KEYS.get(purpose, _PURPOSE_LABEL_FALLBACK_KEY)
    return t(key, locale=locale)
