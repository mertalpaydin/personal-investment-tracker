"""How one instrument has done, on its own, over several horizons.

Everything else in this codebase measures the *portfolio*. This measures an
instrument, which is a different question and the one a comparison needs: to
say "this fund returned 7 percentage points more over three years than yours"
both sides have to be measured the same way, and neither of them is a
portfolio.

Three rules, each of which is a way this could quietly lie.

**Returns are computed in euros.** A Turkish fund up 90% in lira has not
returned 90% to somebody who spends euros, and ranking it against a
euro-quoted ETF on local returns would put it top of every list for a reason
that is not performance. Converting a price at the rate for that same day is
exactly the operation ``valuation.py`` performs daily, which is why this
module is admitted to the short list of places allowed to do it (ADR 0004;
``tests/test_fx.py`` enforces the list).

**Total return where it exists, price return where it does not, and never a
silent mix.** ``yfinance`` supplies a dividend-adjusted series and the TEFAS
and Fintables feeds do not, so in the live database ten instruments have a
total-return series and two have only closes. Comparing a distributing fund
measured on price alone against an accumulating one measured on total return
understates the first by roughly its yield -- several percentage points a
year, in the direction that makes the wrong fund look better. Every result
carries the ``basis`` it was measured on so a caller can refuse to compare
across the two.

**A metric that cannot be computed is None, never zero.** An instrument with
eighteen months of history has no three-year return. Zero would say it went
nowhere, which is a different and false claim.

Computed on read, deliberately. Fifty instruments of daily history is a
fraction of a second, and a stored copy is one more thing that can disagree
with the prices it was derived from -- the failure this repository's
"store facts, derive everything else" rule exists to prevent.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise

from portfolio_tracker.core import fx, returns
from portfolio_tracker.money import ZERO

#: Horizons reported, and the calendar days each looks back over.
WINDOWS: dict[str, int] = {
    "1m": 31,
    "3m": 92,
    "6m": 183,
    "1y": 366,
    "3y": 1096,
    "5y": 1827,
}

#: Fewest observations inside a window before its return is reported at all.
#: Roughly 60% of the trading days the window should contain -- enough that a
#: holiday-heavy stretch still qualifies, few enough that four prices spanning
#: three years cannot masquerade as a three-year return.
MIN_COVERAGE = 0.6

#: Trading days in a year, matching `returns.TRADING_DAYS`.
TRADING_DAYS_PER_YEAR = 252


class PerformanceError(ValueError):
    """The instrument cannot be measured as asked."""


@dataclass(frozen=True)
class Point:
    on: date
    level: Decimal


@dataclass
class Metrics:
    """One instrument's record, over every horizon it has data for."""

    instrument_id: int
    import_key: str
    as_of: date
    currency: str
    #: TOTAL_RETURN when distributions are reflected in the series, PRICE when
    #: only closes were available. Never compare across the two without saying so.
    basis: str
    observations: int
    history_from: date | None
    in_eur: bool = True

    ret_1m: Decimal | None = None
    ret_3m: Decimal | None = None
    ret_6m: Decimal | None = None
    ret_1y: Decimal | None = None
    ret_3y_ann: Decimal | None = None
    ret_5y_ann: Decimal | None = None
    vol_1y: Decimal | None = None
    vol_3y: Decimal | None = None
    max_dd_3y: Decimal | None = None
    sharpe_1y: Decimal | None = None
    sharpe_3y: Decimal | None = None

    caveats: list[str] = field(default_factory=list)

    @property
    def history_days(self) -> int:
        return (self.as_of - self.history_from).days if self.history_from else 0

    @property
    def comparable_basis(self) -> str:
        return self.basis


def _rows(
    conn: sqlite3.Connection, instrument_id: int, *, start: date | None, end: date
) -> list[sqlite3.Row]:
    if start is None:
        return conn.execute(
            "SELECT date, close, total_return_close FROM price "
            "WHERE instrument_id = ? AND date <= ? ORDER BY date",
            (instrument_id, end.isoformat()),
        ).fetchall()
    return conn.execute(
        "SELECT date, close, total_return_close FROM price "
        "WHERE instrument_id = ? AND date BETWEEN ? AND ? ORDER BY date",
        (instrument_id, start.isoformat(), end.isoformat()),
    ).fetchall()


def basis_for(rows: list[sqlite3.Row]) -> str:
    """TOTAL_RETURN only if every row has an adjusted close.

    All-or-nothing on purpose. A series that is adjusted for part of its life
    and raw for the rest has a step in it where the two meet, and that step
    would be read as a return.
    """
    if not rows:
        return "PRICE"
    return "TOTAL_RETURN" if all(r["total_return_close"] is not None for r in rows) else "PRICE"


