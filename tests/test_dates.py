"""Date parsing and the published_at provenance ladder.

The real OParl sentinel ``-0001-11-30T00:00:00+00:53`` is exercised against both
``resolve_published_at`` (must reject cleanly) and ``safe_parse_for_watermark``
(must swallow and return None).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from iris_signal_harvester.config import Settings
from iris_signal_harvester.domain.dates import (
    parse_date,
    resolve_published_at,
    safe_parse_for_watermark,
)
from iris_signal_harvester.domain.models import PublishedAtOrigin, RejectCode
from iris_signal_harvester.errors import ConfigurationError, RecordRejected

SENTINEL = "-0001-11-30T00:00:00+00:53"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    # Explicit values and no .env: the tests must not depend on the environment.
    return Settings(
        _env_file=None,
        min_plausible_year=1990,
        max_future_days=400,
        source_timezone="Europe/Berlin",
    )


def _rejected_code(exc_info: pytest.ExceptionInfo[RecordRejected]) -> str:
    return exc_info.value.reason_code


# --- parsing ---------------------------------------------------------------


def test_german_date_is_localised_then_converted_to_utc(settings: Settings) -> None:
    # 05.03.2024 00:00 Berlin (CET, UTC+1) -> 04.03.2024 23:00 UTC
    result = parse_date("05.03.2024", settings, now=NOW)
    assert result == datetime(2024, 3, 4, 23, 0, tzinfo=UTC)
    assert result.utcoffset() == timedelta(0)


def test_german_date_in_summer_uses_dst_offset(settings: Settings) -> None:
    # CEST is UTC+2
    assert parse_date("15.07.2024", settings, now=NOW) == datetime(2024, 7, 14, 22, 0, tzinfo=UTC)


def test_german_date_with_time(settings: Settings) -> None:
    assert parse_date("05.03.2024 14:30", settings, now=NOW) == datetime(
        2024, 3, 5, 13, 30, tzinfo=UTC
    )


def test_german_single_digit_day_and_month(settings: Settings) -> None:
    assert parse_date("5.3.2024", settings, now=NOW) == parse_date("05.03.2024", settings, now=NOW)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2024-03-05T10:00:00Z", datetime(2024, 3, 5, 10, 0, tzinfo=UTC)),
        ("2024-03-05T10:00:00+00:00", datetime(2024, 3, 5, 10, 0, tzinfo=UTC)),
        ("2024-03-05T10:00:00+01:00", datetime(2024, 3, 5, 9, 0, tzinfo=UTC)),
        ("2024-03-05T10:00:00.123456+02:00", datetime(2024, 3, 5, 8, 0, 0, 123456, tzinfo=UTC)),
        # naive ISO is localised in Europe/Berlin (UTC+1 in March)
        ("2024-03-05T10:00:00", datetime(2024, 3, 5, 9, 0, tzinfo=UTC)),
        ("2024-03-05", datetime(2024, 3, 4, 23, 0, tzinfo=UTC)),
        ("  2024-03-05T10:00:00Z  ", datetime(2024, 3, 5, 10, 0, tzinfo=UTC)),
    ],
)
def test_iso_variants(settings: Settings, text: str, expected: datetime) -> None:
    assert parse_date(text, settings, now=NOW) == expected


def test_datetime_and_date_objects(settings: Settings) -> None:
    aware = datetime(2024, 3, 5, 10, 0, tzinfo=UTC)
    assert parse_date(aware, settings, now=NOW) == aware
    assert parse_date(datetime(2024, 3, 5, 10, 0), settings, now=NOW) == datetime(
        2024, 3, 5, 9, 0, tzinfo=UTC
    )
    assert parse_date(date(2024, 3, 5), settings, now=NOW) == datetime(
        2024, 3, 4, 23, 0, tzinfo=UTC
    )


def test_result_is_always_utc(settings: Settings) -> None:
    for text in ("05.03.2024", "2024-03-05T10:00:00+05:30", "2024-03-05"):
        assert parse_date(text, settings, now=NOW).utcoffset() == timedelta(0)


# --- rejection -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        SENTINEL,
        "0000-01-01",
        "0001-01-01T00:00:00Z",
        "1989-12-31",
        "31.12.1989",
        "01.01.0001",
        "-0001-11-30",
    ],
)
def test_implausibly_early_dates_are_out_of_range(settings: Settings, text: str) -> None:
    with pytest.raises(RecordRejected) as exc_info:
        parse_date(text, settings, now=NOW)
    assert _rejected_code(exc_info) == RejectCode.DATE_OUT_OF_RANGE.value


def test_min_plausible_year_boundary(settings: Settings) -> None:
    assert parse_date("1990-06-01", settings, now=NOW).year == 1990
    with pytest.raises(RecordRejected):
        parse_date("1989-06-01", settings, now=NOW)


def test_min_plausible_year_comes_from_settings(settings: Settings) -> None:
    lenient = Settings(_env_file=None, min_plausible_year=1900)
    assert parse_date("1950-01-01", lenient, now=NOW).year == 1949  # Berlin midnight -> UTC
    with pytest.raises(RecordRejected):
        parse_date("1950-01-01", settings, now=NOW)


def test_future_beyond_max_future_days_is_out_of_range(settings: Settings) -> None:
    too_far = (NOW + timedelta(days=401)).date().isoformat()
    with pytest.raises(RecordRejected) as exc_info:
        parse_date(too_far, settings, now=NOW)
    assert _rejected_code(exc_info) == RejectCode.DATE_OUT_OF_RANGE.value


def test_future_within_max_future_days_is_accepted(settings: Settings) -> None:
    # Scheduled meetings are legitimately in the future.
    near = (NOW + timedelta(days=30)).date().isoformat()
    assert parse_date(near, settings, now=NOW) > NOW


def test_far_future_year_is_out_of_range(settings: Settings) -> None:
    with pytest.raises(RecordRejected) as exc_info:
        parse_date("2999-01-01T00:00:00Z", settings, now=NOW)
    assert _rejected_code(exc_info) == RejectCode.DATE_OUT_OF_RANGE.value


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "not a date",
        "32.13.2024",
        "2024-13-45",
        "31.02.2024",
        "2024-03-05T25:00:00",
        "ab.cd.efgh",
    ],
)
def test_garbage_is_unparseable(settings: Settings, text: str) -> None:
    with pytest.raises(RecordRejected) as exc_info:
        parse_date(text, settings, now=NOW)
    assert _rejected_code(exc_info) == RejectCode.DATE_UNPARSEABLE.value


@pytest.mark.parametrize("value", [12345, 1.5, ["2024-03-05"], {"a": 1}, b"2024-03-05"])
def test_wrong_types_are_unparseable_not_typeerror(settings: Settings, value: object) -> None:
    with pytest.raises(RecordRejected) as exc_info:
        parse_date(value, settings, now=NOW)  # type: ignore[arg-type]
    assert _rejected_code(exc_info) == RejectCode.DATE_UNPARSEABLE.value


def test_invalid_timezone_is_a_configuration_error_not_a_record_error() -> None:
    # Settings now rejects an unknown zone at construction (M-4); model_construct bypasses
    # that so the defence-in-depth check inside dates._timezone is still exercised.
    bad = Settings.model_construct(source_timezone="Mars/Olympus_Mons")
    with pytest.raises(ConfigurationError):
        parse_date("2024-03-05", bad, now=NOW)


# --- provenance ladder -----------------------------------------------------


def test_payload_field_wins_when_present(settings: Settings) -> None:
    value, origin = resolve_published_at(
        "2024-03-05T10:00:00Z",
        datetime(2024, 1, 1, tzinfo=UTC),
        "2023-12-01",
        settings,
        now=NOW,
    )
    assert value == datetime(2024, 3, 5, 10, 0, tzinfo=UTC)
    assert origin is PublishedAtOrigin.PAYLOAD_FIELD


def test_naive_http_header_value_is_gmt_not_berlin_local(settings: Settings) -> None:
    """RFC 9110: HTTP-date is GMT, so a naive header value is UTC, not source-local."""
    value, origin = resolve_published_at(None, "2024-03-05T10:00:00", None, settings, now=NOW)
    assert origin is PublishedAtOrigin.HTTP_HEADER
    assert value == datetime(2024, 3, 5, 10, 0, tzinfo=UTC)  # not 09:00 UTC (Berlin shift)


def test_naive_payload_value_still_uses_source_timezone(settings: Settings) -> None:
    value, origin = resolve_published_at("2024-03-05T10:00:00", None, None, settings, now=NOW)
    assert origin is PublishedAtOrigin.PAYLOAD_FIELD
    assert value == datetime(2024, 3, 5, 9, 0, tzinfo=UTC)


def test_naive_meeting_value_still_uses_source_timezone(settings: Settings) -> None:
    value, origin = resolve_published_at(None, None, "2024-03-05T10:00:00", settings, now=NOW)
    assert origin is PublishedAtOrigin.DERIVED_FROM_MEETING
    assert value == datetime(2024, 3, 5, 9, 0, tzinfo=UTC)


def test_null_payload_date_falls_through_to_http_header(settings: Settings) -> None:
    # The real-world case: OParl Paper.date is null.
    header = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
    value, origin = resolve_published_at(None, header, "2023-12-01", settings, now=NOW)
    assert value == header
    assert origin is PublishedAtOrigin.HTTP_HEADER


def test_blank_payload_date_counts_as_absent(settings: Settings) -> None:
    header = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
    value, origin = resolve_published_at("   ", header, None, settings, now=NOW)
    assert (value, origin) == (header, PublishedAtOrigin.HTTP_HEADER)


def test_meeting_date_is_third_tier(settings: Settings) -> None:
    value, origin = resolve_published_at(None, None, "05.03.2024", settings, now=NOW)
    assert value == datetime(2024, 3, 4, 23, 0, tzinfo=UTC)
    assert origin is PublishedAtOrigin.DERIVED_FROM_MEETING


def test_all_tiers_absent_is_null_and_unknown(settings: Settings) -> None:
    assert resolve_published_at(None, None, None, settings, now=NOW) == (
        None,
        PublishedAtOrigin.UNKNOWN,
    )


def test_resolve_rejects_the_real_sentinel_cleanly(settings: Settings) -> None:
    with pytest.raises(RecordRejected) as exc_info:
        resolve_published_at(SENTINEL, None, None, settings, now=NOW)
    assert _rejected_code(exc_info) == RejectCode.DATE_OUT_OF_RANGE.value
    assert SENTINEL in exc_info.value.detail


@pytest.mark.parametrize("position", [0, 1, 2])
def test_sentinel_in_any_tier_is_rejected_not_skipped(settings: Settings, position: int) -> None:
    tiers: list[str | None] = [None, None, None]
    tiers[position] = SENTINEL
    with pytest.raises(RecordRejected) as exc_info:
        resolve_published_at(tiers[0], tiers[1], tiers[2], settings, now=NOW)
    assert _rejected_code(exc_info) == RejectCode.DATE_OUT_OF_RANGE.value


def test_corrupt_payload_date_does_not_fall_through_to_a_lower_tier(settings: Settings) -> None:
    with pytest.raises(RecordRejected):
        resolve_published_at(
            SENTINEL, datetime(2024, 1, 1, tzinfo=UTC), "2023-12-01", settings, now=NOW
        )


@pytest.mark.parametrize(
    "bad", [SENTINEL, "garbage", 42, 1.5, object(), ["x"], "0000-00-00", "99.99.9999"]
)
def test_resolve_is_total_only_record_rejected_escapes(settings: Settings, bad: object) -> None:
    try:
        result = resolve_published_at(bad, None, None, settings, now=NOW)  # type: ignore[arg-type]
    except RecordRejected:
        return
    assert result[1] in set(PublishedAtOrigin)


# --- watermark -------------------------------------------------------------


def test_watermark_swallows_the_real_sentinel(settings: Settings) -> None:
    assert safe_parse_for_watermark(SENTINEL, settings, now=NOW) is None


def test_watermark_sentinel_with_default_settings() -> None:
    # No explicit settings: must still be swallowed, whatever the environment.
    assert safe_parse_for_watermark(SENTINEL) is None


@pytest.mark.parametrize("bad", [None, "", "garbage", 42, 1.5, [], {}, b"2024-01-01", "0000-01-01"])
def test_watermark_swallows_everything_bad(settings: Settings, bad: object) -> None:
    assert safe_parse_for_watermark(bad, settings, now=NOW) is None


def test_watermark_returns_good_values_in_utc(settings: Settings) -> None:
    assert safe_parse_for_watermark("2024-03-05T10:00:00+01:00", settings, now=NOW) == datetime(
        2024, 3, 5, 9, 0, tzinfo=UTC
    )


def test_corrupt_modified_cannot_poison_a_watermark(settings: Settings) -> None:
    """The failure mode this exists for: year -1 becoming max() of the batch."""
    raw = ["2024-03-05T10:00:00Z", SENTINEL, "2024-04-01T09:00:00Z", "2999-01-01T00:00:00Z"]
    parsed = [d for d in (safe_parse_for_watermark(v, settings, now=NOW) for v in raw) if d]
    assert max(parsed) == datetime(2024, 4, 1, 9, 0, tzinfo=UTC)
    assert len(parsed) == 2
