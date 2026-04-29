"""Unit tests for :mod:`app.api.pagination`."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi import HTTPException
from sqlalchemy import Engine, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Cursor,
    CursorPage,
    CursorScalar,
    Page,
    SortSpec,
    decode_cursor,
    decode_page_cursor,
    encode_cursor,
    encode_page_cursor,
    paginate,
    paginate_query,
    validate_limit,
)
from app.config import get_settings


@pytest.fixture(autouse=True)
def _pagination_signing_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("CREWDAY_ROOT_KEY", "unit-test-pagination-root-key")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@dataclass(frozen=True, slots=True)
class _Row:
    """Minimal stand-in for a domain view with an ``id`` key."""

    id: str


class _Base(DeclarativeBase):
    pass


class _Thing(_Base):
    __tablename__ = "thing"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)


class _NullableThing(_Base):
    __tablename__ = "nullable_thing"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)


@pytest.fixture
def session() -> Iterator[Session]:
    engine: Engine = create_engine("sqlite:///:memory:")
    _Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as db:
        yield db


def _invalid_cursor_detail(exc: HTTPException) -> dict[str, object]:
    detail = exc.detail
    assert isinstance(detail, dict)
    return detail


class TestCursorRoundTrip:
    """A key survives signed encode / decode unchanged."""

    def test_roundtrip_preserves_key(self) -> None:
        key = "01HW9ZABCDE1234567890ABCDE"
        assert decode_cursor(encode_cursor(key)) == key

    def test_structured_cursor_roundtrip(self) -> None:
        cursor = Cursor(last_sort_value=7, last_id_ulid="01HW9ZABCDE1234567890ABCDE")
        encoded = encode_page_cursor(cursor)
        assert decode_page_cursor(encoded) == cursor
        assert decode_cursor(encoded) == "01HW9ZABCDE1234567890ABCDE"

    def test_none_cursor_decodes_to_none(self) -> None:
        assert decode_cursor(None) is None
        assert decode_page_cursor(None) is None

    def test_empty_string_decodes_to_none(self) -> None:
        assert decode_cursor("") is None
        assert decode_page_cursor("") is None

    def test_unsigned_base64_cursor_raises_422(self) -> None:
        unsigned = "MDFIVzlBQUFBQUFBQUFBQUFBQUFBQUFBQUFB"
        with pytest.raises(HTTPException) as exc_info:
            decode_cursor(unsigned)
        assert exc_info.value.status_code == 422
        assert _invalid_cursor_detail(exc_info.value)["error"] == "invalid_cursor"

    def test_tampered_cursor_raises_422(self) -> None:
        encoded = encode_cursor("01HW9ZABCDE1234567890ABCDE")
        replacement = "A" if encoded[-1] != "A" else "B"
        tampered = f"{encoded[:-1]}{replacement}"
        with pytest.raises(HTTPException) as exc_info:
            decode_cursor(tampered)
        assert exc_info.value.status_code == 422
        assert _invalid_cursor_detail(exc_info.value)["error"] == "invalid_cursor"

    def test_malformed_cursor_raises_422(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            decode_cursor("not a cursor!!!!")
        assert exc_info.value.status_code == 422
        assert _invalid_cursor_detail(exc_info.value)["error"] == "invalid_cursor"

    def test_missing_root_key_still_signs_with_process_local_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CREWDAY_ROOT_KEY", raising=False)
        get_settings.cache_clear()

        encoded = encode_cursor("01HW9ZABCDE1234567890ABCDE")

        assert decode_cursor(encoded) == "01HW9ZABCDE1234567890ABCDE"


class TestPaginate:
    """Envelope-building semantics for existing router compatibility."""

    def test_no_overflow_has_no_cursor(self) -> None:
        rows = [_Row(id=f"id{i}") for i in range(3)]
        page = paginate(rows, limit=5, key_getter=lambda row: row.id)
        assert page.has_more is False
        assert page.next_cursor is None
        assert [row.id for row in page.items] == ["id0", "id1", "id2"]

    def test_exactly_limit_has_no_cursor(self) -> None:
        rows = [_Row(id=f"id{i}") for i in range(5)]
        page = paginate(rows, limit=5, key_getter=lambda row: row.id)
        assert page.has_more is False
        assert page.next_cursor is None

    def test_overflow_trims_and_encodes_last_returned(self) -> None:
        rows = [_Row(id=f"id{i:02}") for i in range(6)]
        page = paginate(rows, limit=5, key_getter=lambda row: row.id)
        assert page.has_more is True
        assert [row.id for row in page.items] == [f"id{i:02}" for i in range(5)]
        assert page.next_cursor is not None
        assert decode_cursor(page.next_cursor) == "id04"

    def test_empty_rows(self) -> None:
        page: CursorPage[_Row] = paginate([], limit=5, key_getter=lambda row: row.id)
        assert page.items == ()
        assert page.next_cursor is None
        assert page.has_more is False

    def test_invalid_limit_raises_422(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            paginate([], limit=0, key_getter=lambda row: row.id)
        assert exc_info.value.status_code == 422

    def test_overflow_without_key_raises(self) -> None:
        rows = [_Row(id="id0"), _Row(id="id1")]
        with pytest.raises(ValueError):
            paginate(rows, limit=1)

    def test_explicit_key_overrides_getter_absence(self) -> None:
        rows = [_Row(id="id0"), _Row(id="id1")]
        page = paginate(rows, limit=1, key="id0")
        assert page.has_more is True
        assert decode_cursor(page.next_cursor) == "id0"


class TestBounds:
    """Spec §12 pins default=50 / max=500."""

    def test_default_limit(self) -> None:
        assert DEFAULT_LIMIT == 50

    def test_max_limit(self) -> None:
        assert MAX_LIMIT == 500

    def test_limit_over_max_raises_422_validation(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            validate_limit(MAX_LIMIT + 1)
        assert exc_info.value.status_code == 422
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "validation"


class TestPageEnvelope:
    def test_total_estimate_omitted_when_not_opted_in(self) -> None:
        page = Page[str](data=("a",), next_cursor=None, has_more=False)
        assert page.model_dump() == {
            "data": ("a",),
            "next_cursor": None,
            "has_more": False,
        }

    def test_total_estimate_present_when_opted_in(self) -> None:
        page = Page[str](
            data=("a",),
            next_cursor=None,
            has_more=False,
            total_estimate=12,
        )
        assert page.model_dump()["total_estimate"] == 12


class TestPaginateQuery:
    def _parse_rank(self, value: CursorScalar) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("rank cursor must be an integer")
        return value

    def _sort(self) -> SortSpec[_Thing, int]:
        return SortSpec(
            column=_Thing.rank.expression,
            value_getter=lambda row: row.rank,
            parse_value=self._parse_rank,
            direction="asc",
        )

    def _nullable_sort(self) -> SortSpec[_NullableThing, int | None]:
        def parse(value: CursorScalar) -> int | None:
            if value is None:
                return None
            return self._parse_rank(value)

        return SortSpec(
            column=_NullableThing.rank.expression,
            value_getter=lambda row: row.rank,
            parse_value=parse,
            direction="asc",
        )

    def test_sqlalchemy_helper_has_more_and_total(self, session: Session) -> None:
        session.add_all(
            [
                _Thing(id="id-01", rank=1, label="one"),
                _Thing(id="id-02", rank=1, label="two"),
                _Thing(id="id-03", rank=2, label="three"),
            ]
        )
        session.commit()

        page = paginate_query(
            session,
            select(_Thing),
            sort=self._sort(),
            id_column=_Thing.id.expression,
            id_getter=lambda row: row.id,
            limit=2,
            include_total=True,
        )

        assert [row.id for row in page.data] == ["id-01", "id-02"]
        assert page.has_more is True
        assert page.next_cursor is not None
        assert page.total_estimate == 3

    def test_sqlalchemy_helper_has_more_false(self, session: Session) -> None:
        session.add(_Thing(id="id-01", rank=1, label="one"))
        session.commit()

        page = paginate_query(
            session,
            select(_Thing),
            sort=self._sort(),
            id_column=_Thing.id.expression,
            id_getter=lambda row: row.id,
            limit=2,
        )

        assert [row.id for row in page.data] == ["id-01"]
        assert page.has_more is False
        assert page.next_cursor is None
        assert page.model_dump() == {
            "data": (page.data[0],),
            "next_cursor": None,
            "has_more": False,
        }

    def test_stable_under_write_before_cursor(self, session: Session) -> None:
        session.add_all(
            [
                _Thing(id="id-01", rank=1, label="one"),
                _Thing(id="id-02", rank=1, label="two"),
                _Thing(id="id-03", rank=2, label="three"),
            ]
        )
        session.commit()

        page1 = paginate_query(
            session,
            select(_Thing),
            sort=self._sort(),
            id_column=_Thing.id.expression,
            id_getter=lambda row: row.id,
            limit=2,
        )
        assert page1.next_cursor is not None

        session.add(_Thing(id="id-015", rank=1, label="inserted before cursor"))
        session.commit()

        page2 = paginate_query(
            session,
            select(_Thing),
            sort=self._sort(),
            id_column=_Thing.id.expression,
            id_getter=lambda row: row.id,
            limit=2,
            cursor=page1.next_cursor,
        )

        assert [row.id for row in page2.data] == ["id-03"]
        assert page2.has_more is False

    def test_nullable_sort_values_page_after_null_boundary(
        self, session: Session
    ) -> None:
        session.add_all(
            [
                _NullableThing(id="id-01", rank=1),
                _NullableThing(id="id-02", rank=None),
                _NullableThing(id="id-03", rank=None),
            ]
        )
        session.commit()

        page1 = paginate_query(
            session,
            select(_NullableThing),
            sort=self._nullable_sort(),
            id_column=_NullableThing.id.expression,
            id_getter=lambda row: row.id,
            limit=2,
        )
        assert [row.id for row in page1.data] == ["id-01", "id-02"]
        assert page1.next_cursor is not None

        page2 = paginate_query(
            session,
            select(_NullableThing),
            sort=self._nullable_sort(),
            id_column=_NullableThing.id.expression,
            id_getter=lambda row: row.id,
            limit=2,
            cursor=page1.next_cursor,
        )

        assert [row.id for row in page2.data] == ["id-03"]
        assert page2.has_more is False

    def test_cursor_for_wrong_sort_shape_returns_422(self, session: Session) -> None:
        session.add(_Thing(id="id-01", rank=1, label="one"))
        session.commit()
        wrong_shape = encode_page_cursor(
            Cursor(last_sort_value="not-an-int", last_id_ulid="id-01")
        )

        with pytest.raises(HTTPException) as exc_info:
            paginate_query(
                session,
                select(_Thing),
                sort=self._sort(),
                id_column=_Thing.id.expression,
                id_getter=lambda row: row.id,
                limit=2,
                cursor=wrong_shape,
            )

        assert exc_info.value.status_code == 422
        assert _invalid_cursor_detail(exc_info.value)["error"] == "invalid_cursor"
