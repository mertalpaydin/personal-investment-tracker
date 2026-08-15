"""Business calendars. Pure functions, no network, no database.

The canonical valuation grid is **TARGET2**, the euro settlement calendar --
not the exchange calendar of any instrument, and not the union of them.

The reasoning is that a valuation date is only meaningful if an exchange rate
exists for it, and the ECB publishes reference rates on exactly the TARGET2
calendar. Choosing that grid guarantees the FX leg is never stale, which removes
the worst failure mode available here: a frozen rate holding a foreign position's
euro value still while the currency moves underneath it.

The cost is stated rather than hidden. On a foreign market holiday a valuation
row is still produced -- the local price is a day old but the rate is current,
which is correct, because the euro value of a foreign asset genuinely does move
when the currency does. On a TARGET2 holiday no row exists at all and the
foreign market's move lands on the next grid day.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

Calendar = str

TARGET2 = "TARGET2"
XETRA = "XETRA"
TEFAS = "TEFAS"
ALL_DAYS = "ALL_DAYS"


@lru_cache(maxsize=64)
def easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm.

    Good Friday and Easter Monday are TARGET2 holidays and move every year, so
    they cannot be a static list.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * ell) // 433
    month = (h + ell - 7 * m + 90) // 25
    day = (h + ell - 7 * m + 33 * month + 19) % 32
    return date(year, month, day)


@lru_cache(maxsize=64)
def target2_holidays(year: int) -> frozenset[date]:
    """The six days TARGET2 is closed, beyond weekends.

    Deliberately short: TARGET2 does not observe national holidays, which is
    part of why it is a better spine than any single country's calendar.
    """
    easter = easter_sunday(year)
    return frozenset(
        {
            date(year, 1, 1),
            easter - timedelta(days=2),  # Good Friday
            easter + timedelta(days=1),  # Easter Monday
            date(year, 5, 1),
            date(year, 12, 25),
            date(year, 12, 26),
        }
    )


def is_business_day(day: date, calendar: Calendar = TARGET2) -> bool:
    if calendar == ALL_DAYS:
        return True
    if day.weekday() >= 5:
        return False
    if calendar in (TARGET2, XETRA):
        # Xetra adds 24 and 31 December, which TARGET2 does not observe.
        if calendar == XETRA and (day.month, day.day) in ((12, 24), (12, 31)):
            return False
        return day not in target2_holidays(day.year)
    if calendar == TEFAS:
        # Turkish public and religious holidays are not modelled: the religious
        # ones move against the Gregorian calendar and would need a table that
        # goes stale silently. Weekday-only is honest about that, and the
        # forward-fill machinery already reports the resulting gaps.
        return True
    raise ValueError(f"unknown calendar: {calendar!r}")


def grid_days(start: date, end: date, calendar: Calendar = TARGET2) -> list[date]:
    """Every business day in ``[start, end]`` inclusive."""
    if end < start:
        return []
    days: list[date] = []
    current = start
    while current <= end:
        if is_business_day(current, calendar):
            days.append(current)
        current += timedelta(days=1)
    return days


def previous_business_day(day: date, calendar: Calendar = TARGET2) -> date:
    current = day - timedelta(days=1)
    while not is_business_day(current, calendar):
        current -= timedelta(days=1)
    return current


def business_days_between(start: date, end: date, calendar: Calendar = TARGET2) -> int:
    """Count of business days strictly after ``start``, up to and including ``end``.

    Staleness is measured in *grid* days rather than calendar days on purpose.
    A five-day tolerance in calendar days would be barely a long weekend, while
    a run of foreign market holidays abutting a weekend can put a real gap well
    past that without anything being wrong.
    """
    if end <= start:
        return 0
    return len(grid_days(start + timedelta(days=1), end, calendar))
