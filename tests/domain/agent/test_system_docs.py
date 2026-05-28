"""Tests for system-doc seeding."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.llm.models import AgentDoc, AgentDocRevision
from app.services.agent.system_docs import agent_doc_metadata_hash, seed_agent_docs
from tests.unit.api.admin._helpers import engine_fixture


def test_code_default_upgrade_snapshots_previous_body(
    tmp_path: Path,
) -> None:
    engine_iter = engine_fixture()
    engine = next(engine_iter)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    seed_path = tmp_path / "doc.md"
    try:
        _write_seed(seed_path, body="first body")
        with session_factory() as s:
            seed_agent_docs(s, root=tmp_path)
            s.commit()

        _write_seed(seed_path, body="second body")
        with session_factory() as s:
            seed_agent_docs(s, root=tmp_path)
            row = s.scalar(select(AgentDoc).where(AgentDoc.slug == "test_doc"))
            revisions = s.scalars(select(AgentDocRevision)).all()
    finally:
        engine.dispose()

    assert row is not None
    assert row.version == 2
    assert row.body_md == "# Test\n\nsecond body\n"
    assert len(revisions) == 1
    assert revisions[0].doc_id == row.id
    assert revisions[0].version == 1
    assert revisions[0].body_md == "# Test\n\nfirst body\n"
    assert revisions[0].roles == ["admin"]
    assert revisions[0].created_by_user_id is None


def test_missing_metadata_hash_backfills_without_revision_or_version_bump(
    tmp_path: Path,
) -> None:
    engine_iter = engine_fixture()
    engine = next(engine_iter)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    seed_path = tmp_path / "doc.md"
    try:
        _write_seed(seed_path, body="first body", roles=["admin"])
        with session_factory() as s:
            seed_agent_docs(s, root=tmp_path)
            row = s.scalar(select(AgentDoc).where(AgentDoc.slug == "test_doc"))
            assert row is not None
            row.metadata_default_hash = ""
            s.commit()

        _write_seed(seed_path, body="first body", roles=["employee"])
        with session_factory() as s:
            seed_agent_docs(s, root=tmp_path)
            row = s.scalar(select(AgentDoc).where(AgentDoc.slug == "test_doc"))
            revisions = s.scalars(select(AgentDocRevision)).all()
    finally:
        engine.dispose()

    assert row is not None
    assert row.roles == ["employee"]
    assert row.metadata_default_hash == agent_doc_metadata_hash(["employee"])
    assert row.version == 1
    assert revisions == []


def test_code_role_change_preserves_operator_edited_roles(
    tmp_path: Path,
) -> None:
    engine_iter = engine_fixture()
    engine = next(engine_iter)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    seed_path = tmp_path / "doc.md"
    try:
        _write_seed(seed_path, body="first body", roles=["admin"])
        with session_factory() as s:
            seed_agent_docs(s, root=tmp_path)
            row = s.scalar(select(AgentDoc).where(AgentDoc.slug == "test_doc"))
            assert row is not None
            row.roles = ["manager"]
            s.commit()

        _write_seed(seed_path, body="first body", roles=["employee"])
        with session_factory() as s:
            seed_agent_docs(s, root=tmp_path)
            row = s.scalar(select(AgentDoc).where(AgentDoc.slug == "test_doc"))
            revisions = s.scalars(select(AgentDocRevision)).all()
    finally:
        engine.dispose()

    assert row is not None
    assert row.roles == ["manager"]
    assert row.metadata_default_hash == agent_doc_metadata_hash(["employee"])
    assert row.version == 1
    assert revisions == []


def test_code_role_change_auto_upgrades_unmodified_roles(
    tmp_path: Path,
) -> None:
    engine_iter = engine_fixture()
    engine = next(engine_iter)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    seed_path = tmp_path / "doc.md"
    try:
        _write_seed(seed_path, body="first body", roles=["admin"])
        with session_factory() as s:
            seed_agent_docs(s, root=tmp_path)
            s.commit()

        _write_seed(seed_path, body="first body", roles=["manager", "employee"])
        with session_factory() as s:
            seed_agent_docs(s, root=tmp_path)
            row = s.scalar(select(AgentDoc).where(AgentDoc.slug == "test_doc"))
            revisions = s.scalars(select(AgentDocRevision)).all()
    finally:
        engine.dispose()

    assert row is not None
    assert row.roles == ["manager", "employee"]
    assert row.metadata_default_hash == agent_doc_metadata_hash(["manager", "employee"])
    assert row.version == 2
    assert len(revisions) == 1
    assert revisions[0].roles == ["admin"]


def _write_seed(
    path: Path,
    *,
    body: str,
    roles: list[str] | None = None,
) -> None:
    role_yaml = ", ".join(roles or ["admin"])
    path.write_text(
        "---\n"
        "slug: test_doc\n"
        "title: Test doc\n"
        "summary: Test summary\n"
        f"roles: [{role_yaml}]\n"
        "capabilities: [chat.admin]\n"
        "---\n\n"
        "# Test\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
