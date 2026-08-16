"""Where the return came from: prices, currency, income, or trading.

The tool's headline claim — "your gold fund is up 97%, and in euros you made
34%" — existed only per position, and only as an *unrealized level*. Ask it the
same question about the whole portfolio over a year and there was no answer:
``ret_local_contrib``, ``ret_fx_contrib`` and ``ret_residual`` have been columns
on ``portfolio_daily`` since migration 0001 and nothing has ever written to them.

This module fills them, and answers the portfolio-level version: *of the 14%
you made this year, how much was asset prices and how much was the lira?*

## Two different gaps, and only one of them is arithmetic

**The linking gap.** Daily contributions that sum correctly every single day do
not sum to the compounded period return, because returns multiply and
contributions add. Over 500 days it is not a rounding error. ADR 0004 called
this `residual` and exposed it rather than allocating it away, which was the
right call at the time and is no longer necessary: **Carino smoothing removes it
exactly**, and :func:`link` proves it rather than asserting it.

**The attribution gap.** The modelled terms do not fully explain what the
portfolio actually did. Trades executed away from the closing price, fees, a
quantity that changed mid-day — none of that is a price move or a currency move.
That gap is real, it is *not* a linking artefact, and Carino cannot make it go
away. It is computed, named ``unexplained``, and linked alongside the others.

Collapsing the two would be the easy mistake. A tool reporting `residual: 0`
while quietly folding unexplained trading into "local return" would be lying
more convincingly than one that reports neither.

## The convention, which is the one already in use

Per position, per day, on the quantity held at the *start* of the day::

    local  = Q * (P_t - P_p) / X_t          the price move, in today's euros
    fx     = Q * P_p * (1/X_t - 1/X_p)      the opening capital, re-translated
    -------------------------------------
    sum    = Q*P_t/X_t - Q*P_p/X_p          exactly the change in euro value

This is the same ordering as the position-level split in ADR 0004:
`unrealized_local_eur` is the local gain at *today's* rate, and
`unrealized_fx_eur` is the revaluation of the original capital. Choosing the
same convention here is not cosmetic — the daily terms and the level terms
would otherwise disagree about the same holding and both be defensible, which
is the worst of the available outcomes.

It also means there is **no cross term**. The interaction between a price move
and a currency move is inside the currency leg by construction, because the
price move is converted at the closing rate. A three-way split with an
"interaction" bucket is a different convention, not a more precise one.

Cash and deposit balances carry a currency term and no local term: a lira
balance really does lose euro value when the lira falls. Income — distributions
and credited interest — is its own term, because a dividend is return that no
price move explains, and without it every ex-date would land in ``unexplained``
with the offsetting income nowhere.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from itertools import pairwise

from portfolio_tracker.money import ZERO

ONE = Decimal(1)

#: Linked parts must sum to the period return this closely. Carino is exact in
#: real arithmetic; this band is for Decimal's 28 significant digits accumulated
#: across a few hundred days, and is far tighter than a basis point.
LINKING_TOLERANCE = Decimal("0.0000001")


@dataclass(frozen=True)
class DayTerms:
    """One day's return, split. The parts sum to ``actual`` by construction."""

    on: date
    local: Decimal
    currency: Decimal
    income: Decimal
    unexplained: Decimal
    actual: Decimal

    @property
    def modelled(self) -> Decimal:
        return self.local + self.currency + self.income


@dataclass
class Attribution:
    """A period's return, split, with the parts summing to the whole."""

    start: date
    end: date
    total: Decimal
    local: Decimal
    currency: Decimal
    income: Decimal
    unexplained: Decimal
    linking: str = "carino"
    caveats: list[str] = field(default_factory=list)

    @property
    def residual(self) -> Decimal:
        """The linking gap. Zero, and reported so it can be checked."""
        return self.total - (self.local + self.currency + self.income + self.unexplained)


# ---------------------------------------------------------------------------
# Daily terms
# ---------------------------------------------------------------------------


