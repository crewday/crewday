"""Integration tests for the §15 ``secret_envelope`` row-backed path.

Exercises the cd-znv4 cipher + repository against a real SQLAlchemy
session bound to the test backend (SQLite by default; PG when
``CREWDAY_TEST_DB=postgres``):

* Row-backed encrypt persists a ``secret_envelope`` row and returns
  a tiny pointer-tagged blob (``0x02 || envelope_id``).
* Row-backed decrypt resolves the row, checks the §15 ``key_fp``,
  and recovers the plaintext.
* Mixed-version decrypt: a row-backed cipher opens both new
  ``0x02`` and legacy ``0x01`` ciphertexts (the cd-1ai
  forward-compat contract §15 pins).
* Key-fingerprint mismatch raises
  :class:`KeyFingerprintMismatch` with the §15 actionable message
  shape when the operator restores under the wrong root key.
* iCal feed migration (cd-1ai consumer): ``register_feed`` lands a
  row-backed ``0x02`` blob in ``ical_feed.url``; the matching
  ``secret_envelope`` row carries ``owner_entity_kind='ical_feed'``.

The schema test is implicit: the cd-znv4 migration ran via
``migrate_once``; if the table doesn't exist the insert below
fails loudly with an :class:`OperationalError`.

See ``docs/specs/15-security-privacy.md`` §"Secret envelope" /
§"Key fingerprint" / §"Forward compatibility".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.secrets.models import SecretEnvelope
from app.adapters.db.secrets.repositories import (
    SqlAlchemySecretEnvelopeRepository,
)
from app.adapters.db.stays.models import IcalFeed
from app.adapters.ical.providers import HostProviderDetector
from app.adapters.storage.envelope import (
    Aes256GcmEnvelope,
    compute_key_fingerprint,
)
from app.adapters.storage.ports import (
    EnvelopeOwner,
    KeyFingerprintMismatch,
)
from app.domain.stays.ical_service import (
    IcalFeedCreate,
    IcalFeedUpdate,
    get_plaintext_url,
    register_feed,
    update_feed,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_KEY = SecretStr("x" * 32)
_OTHER_KEY = SecretStr("y" * 32)
_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Cipher round-trip via the SA-backed repository
# ---------------------------------------------------------------------------


class TestRowBackedRoundTripAgainstSqlAlchemy:
    def test_encrypt_persists_row_and_decrypt_recovers(
        self, db_session: Session
    ) -> None:
        repo = SqlAlchemySecretEnvelopeRepository(db_session)
        env = Aes256GcmEnvelope(_KEY, repository=repo, clock=FrozenClock(_PINNED))
        owner = EnvelopeOwner(kind="ical_feed", id="feed-001")
        plaintext = b"https://www.airbnb.com/ical/secret-token.ics"

        ciphertext = env.encrypt(plaintext, purpose="ical-feed-url", owner=owner)

        # Pointer-tagged shape.
        assert ciphertext[0] == 0x02
        envelope_id = ciphertext[1:].decode("utf-8")

        # Row landed in the DB.
        row = db_session.get(SecretEnvelope, envelope_id)
        assert row is not None
        assert row.owner_entity_kind == "ical_feed"
        assert row.owner_entity_id == "feed-001"
        assert row.purpose == "ical-feed-url"
        assert bytes(row.key_fp) == compute_key_fingerprint(_KEY)
        assert len(bytes(row.key_fp)) == 8
        # Ciphertext + nonce are opaque bytes; no plaintext leakage.
        assert plaintext not in bytes(row.ciphertext)

        # Decrypt round-trips.
        assert env.decrypt(ciphertext, purpose="ical-feed-url") == plaintext

    def test_mixed_version_decrypt(self, db_session: Session) -> None:
        """A row-backed cipher opens both legacy and fresh ciphertexts."""
        repo = SqlAlchemySecretEnvelopeRepository(db_session)
        env = Aes256GcmEnvelope(_KEY, repository=repo, clock=FrozenClock(_PINNED))
        owner = EnvelopeOwner(kind="ical_feed", id="feed-mixed")

        # Encrypt one secret in legacy inline mode (no repo on the
        # encryptor) — simulates a row that landed under cd-1ai
        # before this revision.
        legacy_env = Aes256GcmEnvelope(_KEY)
        legacy_blob = legacy_env.encrypt(b"legacy-url", purpose="ical-feed-url")
        assert legacy_blob[0] == 0x01

        # Encrypt another via the row-backed path.
        new_blob = env.encrypt(b"fresh-url", purpose="ical-feed-url", owner=owner)
        assert new_blob[0] == 0x02

        # The same row-backed cipher decrypts both.
        assert env.decrypt(legacy_blob, purpose="ical-feed-url") == b"legacy-url"
        assert env.decrypt(new_blob, purpose="ical-feed-url") == b"fresh-url"

    def test_key_fingerprint_mismatch_raises(self, db_session: Session) -> None:
        """A row encrypted under key A cannot decrypt under key B."""
        repo = SqlAlchemySecretEnvelopeRepository(db_session)
        encrypt_env = Aes256GcmEnvelope(
            _KEY, repository=repo, clock=FrozenClock(_PINNED)
        )
        owner = EnvelopeOwner(kind="ical_feed", id="feed-mismatch")
        ciphertext = encrypt_env.encrypt(
            b"secret", purpose="ical-feed-url", owner=owner
        )

        # Operator restored under the wrong key — same DB.
        decrypt_env = Aes256GcmEnvelope(
            _OTHER_KEY, repository=repo, clock=FrozenClock(_PINNED)
        )
        with pytest.raises(KeyFingerprintMismatch) as exc_info:
            decrypt_env.decrypt(ciphertext, purpose="ical-feed-url")

        # The §15 actionable message shape names both fingerprints.
        message = str(exc_info.value)
        assert compute_key_fingerprint(_KEY).hex() in message
        assert compute_key_fingerprint(_OTHER_KEY).hex() in message
        assert "Restore the correct key or re-encrypt." in message


# ---------------------------------------------------------------------------
# iCal feed migration — cd-1ai consumer rewires onto cd-znv4
# ---------------------------------------------------------------------------


def _ctx(workspace_id: str, slug: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id="01HWA00000000000000000USR1",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


def _bootstrap(db_session: Session) -> tuple[str, str, WorkspaceContext]:
    from app.adapters.db.places.models import Property
    from app.adapters.db.workspace.models import Workspace

    ws_id = new_ulid()
    db_session.add(
        Workspace(
            id=ws_id,
            slug="it-secrets",
            name="It Secrets",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    prop_id = new_ulid()
    db_session.add(
        Property(
            id=prop_id,
            name="Villa Secrets",
            kind="str",
            address="12 Chemin des Secrets",
            address_json={"country": "FR"},
            country="FR",
            locale=None,
            default_currency=None,
            timezone="Europe/Paris",
            lat=None,
            lon=None,
            client_org_id=None,
            owner_user_id=None,
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
            deleted_at=None,
        )
    )
    db_session.flush()
    return ws_id, prop_id, _ctx(ws_id, "it-secrets")


class _StubValidator:
    """Skip the SSRF / fetch / sniff dance — the URL is canonicalised through.

    Returns a fully-populated :class:`IcalValidation` so the service
    treats the probe as parseable and flips ``enabled=True``.
    """

    def validate(self, url: str):  # type: ignore[no-untyped-def]
        from app.adapters.ical.ports import IcalValidation

        return IcalValidation(
            url=url,
            resolved_ip="93.184.216.34",
            content_type="text/calendar",
            parseable_ics=True,
            bytes_read=42,
        )


class TestIcalFeedMigration:
    def test_register_feed_writes_row_backed_blob_and_secret_envelope_row(
        self,
        db_session: Session,
    ) -> None:
        """cd-znv4 acceptance: ``register_feed`` lands a ``0x02`` blob.

        The matching ``secret_envelope`` row carries
        ``owner_entity_kind='ical_feed'`` so the rotation worker can
        scope a re-encrypt sweep to "every secret for this feed".
        """
        _ws, prop_id, ctx = _bootstrap(db_session)
        repo = SqlAlchemySecretEnvelopeRepository(db_session)
        envelope = Aes256GcmEnvelope(_KEY, repository=repo, clock=FrozenClock(_PINNED))

        url = "https://feeds.example.com/cal.ics?token=secret123"
        view = register_feed(
            db_session,
            ctx,
            body=IcalFeedCreate(property_id=prop_id, url=url),
            validator=_StubValidator(),
            detector=HostProviderDetector(),
            envelope=envelope,
            clock=FrozenClock(_PINNED),
        )

        # Stored URL is a row-backed pointer-tagged blob.
        feed = db_session.get(IcalFeed, view.id)
        assert feed is not None
        stored_blob = feed.url.encode("latin-1")
        assert stored_blob[0] == 0x02

        # Matching secret_envelope row landed.
        envelope_id = stored_blob[1:].decode("utf-8")
        secret_row = db_session.get(SecretEnvelope, envelope_id)
        assert secret_row is not None
        assert secret_row.owner_entity_kind == "ical_feed"
        assert secret_row.owner_entity_id == view.id
        assert secret_row.purpose == "ical-feed-url"
        assert bytes(secret_row.key_fp) == compute_key_fingerprint(_KEY)

        # Plaintext recovery still works.
        plain = get_plaintext_url(db_session, ctx, feed_id=view.id, envelope=envelope)
        assert plain == url

    def test_legacy_inline_url_still_decrypts_after_migration(
        self, db_session: Session
    ) -> None:
        """A pre-cd-znv4 ``0x01`` ciphertext stored in ``ical_feed.url``.

        Decrypt path on the row-backed cipher MUST keep opening the
        legacy format — that's the §15 forward-compatibility
        contract the cipher's docstring promised.
        """
        _ws, prop_id, ctx = _bootstrap(db_session)

        # Hand-craft a feed row whose ``url`` carries a legacy 0x01
        # ciphertext (the cd-1ai layout).
        legacy_env = Aes256GcmEnvelope(_KEY)
        legacy_url = "https://feeds.example.com/old.ics"
        legacy_ct = legacy_env.encrypt(
            legacy_url.encode("utf-8"), purpose="ical-feed-url"
        )
        assert legacy_ct[0] == 0x01

        feed_id = new_ulid()
        db_session.add(
            IcalFeed(
                id=feed_id,
                workspace_id=ctx.workspace_id,
                property_id=prop_id,
                unit_id=None,
                url=legacy_ct.decode("latin-1"),
                provider="generic",
                poll_cadence="*/15 * * * *",
                last_polled_at=_PINNED,
                last_etag=None,
                last_error=None,
                enabled=True,
                created_at=_PINNED,
            )
        )
        db_session.flush()

        # Row-backed cipher (the production wiring after cd-znv4)
        # opens the legacy ciphertext.
        repo = SqlAlchemySecretEnvelopeRepository(db_session)
        new_env = Aes256GcmEnvelope(_KEY, repository=repo, clock=FrozenClock(_PINNED))
        plain = get_plaintext_url(db_session, ctx, feed_id=feed_id, envelope=new_env)
        assert plain == legacy_url

        # No ``secret_envelope`` row was created for the legacy feed.
        rows = list(
            db_session.scalars(
                select(SecretEnvelope).where(SecretEnvelope.owner_entity_id == feed_id)
            )
        )
        assert rows == []

    def test_update_feed_migrates_legacy_inline_blob_to_row_backed(
        self, db_session: Session
    ) -> None:
        """A feed seeded with a legacy ``0x01`` blob upgrades to ``0x02`` on URL update.

        Closes the migration loop the rotation worker (cd-19jy) will
        rely on: any operator who edits an old feed's URL ends up with
        a row-backed envelope on the next save, and the legacy blob is
        replaced rather than left to coexist on the row. Same owner —
        the new ``secret_envelope`` row carries
        ``owner_entity_id == feed_id``.
        """
        _ws, prop_id, ctx = _bootstrap(db_session)

        # Seed a feed row with a legacy 0x01 inline ciphertext.
        legacy_env = Aes256GcmEnvelope(_KEY)
        legacy_url = "https://feeds.example.com/legacy.ics?token=old"
        legacy_ct = legacy_env.encrypt(
            legacy_url.encode("utf-8"), purpose="ical-feed-url"
        )
        assert legacy_ct[0] == 0x01

        feed_id = new_ulid()
        db_session.add(
            IcalFeed(
                id=feed_id,
                workspace_id=ctx.workspace_id,
                property_id=prop_id,
                unit_id=None,
                url=legacy_ct.decode("latin-1"),
                provider="generic",
                poll_cadence="*/15 * * * *",
                last_polled_at=_PINNED,
                last_etag=None,
                last_error=None,
                enabled=True,
                created_at=_PINNED,
            )
        )
        db_session.flush()

        # Operator edits the feed under the new (row-backed) cipher
        # wiring — the `update_feed` callsite that lives on the same
        # owner re-encrypts onto the 0x02 path.
        repo = SqlAlchemySecretEnvelopeRepository(db_session)
        new_env = Aes256GcmEnvelope(_KEY, repository=repo, clock=FrozenClock(_PINNED))
        new_url = "https://feeds.example.com/legacy.ics?token=new"

        update_feed(
            db_session,
            ctx,
            feed_id=feed_id,
            body=IcalFeedUpdate(url=new_url),
            validator=_StubValidator(),
            detector=HostProviderDetector(),
            envelope=new_env,
            clock=FrozenClock(_PINNED),
        )

        # The stored URL is now a 0x02 pointer.
        feed = db_session.get(IcalFeed, feed_id)
        assert feed is not None
        stored_blob = feed.url.encode("latin-1")
        assert stored_blob[0] == 0x02

        # And a matching secret_envelope row exists, owned by this feed.
        envelope_id = stored_blob[1:].decode("utf-8")
        secret_row = db_session.get(SecretEnvelope, envelope_id)
        assert secret_row is not None
        assert secret_row.owner_entity_kind == "ical_feed"
        assert secret_row.owner_entity_id == feed_id
        assert bytes(secret_row.key_fp) == compute_key_fingerprint(_KEY)

        # Plaintext recovery returns the new URL.
        plain = get_plaintext_url(db_session, ctx, feed_id=feed_id, envelope=new_env)
        assert plain == new_url
