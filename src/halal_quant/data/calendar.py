"""NYSE trading calendar, 1998 onward (PRD §7, BRD §18 "gaps" and "stale data").

Answers "was the market open on this date?" from NYSE's published holiday rules plus the few
one-off closures, so it needs no data feed and no dependency. Dates are US Eastern trading
dates, matching how the price data is keyed.

Years before 1998 are refused (Martin Luther King Day did not exist yet), and so is a year
this table has not been checked for: an unknown calendar must never be guessed at.
"""

from collections.abc import Iterator
from datetime import date, timedelta

MIN_YEAR = 1998

# The last year whose rules and one-off closures have been checked. Extend it yearly.
MAX_YEAR = 2027

# One-off full-day closures that no rule predicts.
SPECIAL_CLOSURES: dict[date, str] = {
    date(2001, 9, 11): "September 11 attacks",
    date(2001, 9, 12): "September 11 attacks",
    date(2001, 9, 13): "September 11 attacks",
    date(2001, 9, 14): "September 11 attacks",
    date(2004, 6, 11): "National day of mourning (Ronald Reagan)",
    date(2007, 1, 2): "National day of mourning (Gerald Ford)",
    date(2012, 10, 29): "Hurricane Sandy",
    date(2012, 10, 30): "Hurricane Sandy",
    date(2018, 12, 5): "National day of mourning (George H. W. Bush)",
    date(2025, 1, 9): "National day of mourning (Jimmy Carter)",
}

# One-off 1 p.m. closes that the usual pattern (below) does not predict, and the one day it
# predicts wrongly.
SPECIAL_EARLY_CLOSES = {date(1999, 12, 31), date(2002, 7, 5), date(2003, 12, 26)}
NOT_EARLY_CLOSES = {date(2002, 7, 3)}


class CalendarError(Exception):
    """The calendar cannot answer for this date. Callers must treat that as "not tradable"."""


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th (1-based) given weekday of a month; weekday 0 = Monday."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _observed(holiday: date) -> date:
    """NYSE Rule 7.2: a Saturday holiday closes Friday, a Sunday holiday closes Monday."""
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def _check_year(year: int) -> None:
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise CalendarError(
            f"The NYSE calendar covers {MIN_YEAR} to {MAX_YEAR}; it cannot answer for {year}."
        )


def _holidays(year: int) -> dict[date, str]:
    days = {
        _nth_weekday(year, 2, 0, 3): "Washington's Birthday",
        _easter(year) - timedelta(days=2): "Good Friday",
        _last_weekday(year, 5, 0): "Memorial Day",
        _nth_weekday(year, 9, 0, 1): "Labor Day",
        _nth_weekday(year, 11, 3, 4): "Thanksgiving Day",
        _observed(date(year, 7, 4)): "Independence Day",
        _observed(date(year, 12, 25)): "Christmas Day",
    }
    if year >= 1998:
        days[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    if year >= 2022:
        days[_observed(date(year, 6, 19))] = "Juneteenth"
    # New Year's Day: a Saturday 1 January is NOT observed on the Friday before (it would fall
    # in the previous year's trading).
    if date(year, 1, 1).weekday() != 5:
        days[_observed(date(year, 1, 1))] = "New Year's Day"
    return days


def holiday_name(day: date) -> str | None:
    """Why the market was closed on a weekday, or None if it was open."""
    _check_year(day.year)
    if day in SPECIAL_CLOSURES:
        return SPECIAL_CLOSURES[day]
    return _holidays(day.year).get(day)


def is_trading_day(day: date) -> bool:
    """True if the NYSE held a regular session on this date."""
    _check_year(day.year)
    return day.weekday() < 5 and holiday_name(day) is None


def is_early_close(day: date) -> bool:
    """True for a trading day that closed at 1 p.m. instead of 4 p.m.

    Usual pattern: the day after Thanksgiving, 24 December, and 3 July when 4 July is a
    Tuesday to Friday.
    """
    if not is_trading_day(day):
        return False
    if day in SPECIAL_EARLY_CLOSES:
        return True
    if day in NOT_EARLY_CLOSES:
        return False
    if day.month == 11:  # the day after Thanksgiving
        return day == _nth_weekday(day.year, 11, 3, 4) + timedelta(days=1)
    if day.month == 12 and day.day == 24:
        return True
    return day.month == 7 and day.day == 3 and date(day.year, 7, 4).weekday() in (1, 2, 3, 4)


def trading_days(start: date, end: date) -> Iterator[date]:
    """Every trading day from `start` to `end`, both included."""
    current = start
    while current <= end:
        if is_trading_day(current):
            yield current
        current += timedelta(days=1)


def previous_trading_day(day: date) -> date:
    """The last trading day strictly before `day`."""
    current = day - timedelta(days=1)
    while not is_trading_day(current):
        current -= timedelta(days=1)
    return current


def next_trading_day(day: date) -> date:
    """The first trading day strictly after `day`."""
    current = day + timedelta(days=1)
    while not is_trading_day(current):
        current += timedelta(days=1)
    return current


def trading_days_between(earlier: date, later: date) -> int:
    """How many trading days after `earlier` up to and including `later` (0 if none)."""
    if later <= earlier:
        return 0
    return sum(1 for _ in trading_days(earlier + timedelta(days=1), later))
