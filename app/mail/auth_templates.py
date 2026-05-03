"""Auth-flow email rendering — Jinja2 file-resident templates.

Auth templates live alongside the notification templates under
:mod:`app.domain.messaging.templates` (subdirectory ``auth/``); this
module is the thin rendering helper the auth and identity layers call.

On-disk convention (matches §10 "Email template system"):

* ``app/domain/messaging/templates/auth/<name>.subject.j2`` — subject
  line.
* ``app/domain/messaging/templates/auth/<name>.body_text.j2`` —
  plaintext body.

Locale-aware variants (``<name>.<locale>.<channel>.j2``) are not in
use today for auth flows but are supported by
:class:`~app.domain.messaging.notifications.Jinja2TemplateLoader`; if
a future revision ships localised auth copy, the same loader resolves
the fallback chain.

Autoescape is **disabled** here. The auth bodies are plain-text only
(magic-link URLs, masked email addresses) and HTML escaping would
mangle ``&`` characters in URLs and produce ``&lt;``/``&gt;`` in
plaintext bodies that the recipient would see verbatim. Notification
templates keep autoescape on for their HTML / Markdown variants — the
two surfaces have different rendering needs, so they get separate
:class:`~jinja2.Environment` instances.

Public surface:

* :func:`render_auth_email` — render ``(subject, body_text)`` for the
  named template with the supplied context.
* :func:`purpose_label` — magic-link purpose → human-readable phrase
  ("verify your email and finish signing up", "recover your account",
  ...). Kept as a Python map because the lookup is data, not template
  copy.

See ``docs/specs/10-messaging-notifications.md`` §"Email template
system" and ``docs/specs/03-auth-and-tokens.md``.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any, Final

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from jinja2 import TemplateNotFound as _JinjaTemplateNotFound

from app.domain.messaging.notifications import TEMPLATE_ROOT

__all__ = [
    "AUTH_TEMPLATE_ROOT",
    "AuthTemplateNotFound",
    "purpose_label",
    "render_auth_email",
]


# Absolute path to the auth template subdirectory. Exposed so tests
# can assert the on-disk layout without reimplementing the join.
AUTH_TEMPLATE_ROOT: Path = TEMPLATE_ROOT / "auth"


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
def _env() -> Environment:
    """Return a process-wide Jinja2 :class:`Environment` for auth templates.

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


def render_auth_email(
    name: str,
    /,
    **context: Any,
) -> tuple[str, str]:
    """Return ``(subject, body_text)`` for the auth template ``name``.

    Subject is right-stripped of trailing newline so the rendered value
    is one line (Jinja's ``keep_trailing_newline`` adds one to match
    the body's storage shape; the subject header has no use for it).

    Raises :class:`AuthTemplateNotFound` when either the subject or
    the body file is absent on disk.
    """
    env = _env()
    try:
        subject_template = env.get_template(f"{name}.subject.j2")
    except _JinjaTemplateNotFound as exc:
        raise AuthTemplateNotFound(name=name, channel="subject") from exc
    try:
        body_template = env.get_template(f"{name}.body_text.j2")
    except _JinjaTemplateNotFound as exc:
        raise AuthTemplateNotFound(name=name, channel="body_text") from exc

    subject = subject_template.render(**context).rstrip("\n")
    body_text = body_template.render(**context)
    return subject, body_text


# Magic-link purpose → human-readable phrase. Kept as a Python map
# (not a per-purpose template) because the value is data the subject
# AND body templates both interpolate via ``purpose_label`` — splitting
# into two files would duplicate the copy.
_PURPOSE_LABELS: Final[dict[str, str]] = {
    "signup_verify": "verify your email and finish signing up",
    "recover_passkey": "recover your account and enrol a new passkey",
    "email_change_confirm": "confirm your new email address",
    "email_change_revert": "revert the recent email change on your account",
    "grant_invite": "accept the invite to join a workspace",
    "workspace_verify_ownership": "verify ownership of your workspace",
}


def purpose_label(purpose: str) -> str:
    """Return the human-readable phrase for a magic-link ``purpose``.

    Unknown purposes fall back to a generic phrase rather than raising
    so a future purpose added without updating the label map still
    produces a sane email. Callers already validate ``purpose`` at the
    domain layer; a typo there lands elsewhere.
    """
    return _PURPOSE_LABELS.get(purpose, "complete your crew.day action")
