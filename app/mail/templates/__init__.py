"""mail.templates — one module per template kind.

Each template module exposes module-level string constants:

* ``SUBJECT`` — single-line header,
* ``BODY_TEXT`` — plain-text body,
* ``BODY_HTML`` (optional) — HTML alternative.

Each constant is a Python :class:`str` with ``{placeholder}`` slots the
caller substitutes via :func:`render`. Keep the templates minimal —
anything richer than placeholder substitution belongs in a Jinja
template once we land a real rendering engine (deferred; see
``app.mail`` package docstring).
"""

from __future__ import annotations

__all__ = ["render"]


def render(template: str, **values: str) -> str:
    """Return ``template`` with every ``{key}`` replaced by ``values[key]``.

    Uses :meth:`str.format_map` so an unknown placeholder raises
    :class:`KeyError` instead of silently emptying — a template
    string with a typo should fail loudly at send time, not ship a
    half-rendered email. Values are restricted to ``str`` because
    ``{url}`` being an object whose ``__str__`` prints the in-memory
    address would be a PII leak trap.
    """
    return template.format_map(values)
