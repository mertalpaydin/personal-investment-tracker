"""Returns, via unit accounting.

The portfolio is modelled as a fund. Each day it has a value and a number of
units; contributions buy units at that day's price rather than counting as
growth. The unit price *is* the time-weighted return index, and it is correct
in the presence of contributions -- which a naive value-over-value ratio is not.

Two questions, two different answers, and conflating them is the usual way a
tracker flatters its owner:

**TWR** -- how did the investments perform? Contributions are neutralised, so
adding EUR 10,000 is not a 10% gain. This is the number to judge decisions by.

**XIRR** -- how did *the owner* do, given when money went in? Sensitive to
timing. Lower than TWR means money arrived before bad stretches.

The gap between them is `timing_gap`, and it is the honest measure of whether
contribution timing helped or hurt.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from portfolio_tracker.core import attribution
from portfolio_tracker.core.calendars import TARGET2, grid_days
from portfolio_tracker.money import ZERO, quantize_money

#: Unit price at inception. Arbitrary, conventional, and it makes the index
#: readable at a glance: 137 means up 37%.
INCEPTION_UNIT_PRICE = Decimal(100)

#: Trading days per year, for annualising volatility.
TRADING_DAYS = 252

#: The unit identity is asserted daily. A break beyond this means the
#: accounting has drifted from the values it is supposed to describe.
UNIT_IDENTITY_TOLERANCE = Decimal("0.01")


class UnitAccountingError(RuntimeError):
    """The unit series no longer reconciles with the portfolio's value."""


@dataclass(frozen=True)
class DayState:
    on: date
    value_eur: Decimal
    external_flow_eur: Decimal
    units_issued: Decimal
    units_outstanding: Decimal
    unit_price: Decimal
    daily_return: Decimal | None


@dataclass
class ReturnSummary:
    start: date
    end: date
    days: int
    twr: Decimal | None = None
    twr_annualised: Decimal | None = None
    xirr: Decimal | None = None
    xirr_reliability: str = "low"
    timing_gap: Decimal | None = None
    volatility: Decimal | None = None
    max_drawdown: Decimal | None = None
    unit_price_start: Decimal | None = None
    unit_price_end: Decimal | None = None
    external_flows_eur: Decimal = ZERO
    caveats: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Unit accounting
# ---------------------------------------------------------------------------


def build_units(days: list[tuple[date, Decimal, Decimal]]) -> list[DayState]:
    """Turn (date, value, external flow) into a unit series.

    Pure. The rule that makes a contribution not look like a return::

        price_before_flow = (value - flow) / units_held
        units_issued = flow / price_before_flow

    so the new money buys units at the price prevailing before it arrived, and
    the unit price is unchanged by the act of contributing.
    """
    states: list[DayState] = []
    units = ZERO
    previous_price: Decimal | None = None

    for when, value, flow in days:
        if previous_price is None:
            # Inception: the portfolio starts with whatever it starts with.
            # No return is computed for this day -- there is nothing to compare.
            if value <= ZERO:
                continue
            units = value / INCEPTION_UNIT_PRICE
            price = INCEPTION_UNIT_PRICE
            issued = units
            daily = None
        else:
            pre_flow_value = value - flow
            if units <= ZERO:
                # Everything was withdrawn and money is arriving again; restart
                # the unit count without inventing a return across the gap.
                units = value / previous_price if previous_price > ZERO else ZERO
                price = previous_price
                issued = units
                daily = ZERO
            else:
                price_before = pre_flow_value / units
                issued = (flow / price_before) if price_before > ZERO else ZERO
                units += issued
                price = (value / units) if units > ZERO else previous_price
                daily = (price / previous_price - 1) if previous_price > ZERO else None

        states.append(
            DayState(
                on=when,
                value_eur=value,
                external_flow_eur=flow,
                units_issued=issued,
                units_outstanding=units,
                unit_price=price,
                daily_return=daily,
            )
        )
        previous_price = price

    return states


def check_unit_identity(states: list[DayState]) -> list[str]:
    """``value == units x unit_price`` on every day.

    Cheap to assert and the fastest way to notice the series has drifted from
    the values it claims to describe.
    """
    breaks = []
    for state in states:
        implied = state.units_outstanding * state.unit_price
        if abs(implied - state.value_eur) > UNIT_IDENTITY_TOLERANCE:
            breaks.append(
                f"{state.on}: units x price = {implied:.2f} but value = {state.value_eur:.2f}"
            )
    return breaks


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def twr(states: list[DayState]) -> Decimal | None:
    """Total time-weighted return over the whole series."""
    if len(states) < 2:
        return None
    first, last = states[0].unit_price, states[-1].unit_price
    return (last / first - 1) if first > ZERO else None


