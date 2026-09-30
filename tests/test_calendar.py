"""NYSE calendar: known holidays, half-days and one-off closures (task 8 acceptance).

Expected dates come from NYSE's published schedules and were cross-checked once against an
independent calendar library for every day from 1998 to 2027 (no differences).
"""

from datetime import date

import pytest

from halal_quant.data.calendar import (
    CalendarError,
    holiday_name,
    is_early_close,
    is_trading_day,
    next_trading_day,
    previous_trading_day,
    trading_days,
    trading_days_between,
)


@pytest.mark.parametrize(
    ("day", "name"),
    [
        (date(2024, 1, 1), "New Year's Day"),
        (date(2024, 1, 15), "Martin Luther King Jr. Day"),
        (date(2024, 2, 19), "Washington's Birthday"),
        (date(2024, 3, 29), "Good Friday"),
        (date(2024, 5, 27), "Memorial Day"),
        (date(2024, 6, 19), "Juneteenth"),
        (date(2024, 7, 4), "Independence Day"),
        (date(2024, 9, 2), "Labor Day"),
        (date(2024, 11, 28), "Thanksgiving Day"),
        (date(2024, 12, 25), "Christmas Day"),
        (date(2019, 4, 19), "Good Friday"),  # a different Easter
        (date(2011, 4, 22), "Good Friday"),  # late-April Easter
    ],
)
def test_regular_holidays_are_closed(day: date, name: str) -> None:
    assert holiday_name(day) == name
    assert not is_trading_day(day)


def test_juneteenth_is_only_a_holiday_from_2022() -> None:
    assert is_trading_day(date(2021, 6, 18))
    assert is_trading_day(date(2019, 6, 19))
    assert not is_trading_day(date(2022, 6, 20))  # 19 June was a Sunday: observed Monday


def test_saturday_holidays_close_the_friday_before() -> None:
    assert not is_trading_day(date(2020, 7, 3))  # 4 July 2020 was a Saturday
    assert not is_trading_day(date(2021, 12, 24))  # Christmas 2021 was a Saturday


def test_sunday_holidays_close_the_monday_after() -> None:
    assert not is_trading_day(date(2021, 7, 5))
    assert not is_trading_day(date(2022, 12, 26))
    assert not is_trading_day(date(2023, 1, 2))


def test_saturday_new_years_day_does_not_close_the_friday_before() -> None:
    assert date(2022, 1, 1).weekday() == 5
    assert is_trading_day(date(2021, 12, 31))


@pytest.mark.parametrize(
    "day",
    [
        date(2001, 9, 11),
        date(2001, 9, 14),
        date(2004, 6, 11),
        date(2007, 1, 2),
        date(2012, 10, 29),
        date(2012, 10, 30),
        date(2018, 12, 5),
        date(2025, 1, 9),
    ],
)
def test_one_off_closures(day: date) -> None:
    assert not is_trading_day(day)
    assert holiday_name(day) is not None


def test_weekends_are_never_trading_days() -> None:
    assert not is_trading_day(date(2024, 6, 8))
    assert not is_trading_day(date(2024, 6, 9))
    assert holiday_name(date(2024, 6, 8)) is None  # closed, but not a holiday


@pytest.mark.parametrize(
    "day",
    [
        date(2024, 11, 29),  # day after Thanksgiving
        date(2024, 12, 24),
        date(2024, 7, 3),
        date(2023, 7, 3),
        date(2018, 12, 24),
        date(2002, 7, 5),  # one-off
        date(2003, 12, 26),  # one-off
        date(1999, 12, 31),  # one-off
    ],
)
def test_half_days(day: date) -> None:
    assert is_trading_day(day)
    assert is_early_close(day)


@pytest.mark.parametrize(
    "day",
    [
        date(2024, 11, 27),  # Wednesday before Thanksgiving
        date(2024, 12, 23),
        date(2022, 7, 1),  # 4 July 2022 was a Monday: no early close
        date(2020, 7, 2),  # Friday 3 July was the observed holiday
        date(2021, 12, 23),  # Christmas Eve 2021 was a full holiday, not a half-day
        date(2002, 7, 3),  # one-off exception to the usual pattern
        date(2024, 11, 28),  # holiday, not a trading day at all
    ],
)
def test_not_half_days(day: date) -> None:
    assert not is_early_close(day)


def test_previous_and_next_trading_day_skip_weekends_and_holidays() -> None:
    assert previous_trading_day(date(2024, 5, 28)) == date(2024, 5, 24)  # over Memorial Day
    assert next_trading_day(date(2024, 5, 24)) == date(2024, 5, 28)
    assert next_trading_day(date(2024, 3, 28)) == date(2024, 4, 1)  # over Good Friday
    assert previous_trading_day(date(2024, 6, 10)) == date(2024, 6, 7)


def test_trading_days_lists_and_counts_sessions() -> None:
    days = list(trading_days(date(2024, 12, 23), date(2025, 1, 3)))
    assert days == [
        date(2024, 12, 23),
        date(2024, 12, 24),
        date(2024, 12, 26),
        date(2024, 12, 27),
        date(2024, 12, 30),
        date(2024, 12, 31),
        date(2025, 1, 2),
        date(2025, 1, 3),
    ]
    assert trading_days_between(date(2024, 12, 24), date(2025, 1, 2)) == 5
    assert trading_days_between(date(2024, 12, 24), date(2024, 12, 24)) == 0
    assert trading_days_between(date(2025, 1, 2), date(2024, 12, 24)) == 0


@pytest.mark.parametrize("year", [1997, 2028])
def test_years_outside_the_checked_range_are_refused(year: int) -> None:
    with pytest.raises(CalendarError, match="cannot answer"):
        is_trading_day(date(year, 6, 3))
