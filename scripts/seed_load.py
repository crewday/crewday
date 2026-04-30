"""Seed deterministic local data for the §17 Locust load harness."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import make_uow
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.config import get_settings
from app.tenancy import tenant_agnostic
from app.util.clock import SystemClock
from scripts.dev_login import mint_session

DEFAULT_HOST: Final[str] = "http://127.0.0.1:8100"
DEFAULT_WORKSPACE_SLUG: Final[str] = "load"
DEFAULT_OWNER_EMAIL: Final[str] = "load-owner@dev.local"
DEFAULT_WORKER_COUNT: Final[int] = 100
DEFAULT_OCCURRENCE_COUNT: Final[int] = 10_000
DEFAULT_TURNOVER_COUNT: Final[int] = 5
_SQLITE_SAFE_IN_CHUNK: Final[int] = 500


@dataclass(frozen=True, slots=True)
class LoadSeedConfig:
    workspace_slug: str = DEFAULT_WORKSPACE_SLUG
    owner_email: str = DEFAULT_OWNER_EMAIL
    worker_count: int = DEFAULT_WORKER_COUNT
    occurrence_count: int = DEFAULT_OCCURRENCE_COUNT
    turnover_count: int = DEFAULT_TURNOVER_COUNT


@dataclass(frozen=True, slots=True)
class LoadSeedIds:
    workspace_id: str
    property_id: str
    worker_ids: tuple[str, ...]
    occurrence_ids: tuple[str, ...]
    turnover_occurrence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LoadSeedResult:
    host: str
    workspace_slug: str
    session_cookie: str
    property_id: str
    worker_ids: tuple[str, ...]
    occurrence_ids: tuple[str, ...]
    turnover_occurrence_ids: tuple[str, ...]

    def env(self) -> dict[str, str]:
        return {
            "CREWDAY_LOAD_HOST": self.host,
            "CREWDAY_LOAD_WORKSPACE": self.workspace_slug,
            "CREWDAY_LOAD_SESSION_COOKIE": self.session_cookie,
            "CREWDAY_LOAD_PROPERTY_ID": self.property_id,
            "CREWDAY_LOAD_WORKER_IDS": ",".join(self.worker_ids),
            "CREWDAY_LOAD_OCCURRENCE_IDS": ",".join(self.turnover_occurrence_ids),
        }


def stable_load_id(kind: str, slug: str, index: int | None = None) -> str:
    """Return a deterministic 26-char id for seeded load-test rows."""

    seed = f"crewday-load:{slug}:{kind}"
    if index is not None:
        seed = f"{seed}:{index:06d}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest().upper()
    return f"LD{digest[:24]}"


def deterministic_ids(config: LoadSeedConfig, *, workspace_id: str) -> LoadSeedIds:
    worker_ids = tuple(
        stable_load_id("worker", config.workspace_slug, index)
        for index in range(config.worker_count)
    )
    occurrence_ids = tuple(
        stable_load_id("occurrence", config.workspace_slug, index)
        for index in range(config.occurrence_count)
    )
    return LoadSeedIds(
        workspace_id=workspace_id,
        property_id=stable_load_id("property", config.workspace_slug),
        worker_ids=worker_ids,
        occurrence_ids=occurrence_ids,
        turnover_occurrence_ids=occurrence_ids[: config.turnover_count],
    )


def ensure_load_seed(
    session: Session, config: LoadSeedConfig, *, now: datetime
) -> LoadSeedIds:
    """Idempotently prepare the deterministic rows the Locust scenarios need."""

    workspace = _workspace_by_slug(session, config.workspace_slug)
    if workspace is None:
        raise RuntimeError(
            f"workspace {config.workspace_slug!r} does not exist; mint a dev "
            "session before seeding"
        )
    ids = deterministic_ids(config, workspace_id=workspace.id)
    _ensure_property(session, ids, now=now)
    _ensure_workers(session, config, ids, now=now)
    _ensure_worker_memberships(session, ids, now=now)
    _ensure_occurrences(session, config, ids, now=now)
    session.flush()
    return ids


def _workspace_by_slug(session: Session, slug: str) -> Workspace | None:
    with tenant_agnostic():
        return session.scalar(select(Workspace).where(Workspace.slug == slug))


def _ensure_property(session: Session, ids: LoadSeedIds, *, now: datetime) -> None:
    with tenant_agnostic():
        prop = session.get(Property, ids.property_id)
        if prop is None:
            session.add(
                Property(
                    id=ids.property_id,
                    name="Load Villa",
                    kind="str",
                    address="1 Load Test Way",
                    address_json={
                        "line1": "1 Load Test Way",
                        "city": "Testville",
                        "country": "US",
                    },
                    country="US",
                    timezone="UTC",
                    lat=None,
                    lon=None,
                    tags_json=["load"],
                    welcome_defaults_json={},
                    property_notes_md="",
                    created_at=now,
                    updated_at=now,
                    deleted_at=None,
                )
            )
        link = session.get(
            PropertyWorkspace,
            {"property_id": ids.property_id, "workspace_id": ids.workspace_id},
        )
        if link is None:
            session.add(
                PropertyWorkspace(
                    property_id=ids.property_id,
                    workspace_id=ids.workspace_id,
                    label="Load Villa",
                    membership_role="owner_workspace",
                    share_guest_identity=False,
                    auto_shift_from_occurrence=False,
                    status="active",
                    created_at=now,
                )
            )


def _ensure_workers(
    session: Session, config: LoadSeedConfig, ids: LoadSeedIds, *, now: datetime
) -> None:
    with tenant_agnostic():
        existing = _existing_user_ids(session, ids.worker_ids)
        for index, user_id in enumerate(ids.worker_ids):
            if user_id in existing:
                continue
            email = canonicalise_email(
                f"load-worker-{index:03d}@{config.workspace_slug}.dev.local"
            )
            session.add(
                User(
                    id=user_id,
                    email=email,
                    email_lower=email,
                    display_name=f"Load Worker {index:03d}",
                    locale=None,
                    timezone="UTC",
                    avatar_blob_hash=None,
                    created_at=now,
                    last_login_at=None,
                )
            )


def _ensure_worker_memberships(
    session: Session, ids: LoadSeedIds, *, now: datetime
) -> None:
    with tenant_agnostic():
        for user_id in ids.worker_ids:
            membership = session.get(
                UserWorkspace,
                {"user_id": user_id, "workspace_id": ids.workspace_id},
            )
            if membership is None:
                session.add(
                    UserWorkspace(
                        user_id=user_id,
                        workspace_id=ids.workspace_id,
                        source="workspace_grant",
                        added_at=now,
                    )
                )
            grant = session.scalar(
                select(RoleGrant).where(
                    RoleGrant.workspace_id == ids.workspace_id,
                    RoleGrant.user_id == user_id,
                    RoleGrant.grant_role == "worker",
                    RoleGrant.scope_property_id.is_(None),
                )
            )
            if grant is None:
                session.add(
                    RoleGrant(
                        id=stable_load_id("worker-role-grant", user_id),
                        workspace_id=ids.workspace_id,
                        user_id=user_id,
                        grant_role="worker",
                        scope_property_id=None,
                        created_at=now,
                        created_by_user_id=None,
                    )
                )


def _ensure_occurrences(
    session: Session, config: LoadSeedConfig, ids: LoadSeedIds, *, now: datetime
) -> None:
    existing = _existing_occurrence_ids(session, ids.occurrence_ids)
    base = datetime(2020, 1, 1, 8, 0, tzinfo=UTC)
    for index, occurrence_id in enumerate(ids.occurrence_ids):
        if occurrence_id in existing:
            continue
        starts_at = base + timedelta(hours=index)
        assignee = (
            ids.worker_ids[index % len(ids.worker_ids)] if ids.worker_ids else None
        )
        session.add(
            Occurrence(
                id=occurrence_id,
                workspace_id=ids.workspace_id,
                schedule_id=None,
                template_id=None,
                property_id=ids.property_id,
                assignee_user_id=assignee,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(minutes=45),
                scheduled_for_local=starts_at.strftime("%Y-%m-%dT%H:%M"),
                originally_scheduled_for=starts_at.strftime("%Y-%m-%dT%H:%M"),
                state="pending",
                overdue_since=None,
                completed_at=None,
                completed_by_user_id=None,
                reviewer_user_id=None,
                reviewed_at=None,
                cancellation_reason=None,
                title=f"Load task {index:05d}",
                description_md="Seeded by scripts/seed_load.py",
                priority="normal",
                photo_evidence="optional",
                duration_minutes=45,
                area_id=None,
                unit_id=None,
                expected_role_id=None,
                linked_instruction_ids=[],
                inventory_consumption_json={},
                is_personal=False,
                created_by_user_id=None,
                created_at=now,
            )
        )


def _existing_user_ids(session: Session, user_ids: tuple[str, ...]) -> set[str]:
    existing: set[str] = set()
    for chunk in _chunks(user_ids, _SQLITE_SAFE_IN_CHUNK):
        existing.update(
            session.scalars(select(User.id).where(User.id.in_(chunk))).all()
        )
    return existing


def _existing_occurrence_ids(
    session: Session, occurrence_ids: tuple[str, ...]
) -> set[str]:
    existing: set[str] = set()
    for chunk in _chunks(occurrence_ids, _SQLITE_SAFE_IN_CHUNK):
        existing.update(
            session.scalars(select(Occurrence.id).where(Occurrence.id.in_(chunk))).all()
        )
    return existing


def _chunks(values: tuple[str, ...], size: int) -> Iterator[tuple[str, ...]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def seed(config: LoadSeedConfig, *, host: str = DEFAULT_HOST) -> LoadSeedResult:
    _guard_local_dev_database()
    mint = mint_session(
        email=config.owner_email,
        workspace_slug=config.workspace_slug,
        display_name="Load Owner",
        timezone="UTC",
        role="owner",
    )
    with make_uow() as uow_session:
        if not isinstance(uow_session, Session):
            raise TypeError("make_uow returned a non-SQLAlchemy session")
        ids = ensure_load_seed(uow_session, config, now=SystemClock().now())
    return LoadSeedResult(
        host=host.rstrip("/"),
        workspace_slug=config.workspace_slug,
        session_cookie=f"crewday_session={mint.session_issue.cookie_value}",
        property_id=ids.property_id,
        worker_ids=ids.worker_ids,
        occurrence_ids=ids.occurrence_ids,
        turnover_occurrence_ids=ids.turnover_occurrence_ids,
    )


def _guard_local_dev_database() -> None:
    settings = get_settings()
    scheme = settings.database_url.split(":", 1)[0].lower()
    if settings.profile != "dev" or not scheme.startswith("sqlite"):
        raise RuntimeError(
            "seed_load.py only runs against the local dev SQLite profile"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE_SLUG)
    parser.add_argument("--owner-email", default=DEFAULT_OWNER_EMAIL)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKER_COUNT)
    parser.add_argument("--occurrences", type=int, default=DEFAULT_OCCURRENCE_COUNT)
    parser.add_argument("--turnover", type=int, default=DEFAULT_TURNOVER_COUNT)
    parser.add_argument("--json", action="store_true", help="print JSON instead of env")
    return parser


def main() -> None:
    args = _parser().parse_args()
    config = LoadSeedConfig(
        workspace_slug=args.workspace,
        owner_email=args.owner_email,
        worker_count=args.workers,
        occurrence_count=args.occurrences,
        turnover_count=args.turnover,
    )
    result = seed(config, host=args.host)
    if args.json:
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
        return
    for name, value in result.env().items():
        print(f"export {name}={json.dumps(value)}")


if __name__ == "__main__":
    main()
