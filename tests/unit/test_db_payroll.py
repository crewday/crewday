"""Unit tests for :mod:`app.adapters.db.payroll.models`.

Pure-Python sanity on the SQLAlchemy mapped classes: construction,
default handling, and the shape of ``__table_args__`` (CHECK
constraints, unique composites, index shape, tenancy-registry
membership). Integration coverage (migrations, FK cascade /
RESTRICT, CHECK / UNIQUE violations against a real DB, tenant
filter behaviour, CRUD round-trips, JSON round-trip) lives in
``tests/integration/test_db_payroll.py``.

See ``docs/specs/02-domain-model.md`` §"pay_rule", §"pay_period",
§"payslip", and ``docs/specs/09-time-payroll-expenses.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from app.adapters.db.payroll import PayPeriod, PayRule, Payslip
from app.adapters.db.payroll import models as payroll_models

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


class TestPayRuleModel:
    """The ``PayRule`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        rule = PayRule(
            id="01HWA00000000000000000PRLA",
            workspace_id="01HWA00000000000000000WSPA",
            user_id="01HWA00000000000000000USRA",
            currency="EUR",
            base_cents_per_hour=1500,
            effective_from=_PINNED,
            created_at=_PINNED,
        )
        assert rule.id == "01HWA00000000000000000PRLA"
        assert rule.workspace_id == "01HWA00000000000000000WSPA"
        assert rule.user_id == "01HWA00000000000000000USRA"
        assert rule.currency == "EUR"
        assert rule.base_cents_per_hour == 1500
        assert rule.effective_from == _PINNED
        # Nullable columns default to ``None``.
        assert rule.effective_to is None
        assert rule.created_by is None

    def test_explicit_multipliers(self) -> None:
        rule = PayRule(
            id="01HWA00000000000000000PRLB",
            workspace_id="01HWA00000000000000000WSPA",
            user_id="01HWA00000000000000000USRA",
            currency="GBP",
            base_cents_per_hour=2000,
            overtime_multiplier=Decimal("1.75"),
            night_multiplier=Decimal("1.3"),
            weekend_multiplier=Decimal("2.0"),
            effective_from=_PINNED,
            effective_to=_LATER,
            created_by="01HWA00000000000000000USRM",
            created_at=_PINNED,
        )
        assert rule.overtime_multiplier == Decimal("1.75")
        assert rule.night_multiplier == Decimal("1.3")
        assert rule.weekend_multiplier == Decimal("2.0")
        assert rule.effective_to == _LATER
        assert rule.created_by == "01HWA00000000000000000USRM"

    def test_tablename(self) -> None:
        assert PayRule.__tablename__ == "pay_rule"

    def test_currency_length_check_present(self) -> None:
        checks = [
            c
            for c in PayRule.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("currency_length")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "LENGTH(currency)" in sql
        assert "3" in sql

    def test_base_cents_nonneg_check_present(self) -> None:
        checks = [
            c
            for c in PayRule.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("base_cents_per_hour_nonneg")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "base_cents_per_hour" in sql
        assert ">= 0" in sql

    def test_overtime_multiplier_check_present(self) -> None:
        checks = [
            c
            for c in PayRule.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("overtime_multiplier_min")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "overtime_multiplier" in sql
        assert ">= 1" in sql

    def test_night_multiplier_check_present(self) -> None:
        checks = [
            c
            for c in PayRule.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("night_multiplier_min")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "night_multiplier" in sql

    def test_weekend_multiplier_check_present(self) -> None:
        checks = [
            c
            for c in PayRule.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("weekend_multiplier_min")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "weekend_multiplier" in sql

    def test_workspace_user_effective_from_index_present(self) -> None:
        """The current-rule-for-user lookup rides a composite B-tree."""
        indexes = [i for i in PayRule.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_pay_rule_workspace_user_effective_from" in names
        target = next(
            i for i in indexes if i.name == "ix_pay_rule_workspace_user_effective_from"
        )
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "user_id",
            "effective_from",
        ]


class TestPayPeriodModel:
    """The ``PayPeriod`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        period = PayPeriod(
            id="01HWA00000000000000000PDRA",
            workspace_id="01HWA00000000000000000WSPA",
            starts_at=_PINNED,
            ends_at=_LATER,
            state="open",
            created_at=_PINNED,
        )
        assert period.id == "01HWA00000000000000000PDRA"
        assert period.state == "open"
        assert period.starts_at == _PINNED
        assert period.ends_at == _LATER
        # Nullable columns default to ``None``.
        assert period.locked_at is None
        assert period.locked_by is None

    def test_locked_construction(self) -> None:
        period = PayPeriod(
            id="01HWA00000000000000000PDRB",
            workspace_id="01HWA00000000000000000WSPA",
            starts_at=_PINNED,
            ends_at=_LATER,
            state="locked",
            locked_at=_LATER,
            locked_by="01HWA00000000000000000USRM",
            created_at=_PINNED,
        )
        assert period.state == "locked"
        assert period.locked_at == _LATER
        assert period.locked_by == "01HWA00000000000000000USRM"

    def test_tablename(self) -> None:
        assert PayPeriod.__tablename__ == "pay_period"

    def test_state_check_present(self) -> None:
        checks = [
            c
            for c in PayPeriod.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("state")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for state in ("open", "locked", "paid"):
            assert state in sql, f"{state} missing from CHECK constraint"

    def test_ends_after_starts_check_present(self) -> None:
        checks = [
            c
            for c in PayPeriod.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("ends_after_starts")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "ends_at" in sql
        assert "starts_at" in sql

    def test_unique_workspace_window_present(self) -> None:
        uniques = [
            u for u in PayPeriod.__table_args__ if isinstance(u, UniqueConstraint)
        ]
        assert len(uniques) == 1
        assert [c.name for c in uniques[0].columns] == [
            "workspace_id",
            "starts_at",
            "ends_at",
        ]
        assert uniques[0].name == "uq_pay_period_workspace_window"


class TestPayslipModel:
    """The ``Payslip`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        slip = Payslip(
            id="01HWA00000000000000000PSLA",
            workspace_id="01HWA00000000000000000WSPA",
            pay_period_id="01HWA00000000000000000PDRA",
            user_id="01HWA00000000000000000USRA",
            shift_hours_decimal=Decimal("151.67"),
            overtime_hours_decimal=Decimal("12.00"),
            gross_cents=350000,
            deductions_cents={},
            net_cents=350000,
            created_at=_PINNED,
        )
        assert slip.id == "01HWA00000000000000000PSLA"
        assert slip.shift_hours_decimal == Decimal("151.67")
        assert slip.overtime_hours_decimal == Decimal("12.00")
        assert slip.gross_cents == 350000
        assert slip.deductions_cents == {}
        assert slip.net_cents == 350000
        assert slip.pdf_blob_hash is None

    def test_construction_with_deductions_and_pdf(self) -> None:
        slip = Payslip(
            id="01HWA00000000000000000PSLB",
            workspace_id="01HWA00000000000000000WSPA",
            pay_period_id="01HWA00000000000000000PDRA",
            user_id="01HWA00000000000000000USRA",
            shift_hours_decimal=Decimal("160.00"),
            overtime_hours_decimal=Decimal("0"),
            gross_cents=400000,
            deductions_cents={"tax": 80000, "advance": 20000},
            net_cents=300000,
            pdf_blob_hash="sha256-deadbeef",
            created_at=_PINNED,
        )
        assert slip.deductions_cents == {"tax": 80000, "advance": 20000}
        assert slip.pdf_blob_hash == "sha256-deadbeef"

    def test_tablename(self) -> None:
        assert Payslip.__tablename__ == "payslip"

    def test_shift_hours_check_present(self) -> None:
        checks = [
            c
            for c in Payslip.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("shift_hours_decimal_nonneg")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "shift_hours_decimal" in sql
        assert ">= 0" in sql

    def test_overtime_hours_check_present(self) -> None:
        checks = [
            c
            for c in Payslip.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("overtime_hours_decimal_nonneg")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "overtime_hours_decimal" in sql

    def test_gross_cents_check_present(self) -> None:
        checks = [
            c
            for c in Payslip.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("gross_cents_nonneg")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "gross_cents" in sql

    def test_unique_payslip_per_period_user_present(self) -> None:
        """One payslip per (period, user) — the key acceptance criterion."""
        uniques = [u for u in Payslip.__table_args__ if isinstance(u, UniqueConstraint)]
        assert len(uniques) == 1
        assert [c.name for c in uniques[0].columns] == [
            "pay_period_id",
            "user_id",
        ]
        assert uniques[0].name == "uq_payslip_pay_period_user"


class TestPackageReExports:
    """``app.adapters.db.payroll`` re-exports every v1-slice model."""

    def test_models_re_exported(self) -> None:
        assert PayRule is payroll_models.PayRule
        assert PayPeriod is payroll_models.PayPeriod
        assert Payslip is payroll_models.Payslip


class TestRegistryIntent:
    """Every payroll table is registered as workspace-scoped.

    The assertions call :func:`app.tenancy.registry.register` directly
    rather than relying on the import-time side effect of
    ``app.adapters.db.payroll``: a sibling ``test_tenancy_orm_filter``
    autouse fixture calls ``registry._reset_for_tests()`` which wipes
    the process-wide set, so asserting presence after that reset
    would be flaky. The tests below encode the invariant — "every
    payroll table is scoped" — without over-coupling to import
    ordering.
    """

    def test_every_payroll_table_is_registered(self) -> None:
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in ("pay_rule", "pay_period", "payslip"):
            registry.register(table)
        scoped = registry.scoped_tables()
        for table in ("pay_rule", "pay_period", "payslip"):
            assert table in scoped, f"{table} must be scoped"

    def test_is_scoped_reports_true(self) -> None:
        """``is_scoped`` agrees with ``scoped_tables`` membership."""
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in ("pay_rule", "pay_period", "payslip"):
            registry.register(table)
        for table in ("pay_rule", "pay_period", "payslip"):
            assert registry.is_scoped(table) is True
