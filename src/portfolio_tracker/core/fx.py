"""Currency conversion. The only place in the codebase that divides by a rate.

Three rules, from `docs/adr/0004`:

1. **One direction.** ``rate`` is always units of *quote* per 1 EUR, enforced by
   ``CHECK (base = 'EUR')`` in the schema. Conversion to euro is therefore
   always division. There is no multiply path, so an inverted rate is not a bug
   that can be written -- the row it would need cannot exist.
2. **Native is authoritative.** Euro figures are derivations, stored with the
   rate and as-of date that produced them.
3. **Conversion happens here, in the lot builder, and in daily valuation.**
   Nowhere else. ``returns.py``, ``tax.py`` and the API contain no FX
   arithmetic at all, which is checkable by grep and is enforced by a test.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portfolio_tracker.core.calendars import Calendar, business_days_between, grid_days

BASE = "EUR"

#: How many grid days a rate may be carried forward before it stops being a
#: reasonable stand-in. Counted in business days, not calendar days.
DEFAULT_MAX_STALE_DAYS = 5

#: Preferred sources, most authoritative first. A rate from a better source is
#: never overwritten by a worse one, regardless of which arrived later.
SOURCE_PRIORITY: dict[str, int] = {"manual": 0, "ecb": 1, "yfinance": 2}


class FxUnavailable(LookupError):
    """No rate within tolerance. Raised rather than defaulted.

    Substituting 1.0, or the nearest rate at any distance, would silently
    misprice a position -- and for a rapidly depreciating currency an
    off-by-a-few-days rate is a materially wrong number, not a rounding issue.
    """


@dataclass(frozen=True)
class FxRate:
    on: date
    quote: str
    rate: Decimal
    asof: date
    stale_days: int
    source: str

    @property
    def is_identity(self) -> bool:
        return self.source == "identity"


def identity_rate(on: date) -> FxRate:
    return FxRate(on, BASE, Decimal(1), on, 0, "identity")


def get_rate(
    conn: sqlite3.Connection,
    quote: str,
    on: date,
    *,
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
    calendar: Calendar = "TARGET2",
    strict: bool = True,
) -> FxRate:
    """The rate for ``quote`` on ``on``, carried forward if necessary.

    ``quote == 'EUR'`` short-circuits to 1 **without touching the database**.
    That is not an optimisation: it is what guarantees a euro-only portfolio
    reports exactly zero currency contribution rather than a rounding artefact,
    and a test asserts no query is issued.
    """
    if quote == BASE:
        return identity_rate(on)

    row = conn.execute(
        """
        SELECT date, rate, source FROM fx_rate
        WHERE quote = ? AND date <= ?
        ORDER BY date DESC
        LIMIT 1
        """,
        (quote, on.isoformat()),
    ).fetchone()

    if row is None:
        raise FxUnavailable(f"no {BASE}/{quote} rate at or before {on.isoformat()}")

    asof = date.fromisoformat(row["date"])
    stale = business_days_between(asof, on, calendar)
    if stale > max_stale_days:
        message = (
            f"{BASE}/{quote} rate for {on.isoformat()} is {stale} business days stale "
            f"(latest {asof.isoformat()}, tolerance {max_stale_days})"
        )
        if strict:
            raise FxUnavailable(message)

    return FxRate(on, quote, Decimal(str(row["rate"])), asof, stale, row["source"])


def get_rate_series(
    conn: sqlite3.Connection,
    quote: str,
    days: list[date],
    *,
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
    calendar: Calendar = "TARGET2",
    strict: bool = True,
) -> dict[date, FxRate]:
    """Rates for many days in one query rather than N.

    Same forward-fill semantics as :func:`get_rate`, including the euro
    short-circuit.
    """
    if not days:
        return {}
    if quote == BASE:
        return {day: identity_rate(day) for day in days}

    first, last = min(days), max(days)
    rows = conn.execute(
        """
        SELECT date, rate, source FROM fx_rate
        WHERE quote = ? AND date <= ?
        ORDER BY date
        """,
        (quote, last.isoformat()),
    ).fetchall()

    observed = [(date.fromisoformat(r["date"]), Decimal(str(r["rate"])), r["source"]) for r in rows]
    if not observed:
        raise FxUnavailable(f"no {BASE}/{quote} rates at or before {last.isoformat()}")

    series: dict[date, FxRate] = {}
    cursor = 0
    current: tuple[date, Decimal, str] | None = None
    for day in sorted(days):
        while cursor < len(observed) and observed[cursor][0] <= day:
            current = observed[cursor]
            cursor += 1
        if current is None:
            raise FxUnavailable(
                f"no {BASE}/{quote} rate at or before {day.isoformat()} "
                f"(earliest available {first.isoformat()})"
            )
        asof, rate, source = current
        stale = 0 if asof == day else business_days_between(asof, day, calendar)
        if stale > max_stale_days and strict:
            raise FxUnavailable(
                f"{BASE}/{quote} rate for {day.isoformat()} is {stale} business days stale"
            )
        series[day] = FxRate(day, quote, rate, asof, stale, source)
    return series


def to_eur(amount_local: Decimal, rate: Decimal) -> Decimal:
    """The only conversion function in the codebase.

    Always division, because the rate is always quote-per-euro.
    """
    if rate <= 0:
        raise ValueError(f"exchange rate must be positive, got {rate}")
    return amount_local / rate


def required_quotes(conn: sqlite3.Connection) -> set[str]:
    """Every non-euro currency the database actually needs rates for."""
    rows = conn.execute(
        """
        SELECT DISTINCT currency FROM instrument WHERE currency <> ?
        UNION
        SELECT DISTINCT currency FROM account WHERE currency <> ?
        """,
        (BASE, BASE),
    ).fetchall()
    return {row["currency"] for row in rows}


@dataclass(frozen=True)
class FxGap:
    quote: str
    missing: list[date]
    first_available: date | None
    last_available: date | None

    @property
    def is_complete(self) -> bool:
        return not self.missing


def coverage_report(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    *,
    quotes: set[str] | None = None,
    calendar: Calendar = "TARGET2",
) -> list[FxGap]:
    """Which grid days have no observed rate at all.

    Forward-filling hides gaps by design, so this reports on *observations*
    rather than on what the fill would produce. A currency that moves fast makes
    a week-long gap a real error, not a cosmetic one.
    """
    targets = quotes if quotes is not None else required_quotes(conn)
    expected = grid_days(start, end, calendar)

    gaps: list[FxGap] = []
    for quote in sorted(targets):
        rows = conn.execute(
            "SELECT date FROM fx_rate WHERE quote = ? AND date BETWEEN ? AND ?",
            (quote, start.isoformat(), end.isoformat()),
        ).fetchall()
        have = {date.fromisoformat(r["date"]) for r in rows}
        bounds = conn.execute(
            "SELECT min(date) AS lo, max(date) AS hi FROM fx_rate WHERE quote = ?", (quote,)
        ).fetchone()
        gaps.append(
            FxGap(
                quote=quote,
                missing=[day for day in expected if day not in have],
                first_available=date.fromisoformat(bounds["lo"]) if bounds["lo"] else None,
                last_available=date.fromisoformat(bounds["hi"]) if bounds["hi"] else None,
            )
        )
    return gaps
