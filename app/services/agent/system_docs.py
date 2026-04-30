"""Seed and read deployment-scoped system docs for chat agents."""

from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import AgentDoc, AgentDocRevision
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid

__all__ = [
    "DEFAULT_AGENT_DOCS_ROOT",
    "AgentDocSeed",
    "get_agent_doc",
    "list_agent_docs",
    "load_agent_doc_seeds",
    "seed_agent_docs",
]

DEFAULT_AGENT_DOCS_ROOT = Path(__file__).resolve().parents[2] / "agent_docs"
DEFAULT_CAPABILITIES = ("chat.manager", "chat.employee", "chat.admin")
_log = logging.getLogger(__name__)
_SEED_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class AgentDocSeed:
    """Parsed code default for one ``app/agent_docs/*.md`` file."""

    slug: str
    title: str
    summary: str | None
    body_md: str
    roles: tuple[str, ...]
    capabilities: tuple[str, ...]
    default_hash: str


def list_agent_docs(session: Session) -> list[AgentDoc]:
    """Return active system docs, seeding code defaults first."""
    seed_agent_docs(session)
    return list(
        session.scalars(
            select(AgentDoc)
            .where(AgentDoc.is_active.is_(True))
            .order_by(AgentDoc.title.asc(), AgentDoc.slug.asc())
        ).all()
    )


def get_agent_doc(session: Session, slug: str) -> AgentDoc | None:
    """Return one active system doc by slug, seeding code defaults first."""
    seed_agent_docs(session)
    return session.scalar(
        select(AgentDoc).where(AgentDoc.slug == slug, AgentDoc.is_active.is_(True))
    )


def seed_agent_docs(
    session: Session,
    *,
    root: Path = DEFAULT_AGENT_DOCS_ROOT,
    now: datetime | None = None,
) -> None:
    """Synchronise active ``agent_doc`` rows with code-shipped Markdown files."""
    seeds = load_agent_doc_seeds(root)
    if not seeds:
        return

    timestamp = now or datetime.now(UTC)
    lock = _SEED_LOCK if root == DEFAULT_AGENT_DOCS_ROOT else nullcontext()
    with lock, tenant_agnostic():
        existing = {
            row.slug: row
            for row in session.scalars(
                select(AgentDoc).where(AgentDoc.is_active.is_(True))
            )
        }
        for seed in seeds:
            row = existing.get(seed.slug)
            if row is None:
                session.add(
                    AgentDoc(
                        id=new_ulid(),
                        slug=seed.slug,
                        title=seed.title,
                        summary=seed.summary,
                        body_md=seed.body_md,
                        roles=list(seed.roles),
                        capabilities=list(seed.capabilities),
                        version=1,
                        is_active=True,
                        default_hash=seed.default_hash,
                        notes=None,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                )
                _log.info(
                    "agent doc seeded",
                    extra={
                        "event": "template.seeded",
                        "table": "agent_doc",
                        "slug": seed.slug,
                    },
                )
                continue

            _sync_existing(session, row, seed, timestamp)
        session.flush()


def load_agent_doc_seeds(
    root: Path = DEFAULT_AGENT_DOCS_ROOT,
) -> tuple[AgentDocSeed, ...]:
    """Parse every Markdown seed file under ``root``."""
    if not root.exists():
        return ()
    seeds = [_parse_seed(path) for path in sorted(root.glob("*.md"))]
    return tuple(seed for seed in seeds if seed is not None)


def _sync_existing(
    session: Session,
    row: AgentDoc,
    seed: AgentDocSeed,
    now: datetime,
) -> None:
    metadata_changed = (
        row.title != seed.title
        or row.summary != seed.summary
        or tuple(row.roles) != seed.roles
        or tuple(row.capabilities) != seed.capabilities
    )
    default_changed = row.default_hash != seed.default_hash
    if not metadata_changed and not default_changed:
        return

    row.title = seed.title
    row.summary = seed.summary
    row.roles = list(seed.roles)
    row.capabilities = list(seed.capabilities)

    if default_changed:
        old_body_hash = _hash_body(row.body_md)
        if old_body_hash == row.default_hash:
            session.add(
                AgentDocRevision(
                    id=new_ulid(),
                    doc_id=row.id,
                    version=row.version,
                    body_md=row.body_md,
                    notes="Code default auto-upgrade",
                    created_at=now,
                    created_by_user_id=None,
                )
            )
            row.body_md = seed.body_md
            row.version += 1
            _log.info(
                "agent doc auto-upgraded",
                extra={
                    "event": "template.auto_upgraded",
                    "table": "agent_doc",
                    "slug": row.slug,
                    "version": row.version,
                },
            )
        else:
            _log.warning(
                "agent doc customised while code default changed",
                extra={
                    "event": "template.customised_code_default_changed",
                    "table": "agent_doc",
                    "slug": row.slug,
                    "version": row.version,
                },
            )
        row.default_hash = seed.default_hash

    row.updated_at = now


def _parse_seed(path: Path) -> AgentDocSeed | None:
    raw = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(raw)
    if frontmatter is None:
        return None
    meta = _as_mapping(yaml.safe_load(frontmatter), path)
    slug = _required_string(meta, "slug", path)
    title = _required_string(meta, "title", path)
    summary = _optional_string(meta, "summary", path)
    roles = _required_string_list(meta, "roles", path)
    capabilities = (
        _optional_string_list(meta, "capabilities", path) or DEFAULT_CAPABILITIES
    )
    body_md = body.strip() + "\n"
    return AgentDocSeed(
        slug=slug,
        title=title,
        summary=summary,
        body_md=body_md,
        roles=roles,
        capabilities=capabilities,
        default_hash=_hash_body(body_md),
    )


def _split_frontmatter(raw: str) -> tuple[str | None, str]:
    if not raw.startswith("---\n"):
        return None, raw
    marker = "\n---\n"
    end = raw.find(marker, 4)
    if end == -1:
        return None, raw
    return raw[4:end], raw[end + len(marker) :]


def _as_mapping(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} front-matter must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{path} front-matter keys must be strings")
    return value


def _required_string(meta: dict[str, Any], key: str, path: Path) -> str:
    value = meta.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} front-matter {key!r} must be a non-empty string")
    return value.strip()


def _optional_string(meta: dict[str, Any], key: str, path: Path) -> str | None:
    value = meta.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{path} front-matter {key!r} must be a string")
    stripped = value.strip()
    return stripped or None


def _required_string_list(
    meta: dict[str, Any],
    key: str,
    path: Path,
) -> tuple[str, ...]:
    values = _optional_string_list(meta, key, path)
    if not values:
        raise ValueError(f"{path} front-matter {key!r} must contain at least one value")
    return values


def _optional_string_list(
    meta: dict[str, Any], key: str, path: Path
) -> tuple[str, ...] | None:
    value = meta.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{path} front-matter {key!r} must be a string list")
    cleaned = tuple(item.strip() for item in value if item.strip())
    return cleaned or None


def _hash_body(body_md: str) -> str:
    return sha256(body_md.encode("utf-8")).hexdigest()[:16]
