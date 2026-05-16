"""Focused asset extraction domain tests for OCR-compatible persistence."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.domain.assets.extraction import (
    get_extraction,
    record_extraction_success,
    start_extraction,
)
from app.util.clock import FrozenClock
from tests.unit.test_assets_extraction import _NOW, _seed_document


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s
    engine.dispose()


def test_record_extraction_success_accepts_ocr_extractor(session: Session) -> None:
    clock = FrozenClock(_NOW)
    ctx, asset_id, document_id = _seed_document(session, clock=clock)

    start_extraction(session, ctx, document_id, clock=clock)
    record_extraction_success(
        session,
        ctx,
        document_id,
        extractor="ocr",
        body_text="Visible serial number",
        pages_json=[{"page": 1, "char_start": 0, "char_end": 21}],
        token_count=3,
        has_secret_marker=False,
        asset_id=asset_id,
        clock=clock,
    )

    view = get_extraction(session, ctx, document_id)
    assert view.status == "succeeded"
    assert view.body_preview == "Visible serial number"
    assert view.token_count == 3
