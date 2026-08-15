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


#: Turkish national holidays with fixed Gregorian dates, as (month, day).
TURKEY_FIXED_HOLIDAYS: frozenset[tuple[int, int]] = frozenset(
    {
        (1, 1),  # Yılbaşı
        (4, 23),  # Ulusal Egemenlik ve Çocuk Bayramı
        (5, 1),  # Emek ve Dayanışma Günü
        (5, 19),  # Atatürk'ü Anma, Gençlik ve Spor Bayramı
        (7, 15),  # Demokrasi ve Millî Birlik Günü
        (8, 30),  # Zafer Bayramı
        (10, 29),  # Cumhuriyet Bayramı
    }
)

#: Religious holidays, which follow the lunar calendar and so move against the
#: Gregorian one by roughly eleven days a year. They cannot be computed here
#: reliably -- Turkish observance depends on official proclamation -- so this
#: is an explicit table.
#:
#: **These dates were derived from three years of observed fund NAVs**, not
#: copied from a reference: every weekday with no published price was checked,
#: and after removing the fixed holidays above, exactly these remained. That
#: makes the table evidence rather than assumption.
#:
#: It will go stale. Extend it when a new year's dates are announced; until
#: then the affected days appear as gaps in the coverage report, which is the
#: correct behaviour -- a missing price is visible, whereas a wrongly assumed
#: trading day would silently forward-fill.
TURKEY_RELIGIOUS_HOLIDAYS: frozenset[date] = frozenset(
    {
        # Ramazan Bayramı 2024
        date(2024, 4, 10), date(2024, 4, 11), date(2024, 4, 12),
        # Kurban Bayramı 2024
        date(2024, 6, 17), date(2024, 6, 18), date(2024, 6, 19),
        # Ramazan Bayramı 2025
        date(2025, 3, 31), date(2025, 4, 1),
        # Kurban Bayramı 2025
        date(2025, 6, 6), date(2025, 6, 9),
        # Ramazan Bayramı 2026
        date(2026, 3, 20),
        # Kurban Bayramı 2026
        date(2026, 5, 27), date(2026, 5, 28), date(2026, 5, 29),
    }
)  # fmt: skip

#: Last year the religious-holiday table covers. Past this the TEFAS calendar
#: silently over-reports expected trading days.
TURKEY_HOLIDAYS_KNOWN_THROUGH = 2026


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
        if (day.month, day.day) in TURKEY_FIXED_HOLIDAYS:
            return False
        return day not in TURKEY_RELIGIOUS_HOLIDAYS
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