def series(
    conn: sqlite3.Connection,
    instrument_id: int,
    *,
    currency: str,
    start: date | None = None,
    end: date | None = None,
    basis: str = "auto",
    in_eur: bool = True,
) -> tuple[list[Point], str, list[str]]:
    """The instrument's level series, in euros by default.

    Returns the points, the basis actually used, and any caveats about the
    conversion. Days whose exchange rate is unknown are dropped rather than
    carried at a neighbouring rate: a price converted at the wrong day's rate
    is a fabricated observation, and the gap it leaves is honest.
    """
    horizon = end or date.today()
    rows = _rows(conn, instrument_id, start=start, end=horizon)
    if not rows:
        return [], "PRICE", ["no prices are stored for this instrument"]

    used = basis_for(rows) if basis == "auto" else basis
    column = "total_return_close" if used == "TOTAL_RETURN" else "close"
    caveats: list[str] = []
    if used == "PRICE":
        caveats.append(
            "Measured on closing prices only: this instrument's feed supplies no "
            "dividend-adjusted series, so anything it distributed is missing from "
            "these figures."
        )

    days = [date.fromisoformat(r["date"]) for r in rows]
    levels = [Decimal(str(r[column])) for r in rows]

    if not in_eur or currency == fx.BASE:
        return [Point(d, level) for d, level in zip(days, levels, strict=True)], used, caveats

    # One query for the whole series rather than one per day, and non-strict
    # because a decade of history will contain days the rate table does not.
    rates = fx.get_rate_series(conn, currency, days, strict=False)
    points: list[Point] = []
    missing = 0
    for day, level in zip(days, levels, strict=True):
        rate = rates.get(day)
        if rate is None or rate.rate <= ZERO:
            missing += 1
            continue
        points.append(Point(day, fx.to_eur(level, rate.rate)))

    if missing:
        caveats.append(
            f"{missing} day(s) had no {currency} exchange rate and were left out "
            "rather than converted at a neighbouring day's rate."
        )
    return points, used, caveats


def _window(points: list[Point], as_of: date, days: int) -> list[Point]:
    cutoff = as_of - timedelta(days=days)
    return [p for p in points if p.on >= cutoff]


def _return_over(points: list[Point], as_of: date, days: int) -> Decimal | None:
    """Total return across a window, or None if it is too thinly covered."""
    window = _window(points, as_of, days)
    if len(window) < 2:
        return None
    # How many observations to expect depends on how often this series is
    # sampled, not on how often a daily one would be. Assuming 252 a year
    # rejected every weekly series outright -- a full year of weekly closes
    # is 52 rows against a 126-row floor -- so instruments priced weekly had
    # a volatility and a drawdown but never a return, and ranked last in
    # every comparison for want of the figure they are ranked by.
    expected = days * _periods_per_year(window) / 365
    if len(window) < expected * MIN_COVERAGE:
        return None
    first, last = window[0].level, window[-1].level
    return (last / first - 1) if first > ZERO else None


def _daily_returns(points: list[Point]) -> list[Decimal]:
    out: list[Decimal] = []
    for previous, current in pairwise(points):
        if previous.level > ZERO:
            out.append(current.level / previous.level - 1)
    return out


