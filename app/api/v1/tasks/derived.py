"""Derived task fields and recurrence labels."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from app.domain.tasks.oneoff import TaskView

_TERMINAL_STATES: frozenset[str] = frozenset({"completed", "skipped", "cancelled"})


def _aware_utc(value: datetime | None) -> datetime | None:
    """Coerce SQLite-naive UTC datetimes back to aware UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _compute_overdue(view: TaskView, now_utc: datetime) -> bool:
    """``True`` when the task is past its UTC anchor + not terminal.

    The §06 sweeper (:mod:`app.worker.tasks.overdue`) is the canonical
    writer of the soft state: it flips ``state='overdue'`` and stamps
    ``overdue_since`` once ``ends_at + grace`` is past. The column
    therefore takes priority — when the sweeper has visited the row
    we trust its verdict regardless of the comparison below. For
    rows the sweeper hasn't reached yet (between the slip and the
    next 5-minute tick), fall back to the time-derived projection
    so the manager surface doesn't show a stale "on time" chip until
    the sweeper catches up.
    """
    if view.state in _TERMINAL_STATES:
        return False
    # Column wins when present — the sweeper has already decided this
    # row is overdue. The state itself is also ``'overdue'`` in that
    # case (the sweeper writes both fields atomically) but the column
    # check is the explicit signal; checking it first lets a future
    # divergence (e.g. a manual ``revert_overdue`` that cleared the
    # column without flipping ``state`` for some reason) lean on the
    # column rather than a stale state name.
    if view.overdue_since is not None or view.state == "overdue":
        return True
    anchor = view.scheduled_for_utc
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=ZoneInfo("UTC"))
    return anchor < now_utc


def _compute_time_window_local(
    view: TaskView, property_timezone: str | None
) -> str | None:
    """Render ``HH:MM-HH:MM`` in the property's timezone.

    Returns ``None`` for workspace-scoped (personal) tasks without a
    property, or when the zone is unknown / junk. The window width
    falls back to 30 minutes when ``duration_minutes`` is ``NULL`` —
    matching the :func:`create_oneoff` default so the UI never shows
    a zero-minute window.
    """
    if property_timezone is None:
        return None
    try:
        zone = ZoneInfo(property_timezone)
    except ZoneInfoNotFoundError, ValueError:
        return None
    anchor = view.scheduled_for_utc
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=ZoneInfo("UTC"))
    local_start = anchor.astimezone(zone)
    duration = view.duration_minutes if view.duration_minutes is not None else 30
    # ``datetime + timedelta`` keeps the zone; the minutes come out in
    # the property frame.
    local_end = local_start + timedelta(minutes=duration)
    return f"{local_start.strftime('%H:%M')}-{local_end.strftime('%H:%M')}"


# Weekday names indexed by ``dateutil.rrule._byweekday`` (Mon=0..Sun=6).
# Short forms — three letters — match the manager-Schedules mock copy
# ("Weekly on Mon, Thu at 10:30"). The English week starts on Monday to
# line up with ISO 8601 + the dateutil convention.
_WEEKDAY_SHORT: tuple[str, ...] = (
    "Mon",
    "Tue",
    "Wed",
    "Thu",
    "Fri",
    "Sat",
    "Sun",
)
_WEEKDAY_LONG: tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_WEEKDAYS_MON_FRI: frozenset[int] = frozenset({0, 1, 2, 3, 4})
_WEEKDAYS_SAT_SUN: frozenset[int] = frozenset({5, 6})

# Stable Monday used as a sentinel ``dtstart`` when the schedule row
# has no parseable ``dtstart_local``. Picking a fixed date (rather
# than ``datetime.now()``, which dateutil falls back to) keeps the
# rendered label deterministic — a crucial property for cache keys
# and for screenshot tests, even if the real-world write path always
# supplies a dtstart.
_SENTINEL_ANCHOR = datetime(1970, 1, 5)


def _rrule_has_clause(rrule_text: str, clause: str) -> bool:
    """Return ``True`` iff ``rrule_text`` carries an explicit ``CLAUSE=``.

    Used to distinguish a value the source line actually set from one
    dateutil derived from ``dtstart``. Matching is on the raw RRULE
    text rather than the parsed object, since the parsed object
    doesn't expose provenance.
    """
    needle = f"{clause}="
    # Case-insensitive: RFC-5545 keywords are case-insensitive, but
    # crew.day always emits uppercase. Normalising once is cheap and
    # spares us a regex.
    return needle in rrule_text.upper()