def _positions_by_instrument(
    conn: sqlite3.Connection, person_id: int
) -> dict[int, dict[date, tuple[Decimal, Decimal, Decimal]]]:
    """Quantity, local price and FX rate per instrument per day."""
    out: dict[int, dict[date, tuple[Decimal, Decimal, Decimal]]] = {}
    for row in conn.execute(
        "SELECT p.instrument_id, p.date, p.quantity, p.price_local, p.fx_rate "
        "FROM position_daily p JOIN account a ON a.id = p.account_id "
        "WHERE a.person_id = ?",
        (person_id,),
    ):
        if row["price_local"] is None or row["fx_rate"] is None:
            continue
        series = out.setdefault(int(row["instrument_id"]), {})
        when = date.fromisoformat(row["date"])
        # One instrument can sit in two accounts; the price and rate are
        # properties of the instrument, so only the quantity accumulates.
        held, price, rate = series.get(when, (ZERO, row["price_local"], row["fx_rate"]))
        series[when] = (held + row["quantity"], price, rate)
    return out


def _balances_by_currency(
    conn: sqlite3.Connection, person_id: int
) -> dict[str, dict[date, tuple[Decimal, Decimal]]]:
    """Cash and deposit balances in local currency, with the day's rate.

    Both are folded together on purpose: for the currency term the only thing
    that matters is how many lira are sitting there, not which kind of account
    they sit in.
    """
    out: dict[str, dict[date, tuple[Decimal, Decimal]]] = {}
    for row in conn.execute(
        "SELECT c.date, c.currency, c.balance_local, c.fx_rate FROM cash_daily c "
        "JOIN account a ON a.id = c.account_id WHERE a.person_id = ? AND c.fx_rate IS NOT NULL",
        (person_id,),
    ):
        series = out.setdefault(row["currency"], {})
        when = date.fromisoformat(row["date"])
        held, _ = series.get(when, (ZERO, row["fx_rate"]))
        series[when] = (held + row["balance_local"], row["fx_rate"])
    return out


def _income_by_day(conn: sqlite3.Connection, person_id: int) -> dict[date, Decimal]:
    """Distributions and credited interest, per day, in euro.

    Return that no price move explains. Without this term a distribution's
    ex-date shows as a loss in ``local`` with its offsetting credit landing in
    ``unexplained`` — which is exactly the failure the distribution work fixed
    at the ledger level, reintroduced one layer up.
    """
    out: dict[date, Decimal] = {}
    for row in conn.execute(
        "SELECT t.trade_date, t.gross_amount_eur FROM transaction_ t "
        "JOIN account a ON a.id = t.account_id "
        "WHERE a.person_id = ? AND t.type IN ('DIVIDEND', 'INTEREST')",
        (person_id,),
    ):
        if row["gross_amount_eur"] is None:
            continue
        when = date.fromisoformat(row["trade_date"])
        out[when] = out.get(when, ZERO) + row["gross_amount_eur"]
    return out


def daily_terms(
    conn: sqlite3.Connection,
    *,
    person_id: int,
    days: list[date],
    basis: dict[date, Decimal],
    actual: dict[date, Decimal],
) -> dict[date, DayTerms]:
    """Split each day's portfolio return into its sources.

    ``basis`` is the valuation basis per day and ``actual`` the day's return
    from unit accounting. The modelled terms are computed independently and the
    remainder is named rather than distributed, so the sum is an identity on
    every single day before any linking happens.
    """
    income = _income_by_day(conn, person_id)
    per_holding = holding_terms(conn, person_id=person_id, days=days, basis=basis)

    out: dict[date, DayTerms] = {}
    for today, holdings in per_holding.items():
        if today not in actual:
            continue
        local = sum((legs[0] for legs in holdings.values()), ZERO)
        currency = sum((legs[1] for legs in holdings.values()), ZERO)
        earned = income.get(today, ZERO) / basis[_previous(days, today)]
        observed = actual[today]
        out[today] = DayTerms(
            on=today,
            local=local,
            currency=currency,
            income=earned,
            unexplained=observed - local - currency - earned,
            actual=observed,
        )
    return out


