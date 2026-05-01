"""webauthn_challenge subject CHECK rename cd-jtrc

Revision ID: a0b2c4d6e8f1
Revises: f9c1d3e5b7a2
Create Date: 2026-05-01 08:00:00.000000

Renames the doubled-prefix CHECK constraint
``ck_webauthn_challenge_ck_webauthn_challenge_subject`` minted by the
original cd-8m4 ``webauthn_challenge`` migration (cd-jtrc) to its
canonical single-prefix form ``ck_webauthn_challenge_subject``.

The doubled name was a footgun: the model declared the constraint as
``name='ck_webauthn_challenge_subject'`` and ``NAMING_CONVENTION``'s
``ck_%(table_name)s_%(constraint_name)s`` template prepended the table
prefix again. The original migration's ``op.f(...)`` resolved through
the same convention so model and DB stayed consistent on disk — but
the next migration that tries to target this constraint by its
"obvious" name would silently miss. This migration realigns both sides
to the convention-respecting shape (model now passes ``name='subject'``).

The constraint predicate itself is unchanged: exactly one of
``user_id`` / ``signup_session_id`` MUST carry a value.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a0b2c4d6e8f1"
down_revision: str | Sequence[str] | None = "f9c1d3e5b7a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREDICATE = (
    "(user_id IS NOT NULL AND signup_session_id IS NULL) OR "
    "(user_id IS NULL AND signup_session_id IS NOT NULL)"
)
_DOUBLED_NAME = "ck_webauthn_challenge_ck_webauthn_challenge_subject"
_CANONICAL_NAME = "ck_webauthn_challenge_subject"


def upgrade() -> None:
    """Drop the doubled-prefix CHECK and re-create it with the canonical name.

    ``op.f(...)`` marks each name as fully-qualified so the shared
    ``NAMING_CONVENTION`` does not re-prepend ``ck_<table>_`` and double
    (or triple) the prefix at apply time.
    """
    with op.batch_alter_table("webauthn_challenge", schema=None) as batch_op:
        batch_op.drop_constraint(op.f(_DOUBLED_NAME), type_="check")
        batch_op.create_check_constraint(op.f(_CANONICAL_NAME), _PREDICATE)


def downgrade() -> None:
    """Restore the doubled-prefix CHECK name as the original migration emitted it."""
    with op.batch_alter_table("webauthn_challenge", schema=None) as batch_op:
        batch_op.drop_constraint(op.f(_CANONICAL_NAME), type_="check")
        batch_op.create_check_constraint(op.f(_DOUBLED_NAME), _PREDICATE)