def annualise(total_return: Decimal, days: int) -> Decimal | None:
    """Compound a period return to a yearly rate.

    Returns None for very short windows: annualising a fortnight produces a
    number with the shape of a forecast and none of the substance.
    """
    if days < 30 or total_return <= Decimal(-1):
        return None
    growth = float(1 + total_return) ** (365.0 / days) - 1
    return Decimal(str(round(growth, 8)))


def xirr(
    flows: list[tuple[date, Decimal]], *, guess: float = 0.1, tolerance: float = 1e-7
) -> Decimal | None:
    """Money-weighted return: the rate making the flows net to zero.

    Newton's method, falling back to bisection when it wanders -- the function
    is not always well behaved, and silently returning a diverged value would
    be worse than returning nothing.
    """
    if len(flows) < 2:
        return None
    ordered = sorted(flows, key=lambda pair: pair[0])
    origin = ordered[0][0]
    points = [((when - origin).days / 365.0, float(amount)) for when, amount in ordered]

    if not (any(a > 0 for _, a in points) and any(a < 0 for _, a in points)):
        return None  # all one sign: no rate can make them balance

    def npv(rate: float) -> float:
        total: float = 0.0
        for years, amount in points:
            total += amount / (1.0 + rate) ** years
        return total

    rate = guess
    for _ in range(100):
        value = npv(rate)
        derivative = sum(-years * amount / (1.0 + rate) ** (years + 1) for years, amount in points)
        if abs(derivative) < 1e-12:
            break
        step = value / derivative
        rate -= step
        if rate <= -0.999999:
            break
        if abs(step) < tolerance:
            return Decimal(str(round(rate, 8)))

    low, high = -0.9999, 10.0
    if npv(low) * npv(high) > 0:
        return None
    for _ in range(300):
        mid = (low + high) / 2
        if npv(low) * npv(mid) <= 0:
            high = mid
        else:
            low = mid
    return Decimal(str(round((low + high) / 2, 8)))


def volatility(states: list[DayState]) -> Decimal | None:
    """Annualised standard deviation of daily unit-price returns."""
    daily = [float(s.daily_return) for s in states if s.daily_return is not None]
    if len(daily) < 20:
        return None
    mean = sum(daily) / len(daily)
    variance = sum((r - mean) ** 2 for r in daily) / (len(daily) - 1)
    return Decimal(str(round(variance**0.5 * TRADING_DAYS**0.5, 8)))


def max_drawdown(states: list[DayState]) -> Decimal | None:
    """Deepest peak-to-trough fall in the unit price.

    Measured on the unit price rather than on portfolio value, so a withdrawal
    is not mistaken for a loss.
    """
    if len(states) < 2:
        return None
    peak = states[0].unit_price
    worst = ZERO
    for state in states[1:]:
        peak = max(peak, state.unit_price)
        if peak > ZERO:
            worst = min(worst, state.unit_price / peak - 1)
    return worst


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


class DayValues(NamedTuple):
    """One day's inputs, named because six positional Decimals is a trap."""

    market: Decimal
    cash: Decimal
    cost: Decimal
    realized: Decimal
    stale: int
    deposits: Decimal
    #: The part of `cash` that is realised income rather than capital waiting
    #: to be invested. Not added to anything -- reported beside it.
    income_cash: Decimal
    #: All income ever credited, whether or not it is still sitting in cash.
    income_received: Decimal


