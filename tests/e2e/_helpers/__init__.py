"""Shared helpers for the e2e Playwright suite (cd-ndmv).

Three modules:

* :mod:`tests.e2e._helpers.auth` — log in (dev_login fast-path) +
  enroll a passkey via the WebAuthn virtual authenticator (full
  ceremony, gated by RP-ID alignment — see the module docstring).
* :mod:`tests.e2e._helpers.visual` — pixelmatch-driven screenshot
  diff against committed baselines.
* :mod:`tests.e2e._helpers.sitemap` — 360 px viewport walk over the
  authenticated SPA route list.

Each module's contract lives in its own docstring; the package init
exists only to mark the directory as a Python package so pytest's
``importlib`` import mode can resolve ``tests.e2e._helpers.*``.
"""

from __future__ import annotations