def _humanize_rrule(rrule_text: str, dtstart_local: str) -> str:
    """Return a short English summary of an RRULE + DTSTART pair.

    Examples (every shape exercised in the unit tests):

    * ``RRULE:FREQ=DAILY`` + ``2026-04-20T09:00`` → ``"Every day at 09:00"``
    * ``RRULE:FREQ=WEEKLY;BYDAY=SA`` + ``…T09:00`` →
      ``"Every Saturday at 09:00"``
    * ``RRULE:FREQ=WEEKLY;BYDAY=MO,TH`` + ``…T10:30`` →
      ``"Weekly on Mon, Thu at 10:30"``
    * ``RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR`` + ``…T07:00`` →
      ``"Weekdays at 07:00"``
    * ``RRULE:FREQ=WEEKLY;BYDAY=SA,SU`` + ``…T11:00`` →
      ``"Weekends at 11:00"``
    * ``RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO`` →
      ``"Every 2 weeks on Mon at HH:MM"``
    * ``RRULE:FREQ=MONTHLY`` → ``"Monthly at HH:MM"``
    * ``RRULE:FREQ=YEARLY`` → ``"Yearly at HH:MM"``

    Anything the parser can't reason about — a tampered body, an
    HOURLY / MINUTELY frequency, an unrecognised attribute combo —
    collapses to ``"Custom recurrence"`` rather than raising. The
    schedule's RRULE has already been validated by the schedules
    domain at write time, so a parse failure here would be a stored-
    row regression; surfacing a friendly fallback keeps the listing
    endpoint responsive while the underlying issue is investigated.

    Determinism note: ``dateutil.rrule`` defaults a missing
    ``dtstart`` to ``datetime.now()`` and back-fills ``_byweekday`` /
    ``_bymonthday`` from it. To keep the label stable for stored
    rows that somehow lost their ``dtstart_local`` (a write-time
    bug, but we must not render a label that drifts day-to-day),
    we only consult those parsed values when the BYDAY / BYMONTHDAY
    clause was explicit in the source text.
    """
    anchor = _parse_local_anchor(dtstart_local)
    time_label = anchor.strftime("%H:%M") if anchor is not None else None
    # Sentinel anchor when dtstart_local is missing/unparseable: a
    # fixed Monday so dateutil's parse succeeds without leaking
    # ``datetime.now()`` into the byweekday / bymonthday tuples we
    # surface below.
    parse_anchor = anchor if anchor is not None else _SENTINEL_ANCHOR
    try:
        rule = rrulestr(rrule_text, dtstart=parse_anchor)
    except ValueError, TypeError:
        return "Custom recurrence"

    has_byday = _rrule_has_clause(rrule_text, "BYDAY")
    has_bymonthday = _rrule_has_clause(rrule_text, "BYMONTHDAY")
    use_dtstart_derived = anchor is not None

    freq: int | None = getattr(rule, "_freq", None)
    interval: int = getattr(rule, "_interval", 1) or 1
    byweekday_raw: tuple[int, ...] | None = getattr(rule, "_byweekday", None)
    bymonthday_raw: tuple[int, ...] | None = getattr(rule, "_bymonthday", None)
    byweekday = (
        tuple(byweekday_raw)
        if byweekday_raw and (has_byday or use_dtstart_derived)
        else ()
    )
    bymonthday = (
        tuple(bymonthday_raw)
        if bymonthday_raw and (has_bymonthday or use_dtstart_derived)
        else ()
    )
    suffix = f" at {time_label}" if time_label is not None else ""

    # ``dateutil.rrule`` exposes the FREQ ints under module constants
    # (``DAILY = 3``, ``WEEKLY = 2``, ``MONTHLY = 1``, ``YEARLY = 0``,
    # ``HOURLY = 4``, ``MINUTELY = 5``, ``SECONDLY = 6``). Comparing
    # against the ints keeps this helper free of a runtime import-cycle
    # with the constants module — and matches how dateutil documents
    # the attribute (``_freq`` is a public-by-convention int).
    if freq == 3:  # DAILY
        if interval == 1:
            return f"Every day{suffix}"
        return f"Every {interval} days{suffix}"
    if freq == 2:  # WEEKLY
        return _humanize_weekly(byweekday, interval, suffix)
    if freq == 1:  # MONTHLY
        return _humanize_monthly(bymonthday, interval, suffix)
    if freq == 0:  # YEARLY
        if interval == 1:
            return f"Yearly{suffix}"
        return f"Every {interval} years{suffix}"

    return "Custom recurrence"


