"""Round-trip tests for :class:`app.adapters.db._columns.UtcDateTime`
(cd-xma93).

Covers the documented invariants:

1. **Naive write -> aware UTC read** (the SQLite-only bug the type
   exists to fix). A caller that hands the mapper a naive
   ``datetime`` (legacy ``utcnow()`` shape) gets a tz-aware UTC
   value back from the column.
2. **Aware UTC write -> aware UTC read** (Postgres parity). The
   common case stays the common case.
3. **Aware non-UTC write -> aware UTC read**. The decorator
   normalises every input to UTC on the bind path.
4. **None preserved on both sides**. Optional columns continue to
   round-trip ``None`` unchanged.
5. **TypeDecorator metadata** — ``cache_ok=True`` (statement caching
   safe) and ``python_type`` reports ``datetime``.

These run on an in-memory SQLite engine because the bug is a SQLite
artefact (Postgres' driver returns aware values without help). The
read side is trivially a no-op on Postgres; the write side adds a
comparable normalisation step on both backends.

See ``app/adapters/db/_columns.py`` for the docstring documenting
write-side semantics ("naive in -> attach UTC").
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import Column, MetaData, String, Table, create_engine, insert, select
from sqlalchemy.pool import StaticPool

from app.adapters.db._columns import UtcDateTime


def _table() -> tuple[Table, MetaData]:
    """Throw-away one-column table for the round-trip tests.

    A fresh ``MetaData`` per test keeps these isolated from any other
    metadata-binding tests in the suite.
    """
    md = MetaData()
    t = Table(
        "utc_datetime_probe",
        md,
        Column("key", String, primary_key=True),
        Column("ts", UtcDateTime(), nullable=True),
    )
    return t, md


def _engine() -> object:
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    return eng


def test_naive_in_aware_utc_out() -> None:
    """A naive ``datetime`` written to a ``UtcDateTime`` column reads
    back as aware UTC on SQLite (the cd-xma93 bug)."""
    table, md = _table()
    eng = _engine()
    md.create_all(eng)

    naive = datetime(2026, 5, 2, 12, 34, 56, 789012)
    assert naive.tzinfo is None  # sanity

    with eng.begin() as conn:
        conn.execute(insert(table).values(key="naive", ts=naive))
        row = conn.execute(select(table.c.ts).where(table.c.key == "naive")).one()

    out = row[0]
    assert isinstance(out, datetime)
    assert out.tzinfo is not None
    assert out.utcoffset() == timedelta(0)
    # Wall-clock instant preserved (caller meant UTC).
    assert out == naive.replace(tzinfo=UTC)


def test_aware_utc_in_aware_utc_out() -> None:
    """Aware UTC datetimes round-trip unchanged."""
    table, md = _table()
    eng = _engine()
    md.create_all(eng)

    aware = datetime(2026, 5, 2, 12, 34, 56, 789012, tzinfo=UTC)

    with eng.begin() as conn:
        conn.execute(insert(table).values(key="aware", ts=aware))
        row = conn.execute(select(table.c.ts).where(table.c.key == "aware")).one()

    out = row[0]
    assert out == aware
    assert out.tzinfo is not None
    assert out.utcoffset() == timedelta(0)


def test_aware_non_utc_in_normalised_to_utc() -> None:
    """An aware non-UTC datetime gets converted to UTC on write so
    the read-side projection matches the wall-clock instant in UTC.
    """
    table, md = _table()
    eng = _engine()
    md.create_all(eng)

    plus_two = timezone(timedelta(hours=2))
    aware = datetime(2026, 5, 2, 14, 0, 0, tzinfo=plus_two)
    expected_utc = datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC)

    with eng.begin() as conn:
        conn.execute(insert(table).values(key="cest", ts=aware))
        row = conn.execute(select(table.c.ts).where(table.c.key == "cest")).one()

    out = row[0]
    assert out == expected_utc
    assert out.utcoffset() == timedelta(0)


def test_none_round_trips_as_none() -> None:
    """``None`` is preserved on both bind and result paths."""
    table, md = _table()
    eng = _engine()
    md.create_all(eng)

    with eng.begin() as conn:
        conn.execute(insert(table).values(key="null", ts=None))
        row = conn.execute(select(table.c.ts).where(table.c.key == "null")).one()

    assert row[0] is None


def test_decorator_metadata() -> None:
    """``cache_ok = True`` keeps SQLAlchemy's statement cache happy;
    ``python_type`` reports ``datetime`` for the ORM type discovery
    path.
    """
    decorator = UtcDateTime()
    assert decorator.cache_ok is True
    assert decorator.python_type is datetime


def test_copy_returns_independent_instance() -> None:
    """:meth:`UtcDateTime.copy` produces a fresh instance — required
    by SQLAlchemy when columns are inherited or rebound, and by
    ``mypy --strict`` against the parent ``TypeDecorator`` baseline.
    """
    a = UtcDateTime()
    b = a.copy()
    assert isinstance(b, UtcDateTime)
    assert b is not a
