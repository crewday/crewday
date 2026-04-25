"""Cross-cutting authorization helpers.

This package collects authz primitives that are consumed by multiple
domain contexts (identity, tenancy middleware, the permission
resolver). Putting them under ``app.authz`` rather than
``app.domain.identity`` keeps them reachable from the HTTP middleware
(where a ``WorkspaceContext`` is being *built*, not consumed) without
pulling the whole domain-identity module graph into the middleware
import chain.

Unlike ``app.domain`` modules, ``app.authz`` modules MAY import from
``app.adapters`` directly — the import-linter contract
(``app.domain → app.adapters``) does not apply here. These helpers
are thin DB shims; when the proper ``PermissionGroupRepository``
lands (cd-duv6) the body moves behind a Protocol seam.

The :class:`~app.authz.dep.Permission` FastAPI dependency factory is
deliberately **not** re-exported here. It lives in
:mod:`app.authz.dep` and is imported by routers via
``from app.authz.dep import Permission``. Keeping it off the public
``app.authz`` surface means domain modules importing
``from app.authz import require`` do not transitively pull in
:mod:`app.api.deps`, which would otherwise break the import-linter
"Domain forbids handlers (api/web/cli/worker)" contract.

Public surface (pure-domain — safe for ``app.domain`` callers):

* :func:`is_owner_member` / :func:`resolve_is_owner` — explicit
  owners-group membership lookup (cd-ckr).
* :func:`is_deployment_admin` — bare-host admin-surface gate
  (cd-wchi). Returns True iff the user holds any deployment-scoped
  ``role_grant``; consumed by the admin auth dep at cd-xgmu and by
  ``/auth/me`` to populate the ``is_deployment_admin`` flag.
* :func:`is_member_of` — dispatch on system-group slug, derived-vs-
  explicit (cd-dzp).
* :func:`require` — the canonical permission check (cd-dzp). Service
  callers use this directly; routers go through
  :func:`app.authz.dep.Permission` (which itself calls
  :func:`require`).
* :class:`PermissionRuleRepository` / :class:`EmptyPermissionRuleRepository`
  — v1 seam so the resolver is complete before the
  ``permission_rule`` table ships (cd-dzp).

See ``docs/specs/05-employees-and-roles.md`` §"Permissions: surface,
groups, and action catalog" and
``docs/specs/02-domain-model.md`` §"Permission resolution".
"""

from __future__ import annotations

from app.authz.deployment_admin import is_deployment_admin
from app.authz.enforce import (
    CatalogDrift,
    EmptyPermissionRuleRepository,
    InvalidScope,
    PermissionCheck,
    PermissionDenied,
    PermissionRuleRepository,
    RuleEffect,
    RuleRow,
    UnknownActionKey,
    require,
    validate_catalog_integrity,
)
from app.authz.membership import UnknownSystemGroup, is_member_of
from app.authz.owners import is_owner_member, resolve_is_owner

__all__ = [
    "CatalogDrift",
    "EmptyPermissionRuleRepository",
    "InvalidScope",
    "PermissionCheck",
    "PermissionDenied",
    "PermissionRuleRepository",
    "RuleEffect",
    "RuleRow",
    "UnknownActionKey",
    "UnknownSystemGroup",
    "is_deployment_admin",
    "is_member_of",
    "is_owner_member",
    "require",
    "resolve_is_owner",
    "validate_catalog_integrity",
]