def _parse_local_anchor(dtstart_local: str) -> datetime | None:
    """Parse ``dtstart_local`` into a naive datetime, ``None`` on failure.

    Mirrors :func:`app.domain.tasks.schedules._parse_local_datetime`
    but swallows parse errors — the rrule humanizer is best-effort and
    must not raise out of the response projection. A tz-aware suffix
    is dropped (the column contract is naive); a malformed body
    returns ``None`` and the caller renders the recurrence without a
    time component.
    """
    text = dtstart_local.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _humanize_weekly(byweekday: tuple[int, ...], interval: int, suffix: str) -> str:
    """Render the WEEKLY branch of :func:`_humanize_rrule`.

    Single weekday → ``"Every Monday at HH:MM"``;
    ``MO,TU,WE,TH,FR`` → ``"Weekdays at HH:MM"``;
    ``SA,SU`` → ``"Weekends at HH:MM"``;
    other multi-day sets → ``"Weekly on Mon, Thu at HH:MM"``.
    Interval > 1 forces the explicit ``"Every N weeks on …"`` form so
    the cadence is unambiguous.
    """
    if not byweekday:
        # Defensive: dateutil populates ``_byweekday`` from dtstart for
        # a bare ``FREQ=WEEKLY``, so this branch is unreachable in
        # practice. Surface a sensible fallback rather than the empty
        # string a join would produce.
        return f"Weekly{suffix}"

    days_set = frozenset(byweekday)
    sorted_days = sorted(days_set)

    if interval > 1:
        joined = ", ".join(_WEEKDAY_SHORT[d] for d in sorted_days)
        return f"Every {interval} weeks on {joined}{suffix}"

    if len(sorted_days) == 1:
        return f"Every {_WEEKDAY_LONG[sorted_days[0]]}{suffix}"
    if days_set == _WEEKDAYS_MON_FRI:
        return f"Weekdays{suffix}"
    if days_set == _WEEKDAYS_SAT_SUN:
        return f"Weekends{suffix}"
    joined = ", ".join(_WEEKDAY_SHORT[d] for d in sorted_days)
    return f"Weekly on {joined}{suffix}"


def _humanize_monthly(bymonthday: tuple[int, ...], interval: int, suffix: str) -> str:
    """Render the MONTHLY branch of :func:`_humanize_rrule`.

    A single BYMONTHDAY → ``"Monthly on the Nth at HH:MM"``; multiple
    BYMONTHDAYs collapse to ``"Monthly on days 1, 15 at HH:MM"`` so
    the label stays readable. Interval > 1 forces ``"Every N months
    …"``. Without BYMONTHDAY (anchored only on dtstart) we render the
    plain ``"Monthly at HH:MM"`` shape — the day is implicit in the
    next-occurrences preview.
    """
    every = "Every" if interval == 1 else f"Every {interval}"
    unit = "month" if interval == 1 else "months"
    days = sorted({d for d in bymonthday if d > 0})

    if not days:
        if interval == 1:
            return f"Monthly{suffix}"
        return f"{every} {unit}{suffix}"
    if len(days) == 1:
        ordinal = _ordinal(days[0])
        if interval == 1:
            return f"Monthly on the {ordinal}{suffix}"
        return f"{every} {unit} on the {ordinal}{suffix}"
    joined = ", ".join(str(d) for d in days)
    if interval == 1:
        return f"Monthly on days {joined}{suffix}"
    return f"{every} {unit} on days {joined}{suffix}"


def _ordinal(day: int) -> str:
    """Return ``"1st"`` / ``"2nd"`` / ``"3rd"`` / ``"Nth"`` for a 1..31 day.

    Matches the English-language convention the SPA renders elsewhere
    on the manager surface; the helper is private because it only
    services the monthly recurrence label.
    """
    if 11 <= day % 100 <= 13:
        return f"{day}th"
    suffix_by_last = {1: "st", 2: "nd", 3: "rd"}
    return f"{day}{suffix_by_last.get(day % 10, 'th')}"