def _previous(days: list[date], today: date) -> date:
    index = days.index(today)
    return days[index - 1] if index else today


#: Cash and deposit balances have no instrument, and a lira balance still loses
#: euro value when the lira falls. They get a line of their own rather than
#: being spread across the holdings, which would attribute a currency move to
#: assets that did not make it.
CASH_KEY = "(cash & deposits)"


def holding_terms(
    conn: sqlite3.Connection,
    *,
    person_id: int,
    days: list[date],
    basis: dict[date, Decimal],
) -> dict[date, dict[str, tuple[Decimal, Decimal]]]:
    """Each day's local and currency contribution, **per holding**.

    The same arithmetic the portfolio-level split uses, kept at the level it is
    computed at instead of being summed away. Every figure here is a share of
    that day's opening portfolio value, so the holdings add up to the portfolio
    by construction rather than by coincidence.
    """
    positions = _positions_by_instrument(conn, person_id)
    balances = _balances_by_currency(conn, person_id)
    names = {
        int(row["id"]): str(row["import_key"])
        for row in conn.execute("SELECT id, import_key FROM instrument")
    }

    out: dict[date, dict[str, tuple[Decimal, Decimal]]] = {}
    for previous, today in pairwise(days):
        opening = basis.get(previous, ZERO)
        if opening <= ZERO:
            continue
        legs: dict[str, tuple[Decimal, Decimal]] = {}

        for instrument_id, priced in positions.items():
            opened, closed = priced.get(previous), priced.get(today)
            # A position opened or closed today moved by a trade, not by a
            # price. Its whole effect belongs in `unexplained`, which is what
            # skipping it here achieves.
            if opened is None or closed is None:
                continue
            quantity, price_p, rate_p = opened
            _, price_t, rate_t = closed
            if quantity == ZERO or rate_t <= ZERO or rate_p <= ZERO:
                continue
            local = quantity * (price_t - price_p) / rate_t / opening
            currency = quantity * price_p * (ONE / rate_t - ONE / rate_p) / opening
            key = names.get(instrument_id, str(instrument_id))
            before = legs.get(key, (ZERO, ZERO))
            legs[key] = (before[0] + local, before[1] + currency)

        for held in balances.values():
            was, now = held.get(previous), held.get(today)
            if was is None or now is None:
                continue
            balance, opening_rate = was
            _, closing_rate = now
            if balance == ZERO or closing_rate <= ZERO or opening_rate <= ZERO:
                continue
            # No local term: a euro is a euro. A lira balance still moves.
            currency = balance * (ONE / closing_rate - ONE / opening_rate) / opening
            before = legs.get(CASH_KEY, (ZERO, ZERO))
            legs[CASH_KEY] = (before[0], before[1] + currency)

        out[today] = legs
    return out


# ---------------------------------------------------------------------------
# Carino linking
# ---------------------------------------------------------------------------


def carino_factor(period_return: Decimal) -> Decimal:
    """``K = ln(1+R)/R``, the constant that makes the sum come out right.

    ``R = 0`` is the removable singularity: the limit is 1, and a portfolio that
    ended exactly where it started is not a degenerate case worth refusing.
    """
    if period_return == ZERO:
        return ONE
    return (ONE + period_return).ln() / period_return