def _daily_values(
    conn: sqlite3.Connection, person_id: int, days: list[date]
) -> dict[date, DayValues]:
    """Per day: positions, brokerage cash and its income share, cost, and the rest."""
    positions: dict[date, list[Decimal]] = {}
    stale: dict[date, int] = {}
    for row in conn.execute(
        """
        SELECT p.date, p.market_value_eur, p.cost_basis_eur, p.valuation_status
        FROM position_daily p
        JOIN account a ON a.id = p.account_id
        WHERE a.person_id = ?
        """,
        (person_id,),
    ):
        when = date.fromisoformat(row["date"])
        entry = positions.setdefault(when, [ZERO, ZERO])
        entry[0] += row["market_value_eur"]
        entry[1] += row["cost_basis_eur"]
        if row["valuation_status"] != "OK":
            stale[when] = stale.get(when, 0) + 1

    # A deposit account's balance is money the owner knows he has, so it
    # belongs in the headline total. Cash left in a brokerage account after a
    # sale or a distribution does not: the tracker cannot tell whether it is
    # still there or has been spent.
    cash: dict[date, Decimal] = {}
    deposits: dict[date, Decimal] = {}
    income_cash: dict[date, Decimal] = {}
    for row in conn.execute(
        """
        SELECT c.date, c.balance_eur, c.income_eur, a.kind FROM cash_daily c
        JOIN account a ON a.id = c.account_id
        WHERE a.person_id = ? AND c.balance_eur IS NOT NULL
        """,
        (person_id,),
    ):
        when = date.fromisoformat(row["date"])
        is_deposit = row["kind"] in ("TAGESGELD", "FESTGELD")
        bucket = deposits if is_deposit else cash
        bucket[when] = bucket.get(when, ZERO) + row["balance_eur"]
        if not is_deposit and row["income_eur"] is not None:
            income_cash[when] = income_cash.get(when, ZERO) + row["income_eur"]

    # Income *received*, running. Distinct from the income still sitting in
    # cash: a reinvested dividend leaves the balance but was still earned, and
    # a tool that reports only the balance makes the income stream vanish the
    # moment it is put back to work.
    income_received: dict[date, Decimal] = {}
    running_income = ZERO
    for row in conn.execute(
        """
        SELECT t.trade_date, t.gross_amount_eur FROM transaction_ t
        JOIN account a ON a.id = t.account_id
        WHERE a.person_id = ? AND t.type IN ('DIVIDEND', 'INTEREST')
        ORDER BY t.trade_date
        """,
        (person_id,),
    ):
        if row["gross_amount_eur"] is not None:
            running_income += row["gross_amount_eur"]
        income_received[date.fromisoformat(row["trade_date"])] = running_income

    realized: dict[date, Decimal] = {}
    running = ZERO
    for row in conn.execute(
        """
        SELECT t.trade_date, d.gross_gain_eur FROM lot_disposal d
        JOIN transaction_ t ON t.id = d.sell_txn_id
        JOIN account a ON a.id = t.account_id
        WHERE a.person_id = ? ORDER BY t.trade_date
        """,
        (person_id,),
    ):
        if row["gross_gain_eur"] is not None:
            running += row["gross_gain_eur"]
        realized[date.fromisoformat(row["trade_date"])] = running

    out: dict[date, DayValues] = {}
    carried = ZERO
    carried_income = ZERO
    for day in days:
        market, cost = positions.get(day, [ZERO, ZERO])
        carried = realized.get(day, carried)
        carried_income = income_received.get(day, carried_income)
        out[day] = DayValues(
            market=market,
            cash=cash.get(day, ZERO),
            cost=cost,
            realized=carried,
            stale=stale.get(day, 0),
            deposits=deposits.get(day, ZERO),
            income_cash=income_cash.get(day, ZERO),
            income_received=carried_income,
        )
    return out


def _external_flows(
    conn: sqlite3.Connection, person_id: int, days: list[date]
) -> dict[date, Decimal]:
    """External flows, mapped onto the valuation grid.

    A flow dated off-grid is rolled forward to the next grid day rather than
    dropped. Some trade dates in the source really do fall at weekends, and
    silently discarding those flows makes a contribution look like growth --
    which inflated the time-weighted return to 1326% before this was caught.
    """
    if not days:
        return {}
    grid = sorted(days)
    flows: dict[date, Decimal] = {}

    for row in conn.execute(
        """
        SELECT t.trade_date, t.flow_class, t.gross_amount_eur
        FROM transaction_ t JOIN account a ON a.id = t.account_id
        WHERE a.person_id = ? AND t.flow_class IN ('EXTERNAL_IN', 'EXTERNAL_OUT')
        """,
        (person_id,),
    ):
        if row["gross_amount_eur"] is None:
            continue
        when = date.fromisoformat(row["trade_date"])
        signed = row["gross_amount_eur"]
        if row["flow_class"] == "EXTERNAL_OUT":
            signed = -signed

        # Before the window opens, it lands on the first day; otherwise on the
        # first grid day that can reflect it.
        landed = next((day for day in grid if day >= when), grid[-1])
        flows[landed] = flows.get(landed, ZERO) + signed

    return flows


