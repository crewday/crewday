"""Canonical PII redactor for outbound LLM payloads, logs, and exports.

This is the **single** redaction seam domain code reaches for; there
is no per-scope reimplementation. Every caller threads a ``scope``
argument so the same payload shape can be scrubbed strictly for a
log record, selectively (with consent) for an outbound LLM call, or
broadly for a user-facing export.

See ``docs/specs/15-security-privacy.md`` §"Logging and redaction",
§"Audit log", §"Privacy and data rights" and
``docs/specs/11-llm-and-agents.md`` §"Redaction layer" for the
intended surface.

Behaviour overview
------------------

1. **Key-based rules** (all scopes): any mapping key whose
   lowercased + dash→underscore-normalised name either **equals**
   one of the tokens in :data:`_SENSITIVE_KEY_TOKENS` or **ends
   with** ``_<token>`` is replaced with
   ``"<redacted:sensitive-key>"`` regardless of the value.
   ``Authorization``, ``X-API-Key`` (→ ``x_api_key``),
   ``session_id``, ``user_password`` all hit; benign lookalikes
   like ``max_tokens``, ``panel_id``, ``secretary``,
   ``token_purpose``, ``revoked_credential_count``,
   ``cookie_consent_banner_shown``, ``session_id_algorithm`` do
   not. The trailing-segment rule is deliberate: real secret
   fields are named ``cookie`` / ``set_cookie`` / ``auth_cookie``
   (the sensitive token is the TAIL of the name), while benign
   fields using the same word as a modifier prefix — "metadata
   about a credential", "UI state for a cookie banner" — use it
   at the front. This matches the observed naming convention and
   avoids redacting integer counters and enum labels whose key
   names happen to contain a sensitive word.

2. **Free-text rules** (all scopes): every string leaf walked by the
   redactor is run through a small regex pipeline that redacts
   emails, E.164 phone numbers, IBANs (validated via mod-97), PANs
   (validated via Luhn), Bearer tokens, JWTs, and long
   hex/base64url credential blobs. Each match is replaced with a
   tagged ``"<redacted:<reason>>"`` placeholder so downstream logs
   stay debuggable.

3. **Consent pass-through** (``scope="llm"`` only): if the caller
   passes a :class:`ConsentSet` that lists a field name, the
   mapping value under that *exact* key name skips the
   sensitive-key rule but **still runs through the free-text regex
   scrub**. Consent covers the field name, not its contents: a
   user who opted in to share ``legal_name`` did not implicitly
   opt in to share an embedded email / IBAN / PAN / credential
   blob, so the inner scrub stays on. Consent also never
   bypasses the sensitive-key rule outright — IBAN / PAN /
   passwords stay redacted even if the consent set contains a
   matching token.

4. **Image / binary block carve-out** (``scope="llm"`` only):
   multimodal content blocks that wrap opaque bytes (the OpenAI /
   OpenRouter vision shape
   ``{"type": "image_url", "image_url": {"url": "data:..."}}``)
   skip the free-text regex sweep on the ``url`` leaf. The base64
   payload is unstructured noise from a PII perspective and
   routinely hits the ``base64url`` credential rule, which would
   silently break every vision call. Key rules + recursion still
   apply to every other key under the block, so a stray ``email``
   next to the image bytes is still caught.

5. **Structural recursion**: :class:`dict`, :class:`list`,
   :class:`tuple`, :class:`set`, :class:`frozenset` are walked with
   their container type preserved. Non-container, non-string leaves
   (``int``, ``float``, ``bool``, ``None``) pass through unchanged.
   The walker is capped at :data:`_MAX_DEPTH` levels; deeper nodes
   are rendered via ``repr()`` and string-scrubbed so a deeply
   nested credential still cannot leak.

6. **Hash pass-through** (all scopes): string values under keys
   whose normalised name contains ``hash`` / ``hashed`` /
   ``fingerprint`` as a word component skip both the sensitive-key
   rule and the free-text regex sweep. The §15 PII-minimisation
   policy stores hashed forms in audit / magic-link flows so
   forensic lookup still works after the plaintext is purged;
   redacting the hashes would defeat the point.

7. **Purity**: the function returns a deep copy; the input is never
   mutated. Identical input always yields identical output.

The spec names ``structlog`` for the log pipeline; we satisfy the
invariants with the stdlib ``logging`` module, see
:mod:`app.util.logging`, which delegates into this module so the
pattern set lives in one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from pydantic import SecretStr

__all__ = [
    "CONSENT_TOKENS",
    "ConsentSet",
    "RedactScope",
    "redact",
    "scrub_string",
]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


RedactScope = Literal["log", "llm", "export"]


#: Consent tokens documented as valid in :class:`ConsentSet`. A consent
#: flag in this set, when ``scope="llm"``, lets the mapping value under
#: that key name skip the free-text regex scrub. Consent never
#: overrides the sensitive-key rule — see module docstring.
CONSENT_TOKENS: Final[frozenset[str]] = frozenset(
    {"legal_name", "email", "phone", "address"}
)


@dataclass(frozen=True, slots=True)
class ConsentSet:
    """Opt-in flags that let specific PII fields pass through LLMs.

    Empty (the default) means "redact everything" — the safe posture
    for callers that have not yet wired up the per-workspace consent
    column. Unknown tokens are silently ignored rather than raising,
    so a future spec addition doesn't turn a forgotten upgrade into a
    runtime error in the redaction hot path.
    """

    fields: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def none(cls) -> ConsentSet:
        """Return an empty consent set — the redact-everything default."""
        return cls()

    def allows(self, field_name: str) -> bool:
        """Return ``True`` iff ``field_name`` is in this consent set.

        Matching is exact + case-sensitive. The caller normalises
        once (lowercase) before building the set; we refuse to do it
        again on every lookup to keep the hot path cheap.
        """
        return field_name in self.fields


# ---------------------------------------------------------------------------
# Placeholder tags
# ---------------------------------------------------------------------------


_TAG_EMAIL: Final[str] = "<redacted:email>"
_TAG_PHONE: Final[str] = "<redacted:phone>"
_TAG_IBAN: Final[str] = "<redacted:iban>"
_TAG_PAN: Final[str] = "<redacted:pan>"
_TAG_CREDENTIAL: Final[str] = "<redacted:credential>"
_TAG_SENSITIVE_KEY: Final[str] = "<redacted:sensitive-key>"


# ---------------------------------------------------------------------------
# Key-based rules
# ---------------------------------------------------------------------------


#: Mapping-key tokens that mark the value as sensitive. A match on
#: the normalised key (lowercased, dash→underscore) means the *value*
#: is replaced wholesale, regardless of shape — the value could be a
#: :class:`SecretStr`, a nested dict of more secrets, or a plain
#: string; we don't peek.
#
# The match is deliberately conservative:
#
# * the normalised key EQUALS a token (e.g. ``password``,
#   ``api_key``, ``session_id``), or
# * the normalised key ENDS WITH ``_<token>`` (e.g. ``x_password``,
#   ``access_token``, ``set_cookie``, ``x_authorization``).
#
# We do NOT match mid-name occurrences. Earlier versions used a
# ``[^a-z0-9]`` word-boundary regex that treated underscores as splits
# on both sides, which false-positived on names like ``token_purpose``
# (enum label), ``revoked_credential_count`` (integer counter), and
# ``session_id_algorithm`` (config enum) — all benign, none of them
# holding a secret. The tail-anchored rule matches every real-world
# "this field holds a secret" naming pattern (``<modifier>_<token>``)
# while leaving "metadata about a <token>" names
# (``<token>_<modifier>`` / ``<token>_<noun>_<modifier>``) alone.
_SENSITIVE_KEY_TOKENS: Final[tuple[str, ...]] = (
    "password",
    "token",
    "secret",
    "cookie",
    "account_number",
    "account_number_plaintext",
    "pan",
    "iban",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "passkey",
    "session_id",
)


#: Joined regex anchored at both ends of the normalised key. The
#: alternation fires either when the whole key is a token or when it
#: ends with ``_<token>`` — see the tuple comment above for the
#: rationale and the enumerated false-positive cases that drove the
#: rule.
_SENSITIVE_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|_)(" + "|".join(_SENSITIVE_KEY_TOKENS) + r")$"
)


#: Word tokens that mark a value as already-minimised (a hash,
#: fingerprint, or derived id) and therefore safe to pass through
#: without further scrubbing. The audit log and the magic-link /
#: recovery flows deliberately store hashes instead of plaintext (§15
#: PII-minimisation), so fields like ``email_hash`` or
#: ``ip_hash_at_request`` must survive the free-text regex sweep —
#: which would otherwise redact a sha256 as a credential.
#
# Contract for fuzz / property testing: a hash-shaped string under
# any key matching :data:`_HASH_KEY_RE` is preserved verbatim by
# design. Property tests that inject arbitrary literals MUST exclude
# these key shapes from their dict-key strategy or they will
# (correctly) observe the literal surviving — see
# ``tests/property/test_redact_fuzz.py``'s ``_safe_key_strategy``.
_HASH_KEY_TOKENS: Final[tuple[str, ...]] = ("hash", "hashed", "fingerprint")


#: Joined whole-word regex over the hash tokens. Same boundary
#: mechanics as :data:`_SENSITIVE_KEY_RE` — a token hits only when
#: it's a complete word inside the normalised key. ``hash_map`` or
#: ``email_hash_at_request`` both match; ``unhashable`` does not.
_HASH_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[^a-z0-9])(" + "|".join(_HASH_KEY_TOKENS) + r")(?:[^a-z0-9]|$)"
)


def _key_is_hash(key: object) -> bool:
    """Return ``True`` if the key marks an already-minimised value.

    See :data:`_HASH_KEY_TOKENS` for the rationale. Matching is
    case-insensitive + dash→underscore normalised so both ``email-hash``
    and ``EMAIL_HASH`` hit, and tokens survive anywhere in the name
    (``ip_hash_at_request`` still hits via the middle word).
    """
    if not isinstance(key, str):
        return False
    normalised = key.lower().replace("-", "_")
    return _HASH_KEY_RE.search(normalised) is not None


#: Alphabet sets for the hash-shape sniff in :func:`_looks_like_hash`.
#: Pulled out as ``frozenset`` constants so the per-character ``in``
#: check stays O(1) and the intent ("hex digits" / "base64url alphabet,
#: no padding") is named once instead of inlined as a magic string.
_HEX_ALPHABET: Final[frozenset[str]] = frozenset("0123456789abcdefABCDEF")
_BASE64URL_ALPHABET: Final[frozenset[str]] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def _looks_like_hash(value: str) -> bool:
    """Return ``True`` if ``value`` looks like a minimised hash or fingerprint.

    Hashes stored under ``*_hash`` / ``*_hashed`` / ``*_fingerprint`` keys
    for forensic lookups are typically hex or base64url strings of length
    32+ (SHA-256 is 64, MD5 is 32, fingerprints vary). Plaintext PII
    under such keys is an error and must still be redacted.

    This check is permissive — it returns ``True`` for any string that
    could plausibly be a hash (hex or base64url digits), and ``False``
    for strings containing email addresses, phone numbers, etc. The
    caller is responsible for ensuring that genuine hashes land under
    hash-named keys.
    """
    if len(value) < 16:  # Too short to be a meaningful hash.
        return False
    # Hex: 32+ hex digits, no whitespace or special chars.
    if len(value) >= 32 and all(c in _HEX_ALPHABET for c in value):
        return True
    # Base64url: 40+ chars from the URL-safe alphabet, no padding.
    return len(value) >= 40 and all(c in _BASE64URL_ALPHABET for c in value)


def _key_is_sensitive(key: object) -> bool:
    """Return ``True`` if a mapping key should trigger wholesale redaction.

    Matching is case-insensitive + dash→underscore normalised, and
    restricted to two shapes:

    * the whole key equals a token (``password``, ``api_key``,
      ``session_id``);
    * the key ends with ``_<token>`` (``x_password``,
      ``access_token``, ``set_cookie``, ``x_authorization``).

    Mid-name and prefix-only matches deliberately do not fire, so
    benign keys like ``token_purpose`` (enum label),
    ``revoked_credential_count`` (integer counter),
    ``cookie_consent_banner_shown`` (UI flag), and
    ``session_id_algorithm`` (config enum) survive intact. See the
    :data:`_SENSITIVE_KEY_TOKENS` comment for the reasoning.

    We normalise dashes to underscores first so ``api-key`` and
    ``api_key`` collapse to the same canonical form before the
    anchored regex runs.
    """
    if not isinstance(key, str):
        return False
    normalised = key.lower().replace("-", "_")
    return _SENSITIVE_KEY_RE.search(normalised) is not None


# ---------------------------------------------------------------------------
# Free-text regex rules
# ---------------------------------------------------------------------------
#
# Order matters: credential-shaped blobs (Bearer / JWT / long hex /
# long base64url) run first so a secret that *also* matches an email
# or phone pattern is captured by the more specific rule; the IBAN
# and PAN passes include structural checks (mod-97 / Luhn) so false
# positives are rare even on cramped payloads.


# Credential patterns — moved here from ``app.util.logging`` so
# callers across scopes share a single regex set. The logging module
# re-imports the wrapper below.
_BEARER_RE: Final[re.Pattern[str]] = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+")
# JWT shape: three base64url segments separated by dots. We require at
# least one segment to be ≥ 10 chars so legitimate dotted identifiers
# (structured-log event names like ``worker.tick.start`` /
# ``ops.readyz.degraded`` / ``idempotency.sweep``, dotted module
# paths, OTel span attributes) survive — see ``cd-pzr1`` and
# ``cd-udts``.
#
# The alternation form (≥10 on ANY segment, not on EVERY segment) is
# deliberate: a uniform ``{10,}\.{10,}\.{10,}`` would under-redact
# real JWTs because some real-world headers can be short
# (``eyJ0eXAi...`` for a stripped-down ``{"typ":"JWT"}`` payload),
# while at least one of the payload / signature segments is always
# well above 10 chars. Any plausible JWT therefore lights up at
# least one alternative; any plain dotted identifier with all three
# segments under 10 chars (the operator-emitted event-marker case)
# stays untouched.
#
# Real RFC 7519 JWTs are well above the floor on the long segments:
#
# * Payload — claims push this to 50+ chars in even the smallest
#   real token.
# * Signature — HS256 yields 256 bits = 43 base64url chars; RS256 /
#   ES256 are longer.
#
# cd-5n934: keep lowercase FQDNs like ``fcm.googleapis.com`` out of
# JWT redaction. The trade-off is conservative: only lowercase LDH
# hostnames with short TLDs are spared; mixed-case JWT-like candidates
# still redact.
_JWT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[\w-]{10,}\.[\w-]+\.[\w-]+\b"
    r"|\b[\w-]+\.[\w-]{10,}\.[\w-]+\b"
    r"|\b[\w-]+\.[\w-]+\.[\w-]{10,}\b"
)
# RFC 1035 LDH label: lowercase letter or digit, optionally followed
# by letters / digits / hyphens, never starting or ending with a
# hyphen. JWT segments routinely contain uppercase letters (base64url
# alphabet) so the lowercase-only constraint discriminates host
# labels from credential blobs.
_DNS_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
# TLD-like trailing label: 2-8 lowercase letters. Covers every
# in-use gTLD / ccTLD we care about (``com``, ``org``, ``io``,
# ``day``, ``travel``, ``business``) without lifting the cap so high
# that long alpha JWT tail segments (the ``a.b.cdefghijkl`` shape
# pinned in :mod:`tests.unit.test_redact_jwt_boundary`) start
# qualifying as hostnames. Names like ``.amsterdam`` / ``.barcelona``
# (9+ chars) are intentionally excluded — false-positive risk
# on a real JWT tail outweighs the cost of redacting a hostname
# under such a TLD in a log line.
_HOSTNAME_TLD_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z]{2,8}$")


def _looks_like_hostname(candidate: str) -> bool:
    """Return ``True`` if ``candidate`` is a dotted DNS hostname.

    A hostname is two-or-more dot-separated RFC 1035 LDH labels
    whose final segment looks like a TLD. The check is conservative
    (lowercase only) so the JWT regex can run loosely and the
    carve-out drops only the real-world false-positive class —
    vendor hostnames like ``fcm.googleapis.com`` /
    ``whatever.googleusercontent.com`` whose middle label happens
    to clear the 10-char floor.
    """
    parts = candidate.split(".")
    if len(parts) < 2:
        return False
    if _HOSTNAME_TLD_RE.match(parts[-1]) is None:
        return False
    return all(_DNS_LABEL_RE.match(p) is not None for p in parts)


def _jwt_replace(match: re.Match[str]) -> str:
    candidate = match.group(0)
    return candidate if _looks_like_hostname(candidate) else _TAG_CREDENTIAL


# Hex threshold: 32+ chars. The SHA-256 convention is 64, but MD5 +
# SHA-1 + op-specific 32-char fingerprints all cluster at 32. Hashes
# stored under ``*_hash`` keys get the :func:`_key_is_hash`
# pass-through so they survive forensic use-cases.
_HEX_RE: Final[re.Pattern[str]] = re.compile(r"\b[A-Fa-f0-9]{32,}\b")
# Base64url threshold: 40+ chars. A UUID is 36 chars — we raise the
# bar above that so standard UUIDs (``00000000-0000-0000-0000-000000000000``,
# AAGUIDs, …) are not mistaken for credential blobs. Genuine
# credentials (API keys, opaque session tokens) are usually well
# above 40 chars, and any shorter ones are covered by the Bearer /
# JWT / hex rules above.
_BASE64URL_RE: Final[re.Pattern[str]] = re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")

# RFC 5322 simplified. Intentionally narrower than the full spec —
# in-the-wild addresses with quoted-local-parts and IP-literal domains
# are vanishingly rare in crew.day payloads and tightening the regex
# removes a whole class of false positives in log blobs that happen
# to contain an `@`.
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)

# E.164-ish. Up to 15 digits total (ITU-T cap), optional leading ``+``,
# first digit 1-9 (E.164 forbids leading zero on country code). Word
# boundaries keep us from chewing the middle of a longer digit run,
# e.g. a 20-digit transaction reference.
_PHONE_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)\+?[1-9]\d{7,14}(?!\d)")

# IBAN candidate shape: 2-letter country, 2 check digits, 11-30
# alphanumerics (total 15-34 chars). The mod-97 check happens in
# :func:`_iban_is_valid` — only validated candidates are redacted, so
# the pattern itself can be permissive without drowning free text in
# false positives.
_IBAN_CANDIDATE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"
)

# PAN candidate: 13-19 digits (the PCI-DSS range). Luhn verified in
# :func:`_luhn_is_valid`. We look for a run bounded by non-digits so a
# longer numeric string (order id, audit cursor) isn't chopped into a
# faux PAN.
_PAN_CANDIDATE_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)\d{13,19}(?!\d)")


# ---------------------------------------------------------------------------
# IBAN / PAN validators
# ---------------------------------------------------------------------------


def _iban_is_valid(candidate: str) -> bool:
    """Run the IBAN mod-97 check. Candidate is already uppercased.

    ISO 13616: move the leading four characters to the end, replace
    every letter with its position in the alphabet plus nine (A=10 .. Z=35),
    treat the result as a base-10 integer, check that it ``% 97 == 1``.
    Rejected candidates fall through unchanged.
    """
    if len(candidate) < 15 or len(candidate) > 34:
        return False
    rearranged = candidate[4:] + candidate[:4]
    digits: list[str] = []
    for ch in rearranged:
        if ch.isdigit():
            digits.append(ch)
        elif "A" <= ch <= "Z":
            # A=10, B=11, ..., Z=35.
            digits.append(str(ord(ch) - 55))
        else:
            return False
    try:
        return int("".join(digits)) % 97 == 1
    except ValueError:
        return False


def _luhn_is_valid(candidate: str) -> bool:
    """Return ``True`` if ``candidate`` (digits only) passes Luhn.

    Standard right-to-left walk: double every second digit, subtract
    9 when doubling overflows, sum; a total divisible by 10 passes.
    """
    total = 0
    # Walk right-to-left; every second digit gets doubled.
    for idx, ch in enumerate(reversed(candidate)):
        if not ch.isdigit():
            return False
        digit = int(ch)
        if idx % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


# ---------------------------------------------------------------------------
# String scrubber
# ---------------------------------------------------------------------------


def scrub_string(value: str) -> str:
    """Run every free-text regex over ``value`` and return the scrubbed form.

    Exposed publicly so :mod:`app.util.logging` can reuse the exact
    same pass on its formatted message text.

    Ordering rationale:

    1. **Email first.** An address like ``user@a.b.example.co`` has
       three or more dot-separated segments after the ``@`` that
       look structurally identical to a JWT; running the JWT regex
       first would blow the email away as three credential chunks.
    2. **Bearer / JWT / hex / base64url** next — credential shapes
       are narrow and unambiguous once the email-shaped string is
       out of the way. Bearer before JWT because the ``Bearer``
       prefix swallows the whole token including any embedded dots.
    3. **Phone / IBAN / PAN** last — these are digit-heavy patterns
       that run on a string already cleared of the alpha-numeric
       credential blobs above, so there's no cross-interference.
    """
    out = _EMAIL_RE.sub(_TAG_EMAIL, value)
    out = _BEARER_RE.sub(_TAG_CREDENTIAL, out)
    out = _JWT_RE.sub(_jwt_replace, out)
    out = _HEX_RE.sub(_TAG_CREDENTIAL, out)
    out = _BASE64URL_RE.sub(_TAG_CREDENTIAL, out)
    # IBAN and PAN run BEFORE phone so the digit tail of an IBAN /
    # credit-card number isn't chewed into a false E.164 match. Both
    # checks are structural (mod-97 / Luhn) so false positives stay
    # rare even on cramped payloads.
    out = _IBAN_CANDIDATE_RE.sub(_iban_replace, out)
    out = _PAN_CANDIDATE_RE.sub(_pan_replace, out)
    out = _PHONE_RE.sub(_TAG_PHONE, out)
    return out


def _iban_replace(match: re.Match[str]) -> str:
    candidate = match.group(0)
    return _TAG_IBAN if _iban_is_valid(candidate) else candidate


def _pan_replace(match: re.Match[str]) -> str:
    candidate = match.group(0)
    return _TAG_PAN if _luhn_is_valid(candidate) else candidate


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


_MAX_DEPTH: Final[int] = 8


def redact(
    payload: Any,
    *,
    scope: RedactScope,
    consents: ConsentSet | None = None,
    max_depth: int = _MAX_DEPTH,
) -> Any:
    """Return a deep copy of ``payload`` with PII redacted per ``scope``.

    Behaviour summary (full spec in the module docstring):

    * ``scope="log"`` — maximum strictness; ``consents`` is ignored.
      This is what :mod:`app.util.logging` passes so operator logs
      stay clean.

    * ``scope="llm"`` — strict by default; a non-empty ``consents``
      set lets matching *field names* skip the free-text regex pass.
      Consent does NOT bypass the sensitive-key rule (passwords /
      IBAN / PAN keys stay redacted), and it does NOT disable regex
      scrubbing *inside* the allowed value — only the direct string
      value under the allowed mapping key is preserved verbatim.

    * ``scope="export"`` — strict for cross-user safety. The
      function cannot know whose data is in the payload, so the
      caller is responsible for not feeding in another user's rows;
      this path is the regex safety net.

    ``max_depth`` caps structural recursion. Deeper nodes are
    rendered via ``repr()`` and string-scrubbed so a pathologically
    nested credential still cannot leak. The default is high enough
    for every known call site; tests exercise the fallback path.

    Returned values are fresh objects — the caller can mutate them
    without touching the input.
    """
    effective_consents = consents if scope == "llm" else None
    return _redact(
        payload, scope=scope, consents=effective_consents, depth=0, max_depth=max_depth
    )


def _redact(
    value: object,
    *,
    scope: RedactScope,
    consents: ConsentSet | None,
    depth: int,
    max_depth: int,
) -> object:
    """Internal recursive walker. See :func:`redact` for the public contract."""
    # SecretStr short-circuit: never peek at the secret value. The
    # logging filter also catches this, but callers outside logging
    # (audit writer, LLM adapter) rely on the same behaviour.
    if isinstance(value, SecretStr):
        return _TAG_SENSITIVE_KEY

    if isinstance(value, str):
        return scrub_string(value)

    # bool is a subclass of int; short-circuit before the int path.
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return value

    if depth >= max_depth:
        # Past the recursion cap, render + string-scrub. This is the
        # DoS backstop — structures deeper than ``max_depth`` stop
        # being walked so the hot path stays O(n).
        return scrub_string(repr(value))

    if isinstance(value, dict):
        return _redact_mapping(
            value, scope=scope, consents=consents, depth=depth, max_depth=max_depth
        )

    if isinstance(value, list):
        return [
            _redact(
                item,
                scope=scope,
                consents=consents,
                depth=depth + 1,
                max_depth=max_depth,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _redact(
                item,
                scope=scope,
                consents=consents,
                depth=depth + 1,
                max_depth=max_depth,
            )
            for item in value
        )
    if isinstance(value, frozenset):
        # Build as a frozenset so callers' type expectations survive.
        return frozenset(
            _redact(
                item,
                scope=scope,
                consents=consents,
                depth=depth + 1,
                max_depth=max_depth,
            )
            for item in value
        )
    if isinstance(value, set):
        return {
            _redact(
                item,
                scope=scope,
                consents=consents,
                depth=depth + 1,
                max_depth=max_depth,
            )
            for item in value
        }

    # Unknown leaf type: render safely via repr and scrub as a string.
    # The logging filter had the same fallback; callers that pass
    # ``datetime`` / ``Decimal`` / ``UUID`` would otherwise lose the
    # value completely.
    return scrub_string(repr(value))


def _redact_mapping(
    mapping: dict[object, object],
    *,
    scope: RedactScope,
    consents: ConsentSet | None,
    depth: int,
    max_depth: int,
) -> dict[object, object]:
    """Walk a ``dict``, applying key rules + recursing into values."""
    # Image / binary multimodal block carve-out. OpenAI / OpenRouter
    # vision requests arrive as ``{"type": "image_url",
    # "image_url": {"url": "data:..."}}`` — the ``url`` is a base64
    # payload that routinely matches the credential-shape regex and
    # would silently break every vision call. Detect the shape once
    # at the mapping level and skip the free-text sweep on the URL
    # leaf while still running every other key through the regular
    # rules (so a stray PII field next to the bytes is still caught).
    is_image_block = _looks_like_image_block(mapping)

    redacted: dict[object, object] = {}
    for key, raw in mapping.items():
        # Hash / fingerprint pass-through: values under keys like
        # ``email_hash`` are already minimised forms (§15 PII
        # minimisation) and the magic-link / recovery audit flows
        # rely on them surviving for forensic lookup. Strings under
        # such keys skip both the sensitive-key rule and the
        # free-text regex sweep — **but only if they look like
        # hashes** (hex or base64url of sufficient length).
        # Plaintext PII under a hash key is an error and is redacted
        # normally. Structural values still walk so a hash sub-tree
        # with a nested plaintext leak is caught.
        if _key_is_hash(key) and isinstance(raw, str) and _looks_like_hash(raw):
            redacted[key] = raw
            continue

        # Image-block carve-out: preserve the bytes wrapper verbatim.
        # The outer ``type`` discriminator also survives so the
        # provider can still route the block. Structural children
        # (``image_url`` sub-dict) carry the opaque URL we want to
        # keep, so we recurse without regex-scrubbing its leaf.
        # ``type``, ``text``, and any other string siblings fall
        # through to the normal rules — a prompt carrying a Bearer
        # token right next to the image should still be caught.
        if (
            is_image_block
            and isinstance(key, str)
            and key == "image_url"
            and isinstance(raw, dict)
        ):
            redacted[key] = _redact_image_url_block(
                raw,
                scope=scope,
                consents=consents,
                depth=depth + 1,
                max_depth=max_depth,
            )
            continue

        if _key_is_sensitive(key):
            # Sensitive-key rule wins over consent; spec is explicit.
            redacted[key] = _TAG_SENSITIVE_KEY
            continue

        # Consent pass-through (``scope="llm"`` only): the field name
        # is allowed, but the *contents* are still run through the
        # free-text regex scrub. Consent for ``legal_name`` does NOT
        # imply consent for an embedded email / IBAN / PAN — those
        # were not what the user opted in to share. The scrub is a
        # no-op on content that has no PII shape (a plain name), so
        # benign cases pay nothing; malicious / accidental leaks are
        # still caught. Structural values (nested dict / list) walk
        # recursively under consent for the same reason.
        if (
            scope == "llm"
            and consents is not None
            and isinstance(key, str)
            and consents.allows(key)
            and isinstance(raw, str)
        ):
            redacted[key] = scrub_string(raw)
            continue

        redacted[key] = _redact(
            raw, scope=scope, consents=consents, depth=depth + 1, max_depth=max_depth
        )
    return redacted


def _looks_like_image_block(mapping: dict[object, object]) -> bool:
    """Return ``True`` if ``mapping`` is a multimodal image block.

    Matches the OpenAI / OpenRouter vision shape exactly —
    ``{"type": "image_url", ...}`` (URL / data-URL form) or
    ``{"type": "image", ...}`` (Anthropic shape we accept for
    robustness even though the adapter only emits the former). The
    check is intentionally tight so a domain dict that happens to
    carry a ``type`` field doesn't accidentally skip regex scrubbing.
    """
    type_value = mapping.get("type")
    return type_value in ("image_url", "image")


def _redact_image_url_block(
    block: dict[object, object],
    *,
    scope: RedactScope,
    consents: ConsentSet | None,
    depth: int,
    max_depth: int,
) -> dict[object, object]:
    """Return a shallow copy of an ``image_url`` sub-dict, URL preserved.

    The ``url`` leaf carries the opaque ``data:<mime>;base64,<payload>``
    wrapper; we keep it verbatim because scrubbing it as a credential
    would break every vision call. Other keys under the block (a
    future ``detail``, ``size``, …) run through the regular redactor
    so no latent PII sneaks past the carve-out.
    """
    out: dict[object, object] = {}
    for key, raw in block.items():
        if key == "url" and isinstance(raw, str):
            out[key] = raw
            continue
        # Any other key stays under the normal rule set — we thread
        # the live depth / scope / consents through so a deep
        # pathological block still respects the recursion cap.
        out[key] = _redact(
            raw,
            scope=scope,
            consents=consents,
            depth=depth + 1,
            max_depth=max_depth,
        )
    return out