def link(terms: list[DayTerms]) -> tuple[Decimal, dict[str, Decimal]]:
    """Carino-smooth daily contributions onto the compounded period return.

    Each day's contributions are scaled by ``k_t / K``, where ``k_t`` is that
    day's own factor. The result is exact rather than approximate::

        Σ_t R_t * k_t/K  =  Σ_t ln(1+R_t) / K  =  ln(Π(1+R_t)) / K  =  R

    so any decomposition that sums to ``R_t`` each day sums to ``R`` over the
    period. That is the whole reason a residual is no longer needed: it is not
    allocated away, it never arises.
    """
    compounded = ONE
    for day in terms:
        compounded *= ONE + day.actual
    total = compounded - ONE

    outer = carino_factor(total)
    parts = {"local": ZERO, "currency": ZERO, "income": ZERO, "unexplained": ZERO}
    if outer == ZERO:  # pragma: no cover - only if R is such that ln(1+R) is 0
        return total, parts

    for day in terms:
        scale = carino_factor(day.actual) / outer
        parts["local"] += day.local * scale
        parts["currency"] += day.currency * scale
        parts["income"] += day.income * scale
        parts["unexplained"] += day.unexplained * scale
    return total, parts


# ---------------------------------------------------------------------------
# The public call
# ---------------------------------------------------------------------------


def summarise(
    conn: sqlite3.Connection,
    *,
    person_id: int,
    start: date | None = None,
    end: date | None = None,
) -> Attribution | None:
    """Attribute the period's return, reading the stored daily contributions.

    Returns ``None`` when there is nothing to attribute rather than a zeroed
    report: no history and a flat year are different answers, and a table of
    zeroes reads like the second.
    """
    query = (
        "SELECT date, daily_return, ret_local_contrib, ret_fx_contrib, "
        "ret_income_contrib, ret_residual FROM portfolio_daily WHERE person_id = ? "
        "AND daily_return IS NOT NULL AND ret_local_contrib IS NOT NULL"
    )
    params: list[object] = [person_id]
    if start:
        query += " AND date >= ?"
        params.append(start.isoformat())
    if end:
        query += " AND date <= ?"
        params.append(end.isoformat())
    rows = conn.execute(query + " ORDER BY date", tuple(params)).fetchall()
    if not rows:
        return None

    terms = [
        DayTerms(
            on=date.fromisoformat(r["date"]),
            local=r["ret_local_contrib"],
            currency=r["ret_fx_contrib"],
            income=r["ret_income_contrib"] or ZERO,
            unexplained=r["ret_residual"] or ZERO,
            actual=r["daily_return"],
        )
        for r in rows
    ]

    try:
        total, parts = link(terms)
    except (InvalidOperation, ValueError):  # pragma: no cover - a total wipeout
        return None

    report = Attribution(
        start=terms[0].on,
        end=terms[-1].on,
        total=total,
        local=parts["local"],
        currency=parts["currency"],
        income=parts["income"],
        unexplained=parts["unexplained"],
    )
    report.caveats = _caveats(report, terms)
    return report


@dataclass(frozen=True)
class HoldingContribution:
    """One holding's share of the period return, in percentage points."""

    instrument: str
    local: Decimal
    currency: Decimal

    @property
    def total(self) -> Decimal:
        return self.local + self.currency


@dataclass
class HoldingAttribution:
    """Who produced the return, rather than what kind of thing produced it."""

    start: date
    end: date
    total: Decimal
    holdings: list[HoldingContribution]
    unexplained: Decimal
    income: Decimal
    caveats: list[str] = field(default_factory=list)

    @property
    def accounted(self) -> Decimal:
        return sum((h.total for h in self.holdings), ZERO) + self.unexplained + self.income

    @property
    def residual(self) -> Decimal:
        return self.total - self.accounted