def total_external_flows(conn: sqlite3.Connection, person_id: int) -> Decimal:
    """Every external flow in the ledger, regardless of the grid."""
    total = ZERO
    for row in conn.execute(
        "SELECT t.flow_class, t.gross_amount_eur FROM transaction_ t "
        "JOIN account a ON a.id = t.account_id "
        "WHERE a.person_id = ? AND t.flow_class IN ('EXTERNAL_IN', 'EXTERNAL_OUT')",
        (person_id,),
    ):
        if row["gross_amount_eur"] is None:
            continue
        total += (
            -row["gross_amount_eur"]
            if row["flow_class"] == "EXTERNAL_OUT"
            else row["gross_amount_eur"]
        )
    return total


def build_portfolio_daily(
    conn: sqlite3.Connection, *, person_id: int, end: date, start: date | None = None
) -> list[str]:
    """Aggregate positions and cash into the daily unit series. Returns breaks."""
    bounds = conn.execute(
        "SELECT min(date) AS lo FROM position_daily p JOIN account a ON a.id = p.account_id "
        "WHERE a.person_id = ?",
        (person_id,),
    ).fetchone()
    if bounds is None or bounds["lo"] is None:
        return []

    window_start = start or date.fromisoformat(bounds["lo"])
    days = grid_days(window_start, end, TARGET2)
    values = _daily_values(conn, person_id, days)
    flows = _external_flows(conn, person_id, days)

    def basis(day: date) -> Decimal:
        """What the return maths runs on: everything the portfolio holds."""
        v = values[day]
        return v.market + v.cash + v.deposits

    series = [(day, basis(day), flows.get(day, ZERO)) for day in days if basis(day) > ZERO]
    states = build_units(series)
    breaks = check_unit_identity(states)

    # Where each day's return came from. Computed from the same stored prices
    # and rates the valuation used, and the part they do not explain is named
    # rather than distributed -- see core/attribution.py.
    terms = attribution.daily_terms(
        conn,
        person_id=person_id,
        days=[state.on for state in states],
        basis={state.on: state.value_eur for state in states},
        actual={s.on: s.daily_return for s in states if s.daily_return is not None},
    )

    # Every euro that crossed the boundary must appear in the series. A flow
    # that goes missing reads as growth, so this is checked rather than
    # assumed -- it is how the 1326% figure was caught.
    carried = sum((state.external_flow_eur for state in states), ZERO)
    expected = total_external_flows(conn, person_id)
    if abs(carried - expected) > Decimal("0.05"):
        breaks.append(
            f"external flows do not reconcile: {carried:.2f} in the series against "
            f"{expected:.2f} in the ledger -- a contribution has been dropped and "
            "would be reported as return"
        )

    conn.execute(
        "DELETE FROM portfolio_daily WHERE person_id = ? AND date BETWEEN ? AND ?",
        (person_id, window_start.isoformat(), end.isoformat()),
    )

    peak = states[0].unit_price if states else ZERO
    for state in states:
        v = values[state.on]
        # Headline: what the owner has. Basis: what the return maths needs.
        headline = v.market + v.deposits
        peak = max(peak, state.unit_price)
        conn.execute(
            """
            INSERT INTO portfolio_daily
                (date, person_id, market_value_eur, accrued_income_eur, total_value_eur,
                 cost_basis_eur, cash_eur, deposits_eur, valuation_basis_eur,
                 income_cash_eur, income_received_eur, realized_gain_eur,
                 net_external_flow_eur, units_issued, units_outstanding, unit_price,
                 daily_return, drawdown_pct, completeness, stale_instruments,
                 ret_local_contrib, ret_fx_contrib, ret_income_contrib, ret_residual)
            VALUES (?, ?, ?, '0', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?)
            """,
            (
                state.on.isoformat(),
                person_id,
                quantize_money(v.market),
                quantize_money(headline),
                quantize_money(v.cost),
                quantize_money(v.cash),
                quantize_money(v.deposits),
                quantize_money(state.value_eur),
                quantize_money(v.income_cash),
                quantize_money(v.income_received),
                quantize_money(v.realized),
                quantize_money(state.external_flow_eur),
                state.units_issued,
                state.units_outstanding,
                state.unit_price,
                state.daily_return,
                (state.unit_price / peak - 1) if peak > ZERO else None,
                "PARTIAL" if v.stale else "FULL",
                v.stale,
                # NULL on the inception day and on any day with no computable
                # return. Zero would claim the return was attributed and found
                # to be nothing, which is a different statement.
                *_attribution_row(terms.get(state.on)),
            ),
        )
    return breaks