def _periods_per_year(points: list[Point]) -> float:
    """Observations a year, from the median gap between them.

    The median rather than the mean, because a series that starts weekly and
    becomes daily -- which is exactly what happens when a watched instrument
    is bought -- would otherwise be scaled by neither.
    """
    if len(points) < 3:
        return float(TRADING_DAYS_PER_YEAR)
    gaps = sorted(
        (points[i].on - points[i - 1].on).days
        for i in range(1, len(points))
        if (points[i].on - points[i - 1].on).days > 0
    )
    if not gaps:
        return float(TRADING_DAYS_PER_YEAR)
    median = gaps[len(gaps) // 2]
    # A daily series has a median gap of 1 (weekends land in the tail), and
    # 252 is the right divisor for it. Anything sparser is scaled by calendar
    # days, which is what a weekly series actually is.
    return float(TRADING_DAYS_PER_YEAR) if median <= 1 else 365.25 / median


#: Computed metrics, keyed by everything that could change them: the
#: instrument and horizon, plus a stamp of its price rows and of the FX table.
#: Prices change once a day, so the second request for a page is served from
#: here instead of re-walking three years of closes for 250 instruments.
_METRICS_CACHE: dict[tuple[object, ...], Metrics] = {}
_METRICS_CACHE_LIMIT = 2000


def _data_stamp(conn: sqlite3.Connection, instrument_id: int) -> tuple[object, ...] | None:
    """What the cache key must carry, or None when caching is unsafe.

    An in-memory database (tests) has no identity across connections, so two
    unrelated fixtures would collide on the same instrument id: never cache
    those. For a file, the path plus the newest price and FX rows is enough --
    the daily update only ever appends.
    """
    files = [row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"]
    if not files or not files[0]:
        return None
    price_max_date = conn.execute(
        "SELECT max(date) FROM price WHERE instrument_id = ?",
        (instrument_id,),
    ).fetchone()[0]
    price_max_rowid = conn.execute("SELECT max(rowid) FROM price").fetchone()[0]
    fx_max_rowid = conn.execute("SELECT max(rowid) FROM fx_rate").fetchone()[0]
    return (files[0], price_max_date, price_max_rowid, fx_max_rowid)


def clear_metrics_cache() -> None:
    _METRICS_CACHE.clear()


def metrics(
    conn: sqlite3.Connection,
    instrument_id: int,
    *,
    import_key: str,
    currency: str,
    as_of: date | None = None,
    in_eur: bool = True,
) -> Metrics:
    """Every horizon this instrument has enough history to support.

    Memoised per process: the result only changes when a price or FX row is
    added, and the cache key carries a stamp of both.
    """
    horizon = as_of or date.today()
    stamp = _data_stamp(conn, instrument_id)
    key = (instrument_id, currency, in_eur, horizon.isoformat(), *stamp) if stamp else None
    if key is not None:
        hit = _METRICS_CACHE.get(key)
        if hit is not None:
            return hit
    result = _compute_metrics(
        conn,
        instrument_id,
        import_key=import_key,
        currency=currency,
        horizon=horizon,
        in_eur=in_eur,
    )
    if key is not None:
        if len(_METRICS_CACHE) >= _METRICS_CACHE_LIMIT:
            _METRICS_CACHE.clear()
        _METRICS_CACHE[key] = result
    return result


def _compute_metrics(
    conn: sqlite3.Connection,
    instrument_id: int,
    *,
    import_key: str,
    currency: str,
    horizon: date,
    in_eur: bool,
) -> Metrics:
    points, basis, caveats = series(
        conn, instrument_id, currency=currency, end=horizon, in_eur=in_eur
    )

    result = Metrics(
        instrument_id=instrument_id,
        import_key=import_key,
        as_of=horizon,
        currency=currency,
        basis=basis,
        observations=len(points),
        history_from=points[0].on if points else None,
        in_eur=in_eur,
        caveats=caveats,
    )
    if len(points) < 2:
        result.caveats.append("too little price history to measure anything")
        return result

    result.ret_1m = _return_over(points, horizon, WINDOWS["1m"])
    result.ret_3m = _return_over(points, horizon, WINDOWS["3m"])
    result.ret_6m = _return_over(points, horizon, WINDOWS["6m"])
    result.ret_1y = _return_over(points, horizon, WINDOWS["1y"])

    three = _return_over(points, horizon, WINDOWS["3y"])
    if three is not None:
        result.ret_3y_ann = returns.annualise(three, WINDOWS["3y"])
    five = _return_over(points, horizon, WINDOWS["5y"])
    if five is not None:
        result.ret_5y_ann = returns.annualise(five, WINDOWS["5y"])

    # Scaled by how far apart the observations actually are. A weekly series
    # annualised as if it were daily reports about 2.2x the real volatility,
    # which lands the instrument in the wrong band with no sign anything is
    # wrong -- the number is plausible, precise, and 120% too big.
    per_year = _periods_per_year(points)
    result.vol_1y = returns.annualised_volatility(
        _daily_returns(_window(points, horizon, WINDOWS["1y"])), periods_per_year=per_year
    )
    result.vol_3y = returns.annualised_volatility(
        _daily_returns(_window(points, horizon, WINDOWS["3y"])), periods_per_year=per_year
    )
    result.max_dd_3y = returns.deepest_fall(
        [p.level for p in _window(points, horizon, WINDOWS["3y"])]
    )

    # Risk-free rate baseline (standard euro deposit / overnight ~3.0%)
    rf = Decimal("0.03")
    if result.ret_1y is not None and result.vol_1y is not None and result.vol_1y > ZERO:
        result.sharpe_1y = (result.ret_1y - rf) / result.vol_1y
    if result.ret_3y_ann is not None and result.vol_3y is not None and result.vol_3y > ZERO:
        result.sharpe_3y = (result.ret_3y_ann - rf) / result.vol_3y

    if in_eur and currency != fx.BASE:
        result.caveats.append(
            f"Measured in euros: the {currency} price series converted at each "
            "day's rate, which is what a euro investor actually earned and is "
            "not the same as the local return."
        )
    return result


def for_instruments(
    conn: sqlite3.Connection,
    instrument_ids: list[int] | None = None,
    *,
    as_of: date | None = None,
) -> dict[int, Metrics]:
    """Measure many instruments in one pass, keyed by id."""
    where = ""
    args: list[object] = []
    if instrument_ids is not None:
        if not instrument_ids:
            return {}
        where = f"WHERE id IN ({','.join('?' for _ in instrument_ids)})"
        args = list(instrument_ids)

    rows = conn.execute(
        f"SELECT id, import_key, currency FROM instrument {where} ORDER BY import_key", args
    ).fetchall()
    return {
        int(r["id"]): metrics(
            conn,
            int(r["id"]),
            import_key=r["import_key"],
            currency=r["currency"],
            as_of=as_of,
        )
        for r in rows
    }