def by_holding(
    conn: sqlite3.Connection,
    *,
    person_id: int = 1,
    start: date | None = None,
    end: date | None = None,
) -> HoldingAttribution | None:
    """Which holdings produced the period's return, Cariño-linked.

    The portfolio-level split says the return came 82 points from prices and
    -26 from currency. This says *which holdings* those points came from -- the
    level at which the owner actually makes decisions, since nobody can sell
    "currency".

    The same smoothing factors are applied, so these sum to the portfolio total
    exactly, alongside income and the unexplained remainder. Recomputed on read
    rather than stored: the daily figures already exist in ``portfolio_daily``
    and a second copy of a derived number is a second thing that can drift.
    """
    # Every row, including the inception day that has no return of its own.
    # Filtering it out here would drop the *second* day's contribution too --
    # it is the one paired with inception -- and the holdings would then sum to
    # slightly more than the portfolio, for a reason nothing would explain.
    rows = conn.execute(
        "SELECT date, daily_return, valuation_basis_eur, ret_income_contrib, ret_residual "
        "FROM portfolio_daily WHERE person_id = ? ORDER BY date",
        (person_id,),
    ).fetchall()
    if not rows:
        return None

    days = [date.fromisoformat(r["date"]) for r in rows]
    basis = {date.fromisoformat(r["date"]): Decimal(str(r["valuation_basis_eur"])) for r in rows}
    actual = {
        date.fromisoformat(r["date"]): Decimal(str(r["daily_return"]))
        for r in rows
        if r["daily_return"] is not None
    }
    extra = {
        date.fromisoformat(r["date"]): (
            Decimal(str(r["ret_income_contrib"] or 0)),
            Decimal(str(r["ret_residual"] or 0)),
        )
        for r in rows
    }

    window = [
        d for d in days if d in actual and (not start or d >= start) and (not end or d <= end)
    ]
    # One day is a period. `summarise` accepts one row and these two must agree
    # about what is reportable, or the page shows a portfolio split with no
    # holdings beneath it.
    if not window:
        return None

    per_day = holding_terms(conn, person_id=person_id, days=days, basis=basis)

    compounded = ONE
    for day in window:
        compounded *= ONE + actual[day]
    total = compounded - ONE
    outer = carino_factor(total)
    if outer == ZERO:  # pragma: no cover - only if ln(1+R) is exactly 0
        return None

    legs: dict[str, tuple[Decimal, Decimal]] = {}
    income = unexplained = ZERO
    for day in window:
        scale = carino_factor(actual[day]) / outer
        for key, (local, currency) in per_day.get(day, {}).items():
            before = legs.get(key, (ZERO, ZERO))
            legs[key] = (before[0] + local * scale, before[1] + currency * scale)
        earned, residual = extra.get(day, (ZERO, ZERO))
        income += earned * scale
        unexplained += residual * scale

    holdings = sorted(
        (
            HoldingContribution(instrument=key, local=local, currency=currency)
            for key, (local, currency) in legs.items()
        ),
        key=lambda h: -h.total,
    )
    report = HoldingAttribution(
        start=window[0],
        end=window[-1],
        total=total,
        holdings=holdings,
        unexplained=unexplained,
        income=income,
    )
    report.caveats = [
        "Percentage points of the portfolio's return, not each holding's own return. "
        "A holding that doubled while being 1% of the portfolio contributes about one "
        "point, and that is the number that matters for the total.",
        "Weighted by what was held on each day, so a position bought late contributes "
        "only from the day it was bought.",
        "A position opened or closed on a given day contributes nothing for that day: "
        "it moved by a trade, not by a price. That lands in 'trading'.",
    ]
    return report


def _caveats(report: Attribution, terms: list[DayTerms]) -> list[str]:
    out = [
        "Contributions are Carino-smoothed onto the compounded return, so the parts "
        "sum to the whole exactly. Nothing is allocated away to make them.",
        "The currency leg is the opening capital re-translated; the price leg is "
        "converted at the closing rate. There is no cross term to distribute.",
    ]
    if report.total != ZERO and abs(report.unexplained / report.total) > Decimal("0.05"):
        out.append(
            f"{report.unexplained:.2%} of the return is unexplained by prices, currency "
            "or income. That is trading: purchases and sales executed away from the "
            "closing price, fees, and quantities that changed within a day. It is a "
            "real part of the return, not a rounding error."
        )
    flat = sum(1 for t in terms if t.currency == ZERO)
    if flat == len(terms):
        out.append(
            "Every holding is euro-quoted, so the currency leg is zero by construction. "
            "That is a statement about listing venue, not about risk -- see "
            "/api/allocation?basis=look_through."
        )
    return out