def _attribution_row(
    terms: attribution.DayTerms | None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
    if terms is None:
        return (None, None, None, None)
    return (terms.local, terms.currency, terms.income, terms.unexplained)


def summarise(
    conn: sqlite3.Connection, *, person_id: int, start: date | None = None, end: date | None = None
) -> ReturnSummary:
    """Headline metrics, with the caveats that qualify them."""
    query = "SELECT * FROM portfolio_daily WHERE person_id = ?"
    params: list[object] = [person_id]
    if start:
        query += " AND date >= ?"
        params.append(start.isoformat())
    if end:
        query += " AND date <= ?"
        params.append(end.isoformat())
    rows = conn.execute(query + " ORDER BY date", tuple(params)).fetchall()

    if not rows:
        return ReturnSummary(start=start or date.today(), end=end or date.today(), days=0)

    states = [
        DayState(
            on=date.fromisoformat(r["date"]),
            value_eur=r["total_value_eur"],
            external_flow_eur=r["net_external_flow_eur"],
            units_issued=r["units_issued"],
            units_outstanding=r["units_outstanding"],
            unit_price=r["unit_price"],
            daily_return=r["daily_return"],
        )
        for r in rows
    ]

    first, last = states[0], states[-1]
    span = (last.on - first.on).days
    total = twr(states)

    flows: list[tuple[date, Decimal]] = [
        (s.on, -s.external_flow_eur) for s in states if s.external_flow_eur != ZERO
    ]
    flows.append((last.on, last.value_eur))
    money_weighted = xirr(flows)

    annualised = annualise(total, span) if total is not None else None
    # Both are yearly rates, so the difference is meaningful: negative means
    # money arrived before the weaker stretches.
    gap = (money_weighted - annualised) if (money_weighted and annualised) else None

    summary = ReturnSummary(
        start=first.on,
        end=last.on,
        days=span,
        twr=total,
        twr_annualised=annualised,
        xirr=money_weighted,
        timing_gap=gap,
        volatility=volatility(states),
        max_drawdown=max_drawdown(states),
        unit_price_start=first.unit_price,
        unit_price_end=last.unit_price,
        external_flows_eur=sum((s.external_flow_eur for s in states), ZERO),
    )

    reconstructed = conn.execute(
        "SELECT count(*) n FROM transaction_ t JOIN account a ON a.id = t.account_id "
        "WHERE a.person_id = ? AND t.flow_class IN ('EXTERNAL_IN', 'EXTERNAL_OUT') "
        "AND t.confidence = 'RECONSTRUCTED'",
        (person_id,),
    ).fetchone()["n"]
    if reconstructed:
        summary.caveats.append(
            f"{reconstructed} contribution(s) were inferred from purchases rather than "
            "recorded, so their dates are the days the money was invested, not the days "
            "it arrived. XIRR is therefore approximate; TWR is unaffected."
        )
    summary.xirr_reliability = "low" if reconstructed else "high"

    no_fees = conn.execute(
        "SELECT count(*) n FROM transaction_ WHERE fees_known = 0 AND type IN ('BUY', 'SELL')"
    ).fetchone()["n"]
    if no_fees:
        summary.caveats.append(
            f"{no_fees} trade(s) have no fee data; returns are overstated by the "
            "fees actually paid."
        )
    distributions = conn.execute(
        "SELECT count(*) AS n FROM transaction_ t JOIN account a ON a.id = t.account_id "
        "WHERE a.person_id = ? AND t.type = 'DIVIDEND'",
        (person_id,),
    ).fetchone()["n"]
    if distributions:
        summary.caveats.append(
            f"{distributions} distribution(s) are included, taken from the price feed and "
            "gross of the withholding tax the bank applies."
        )
    else:
        summary.caveats.append(
            "No distributions are recorded. If any holding pays income, its ex-date price "
            "fall is currently being counted as a loss."
        )

    summary.caveats.append(
        "The headline total counts holdings and deposit balances only. Uninvested cash from "
        "sales and distributions is shown separately, because whether it is still held "
        "cannot be known from the source. Returns are computed on the complete value "
        "including that cash -- otherwise a sale would read as a loss."
    )
    return summary
