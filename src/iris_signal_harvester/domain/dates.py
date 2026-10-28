"""Date parsing and the ``published_at`` provenance ladder.

Real municipal data is hostile to dates. A live OParl endpoint was probed and
returns ``"date": null`` on most Paper objects and, in ``modified``, the corrupt
sentinel ``-0001-11-30T00:00:00+00:53``. Two consequences shape this module:

* The first tier of the ladder genuinely fails on real data, so the ladder is
  load-bearing: payload field, then HTTP ``Last-Modified``, then meeting date,
  then an honest ``(None, UNKNOWN)``.
* A sentinel must never be accepted as a date and must never become the maximum
  of a watermark, because that would permanently break incremental harvesting.

Error contract: everything here is *total*. The only exceptions that can escape
are ``RecordRejected`` (a record-level verdict carrying a ``RejectCode``) and,
for an unusable ``settings.source_timezone``, ``ConfigurationError``. No
``ValueError``, ``OverflowError`` or ``TypeError`` from the standard library
ever reaches a caller.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from iris_signal_harvester.config import Settings, get_settings
from iris_signal_harvester.domain.models import PublishedAtOrigin, RejectCode
from iris_signal_harvester.errors import ConfigurationError, RecordRejected

__all__ = ["parse_date", "resolve_published_at", "safe_parse_for_watermark"]

# German "TT.MM.JJJJ", optionally followed by a time of day.
_GERMAN = re.compile(
    r"^(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})"
    r"(?:[ T,]+(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?)?$"
)
# Leading year of an ISO-8601 value, including signed / extended years such as
# the sentinel's "-0001". Extracted *before* parsing, because ``datetime``
# cannot even represent year -1 or 0 and would otherwise raise a bare ValueError.
_ISO_YEAR = re.compile(r"^(?P<y>[+-]?\d{4,6})(?:-|\d{4}|$|T)")


def _reject(code: RejectCode, detail: str) -> RecordRejected:
    return RecordRejected(code.value, detail)


def _timezone(settings: Settings) -> ZoneInfo:
    try:
        return ZoneInfo(settings.source_timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ConfigurationError(
            f"source_timezone {settings.source_timezone!r} is not a known IANA zone"
        ) from exc


def _check_year(year: int, text: str, settings: Settings) -> None:
    if year < settings.min_plausible_year:
        raise _reject(
            RejectCode.DATE_OUT_OF_RANGE,
            f"year {year} in {text!r} is before min_plausible_year={settings.min_plausible_year}",
        )


def _to_utc(parsed: datetime, settings: Settings, *, naive_is_utc: bool = False) -> datetime:
    """Aware -> UTC. Naive -> localised in ``source_timezone`` first, then UTC.

    With ``naive_is_utc`` a naive value is taken to already be UTC instead. That
    is the HTTP-header tier: RFC 9110 requires an HTTP-date to be GMT, whereas
    German municipal systems write payload dates in local time.
    """
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        parsed = parsed.replace(tzinfo=UTC if naive_is_utc else _timezone(settings))
    return parsed.astimezone(UTC)


def _parse_string(text: str, settings: Settings) -> datetime:
    german = _GERMAN.match(text)
    if german:
        year = int(german["y"])
        _check_year(year, text, settings)
        return datetime(
            year,
            int(german["m"]),
            int(german["d"]),
            int(german["H"] or 0),
            int(german["M"] or 0),
            int(german["S"] or 0),
        )

    iso_year = _ISO_YEAR.match(text)
    if iso_year:
        # Judges the sentinel "-0001-11-30T00:00:00+00:53" by its year alone,
        # so it is reported as out-of-range rather than as gibberish.
        _check_year(int(iso_year["y"]), text, settings)
    return datetime.fromisoformat(text)


def parse_date(
    value: str | datetime | date,
    settings: Settings,
    *,
    now: datetime | None = None,
    naive_is_utc: bool = False,
) -> datetime:
    """Parse one date value to an aware UTC datetime, or raise ``RecordRejected``.

    Accepts German ``TT.MM.JJJJ`` (optionally with ``HH:MM[:SS]``), ISO-8601
    variants, and ``datetime``/``date`` objects. Naive values are localised with
    ``settings.source_timezone`` and converted to UTC, unless ``naive_is_utc``
    is set (HTTP-header values, which are GMT by RFC 9110).

    Raises:
        RecordRejected: ``DATE_OUT_OF_RANGE`` if the year is below
            ``settings.min_plausible_year`` or the instant is more than
            ``settings.max_future_days`` after ``now``; ``DATE_UNPARSEABLE`` for
            anything that cannot be read as a date at all.
        ConfigurationError: ``settings.source_timezone`` is not a valid zone.
    """
    reference = now if now is not None else datetime.now(UTC)
    try:
        if isinstance(value, datetime):
            _check_year(value.year, repr(value), settings)
            parsed = value
        elif isinstance(value, date):
            _check_year(value.year, repr(value), settings)
            parsed = datetime(value.year, value.month, value.day)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                raise _reject(RejectCode.DATE_UNPARSEABLE, "empty date string")
            parsed = _parse_string(text, settings)
        else:
            raise _reject(
                RejectCode.DATE_UNPARSEABLE,
                f"unsupported date type {type(value).__name__}",
            )
        result = _to_utc(parsed, settings, naive_is_utc=naive_is_utc)
    except (RecordRejected, ConfigurationError):
        raise
    except (ValueError, OverflowError, TypeError) as exc:
        raise _reject(RejectCode.DATE_UNPARSEABLE, f"cannot parse {value!r}: {exc}") from exc

    # Re-check after UTC conversion: a value near year-boundary may cross it, and
    # the future bound is only meaningful on an absolute instant.
    _check_year(result.year, repr(value), settings)
    limit = _as_utc(reference) + timedelta(days=settings.max_future_days)
    if result > limit:
        raise _reject(
            RejectCode.DATE_OUT_OF_RANGE,
            f"{result.isoformat()} is more than {settings.max_future_days} days in the future",
        )
    return result


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def resolve_published_at(
    payload_date: str | datetime | date | None,
    http_last_modified: str | datetime | date | None,
    meeting_date: str | datetime | date | None,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> tuple[datetime | None, PublishedAtOrigin]:
    """Resolve ``published_at`` and its provenance via the tier ladder.

    Tiers, most to least trustworthy: payload field, HTTP ``Last-Modified``,
    meeting date, then ``(None, UNKNOWN)``. A tier that is *absent* (``None`` or
    blank) falls through to the next. A tier that is *present but corrupt*
    raises ``RecordRejected`` instead of falling through: a record carrying a
    sentinel date is bad data, and silently substituting a lower tier's value
    would hide that from the quality report.

    Raises:
        RecordRejected: a supplied value is out of range or unparseable.
        ConfigurationError: ``settings.source_timezone`` is invalid.
    """
    tiers = (
        (payload_date, PublishedAtOrigin.PAYLOAD_FIELD),
        (http_last_modified, PublishedAtOrigin.HTTP_HEADER),
        (meeting_date, PublishedAtOrigin.DERIVED_FROM_MEETING),
    )
    for value, origin in tiers:
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        # HTTP-date is GMT (RFC 9110); payload/meeting dates are source-local.
        naive_is_utc = origin is PublishedAtOrigin.HTTP_HEADER
        return parse_date(value, settings, now=now, naive_is_utc=naive_is_utc), origin
    return None, PublishedAtOrigin.UNKNOWN


def safe_parse_for_watermark(
    value: object,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Parse a ``modified`` value for watermark use; return ``None`` if it is bad.

    Corrupt values (the ``-0001-11-30T00:00:00+00:53`` sentinel, unparseable
    text, implausible past/future dates, wrong types) are swallowed so they can
    never become the maximum of a watermark. Absent input also yields ``None``.
    """
    if value is None or not isinstance(value, str | datetime | date):
        return None
    try:
        return parse_date(value, settings or get_settings(), now=now)
    except RecordRejected:
        return None
