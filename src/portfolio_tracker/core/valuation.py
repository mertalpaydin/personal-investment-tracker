"""Daily revaluation of every position.

Two prices matter on any given day and they are not the same number: the
instrument's price in its own currency, and the exchange rate. Both are
forward-filled independently, against separate calendars, and each carries its
own staleness count -- because a foreign fund's euro value moves when *either*
of them moves, and conflating them hides which.

The output supports a decomposition that is exact at the level of a position,
with no cross term::

    unrealized_eur       = market_value_eur - cost_basis_eur
                           (cost frozen at the acquisition-date rate)
    unrealized_local_eur = market_value_eur - cost_basis_eur_at_spot
                           (this is the figure the source export shows)
    unrealized_fx_eur    = cost_basis_eur_at_spot - cost_basis_eur

    => unrealized_eur = unrealized_local_eur + unrealized_fx_eur

The middle line is the number the broker export reports. Storing all three is
what lets the tool say "your export shows X; the real euro figure is Y; the
difference is currency", rather than silently disagreeing with a number the
user can see elsewhere.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from portfolio_tracker.core import fx
from portfolio_tracker.core.calendars import TARGET2, business_days_between, grid_days
from portfolio_tracker.money import ZERO, quantize_money

#: Beyond this many of the instrument's own trading days, a carried-forward
#: price stops being a stand-in and becomes an invention.
MAX_STALE_DAYS = 5

#: A stale price on a position this large is escalated regardless of the
#: staleness limit: losing the biggest holding for a few days is not a warning.
MATERIAL_POSITION_PCT = Decimal("1.0")


class StalePriceError(RuntimeError):
    """A price is too old to stand in for the valuation date."""


@dataclass
class ValuationReport:
    days: int = 0
    rows: int = 0
    stale: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


@dataclass(frozen=True)
class PositionValue:
    """The pure arithmetic, extracted so it can be tested without a database."""

    market_value_local: Decimal
    market_value_eur: Decimal
    cost_basis_local: Decimal
    cost_basis_eur: Decimal
    cost_basis_eur_at_spot: Decimal
    unrealized_eur: Decimal
    unrealized_local_eur: Decimal
    unrealized_fx_eur: Decimal


def value_position(
    quantity: Decimal,
    price_local: Decimal,
    fx_rate: Decimal,
    cost_local: Decimal,
    cost_eur_frozen: Decimal,
) -> PositionValue:
    """Value one position, splitting the gain into local and currency parts.

    Pure. The identity ``unrealized_eur == unrealized_local_eur +
    unrealized_fx_eur`` holds by construction here, and is asserted by test.
    """
    market_local = quantity * price_local
    market_eur = quantize_money(fx.to_eur(market_local, fx_rate))
    cost_at_spot = quantize_money(fx.to_eur(cost_local, fx_rate))

    unrealized_eur = market_eur - cost_eur_frozen
    unrealized_fx = cost_at_spot - cost_eur_frozen
    return PositionValue(
        market_value_local=quantize_money(market_local),
        market_value_eur=market_eur,
        cost_basis_local=quantize_money(cost_local),
        cost_basis_eur=cost_eur_frozen,
        cost_basis_eur_at_spot=cost_at_spot,
        unrealized_eur=unrealized_eur,
        unrealized_local_eur=market_eur - cost_at_spot,
        unrealized_fx_eur=unrealized_fx,
    )


@dataclass
class _Holding:
    """A lot's quantity over time, as a series of dated changes."""

    account_id: int
    instrument_id: int
    currency: str
    cost_per_unit_local: Decimal
    cost_per_unit_eur: Decimal | None
    events: list[tuple[date, Decimal]] = field(default_factory=list)

    def quantity_on(self, day: date) -> Decimal:
        return sum((qty for when, qty in self.events if when <= day), ZERO)


def _load_holdings(conn: sqlite3.Connection) -> list[_Holding]:
    """One entry per lot, with its opening and every disposal against it."""
    holdings: dict[int, _Holding] = {}
    for row in conn.execute(
        "SELECT id, account_id, instrument_id, currency, open_date, qty_opened, "
        "cost_per_unit_local, cost_per_unit_eur FROM lot"
    ):
        holdings[int(row["id"])] = _Holding(
            account_id=int(row["account_id"]),
            instrument_id=int(row["instrument_id"]),
            currency=row["currency"],
            cost_per_unit_local=Decimal(str(row["cost_per_unit_local"])),
            cost_per_unit_eur=(
                Decimal(str(row["cost_per_unit_eur"]))
                if row["cost_per_unit_eur"] is not None
                else None
            ),
            events=[(date.fromisoformat(row["open_date"]), Decimal(str(row["qty_opened"])))],
        )

    for row in conn.execute(
        "SELECT d.lot_id, d.qty, t.trade_date FROM lot_disposal d "
        "JOIN transaction_ t ON t.id = d.sell_txn_id"
    ):
        holding = holdings.get(int(row["lot_id"]))
        if holding is not None:
            holding.events.append(
                (date.fromisoformat(row["trade_date"]), -Decimal(str(row["qty"])))
            )

    return list(holdings.values())


def _price_series(conn: sqlite3.Connection, instrument_id: int) -> list[tuple[date, Decimal]]:
    return [
        (date.fromisoformat(r["date"]), Decimal(str(r["close"])))
        for r in conn.execute(
            "SELECT date, close FROM price WHERE instrument_id = ? ORDER BY date",
            (instrument_id,),
        )
    ]


def build_position_daily(
    conn: sqlite3.Connection,
    *,
    end: date,
    start: date | None = None,
    strict: bool = True,
) -> ValuationReport:
    """Recompute ``position_daily`` over the TARGET2 grid.

    ``strict`` raises on a price older than the tolerance. The nightly job runs
    strict, because a silently stale valuation is worse than a failed one; a
    multi-year backfill runs non-strict and records the affected days instead,
    since one market holiday should not abort an entire rebuild.
    """
    report = ValuationReport()
    holdings = _load_holdings(conn)
    if not holdings:
        return report

    first = min(when for h in holdings for when, _ in h.events)
    window_start = start or first
    days = grid_days(window_start, end, TARGET2)
    report.days = len(days)

    instruments = {
        int(r["id"]): r
        for r in conn.execute("SELECT id, import_key, currency, trading_calendar FROM instrument")
    }
    prices = {i: _price_series(conn, i) for i in {h.instrument_id for h in holdings}}
    currencies = {h.currency for h in holdings}
    rates = {
        currency: fx.get_rate_series(conn, currency, days, strict=False) for currency in currencies
    }

    conn.execute(
        "DELETE FROM position_daily WHERE date BETWEEN ? AND ?",
        (window_start.isoformat(), end.isoformat()),
    )

    # Aggregate lots into positions: a position is what the user holds, a lot is
    # how the tax engine sees it.
    for day in days:
        by_position: dict[tuple[int, int], list[_Holding]] = {}
        for holding in holdings:
            if holding.quantity_on(day) > ZERO:
                by_position.setdefault((holding.account_id, holding.instrument_id), []).append(
                    holding
                )

        for (account_id, instrument_id), lots in by_position.items():
            instrument = instruments[instrument_id]
            calendar = instrument["trading_calendar"]
            currency = instrument["currency"]

            observed = [(d, p) for d, p in prices[instrument_id] if d <= day]
            if not observed:
                report.missing.append(f"{instrument['import_key']} {day}: no price at all")
                continue
            price_asof, price = observed[-1]
            price_stale = business_days_between(price_asof, day, calendar)

            rate_info = rates[currency].get(day)
            if rate_info is None:
                report.missing.append(f"{instrument['import_key']} {day}: no FX rate")
                continue

            if price_stale > MAX_STALE_DAYS:
                message = (
                    f"{instrument['import_key']} {day}: price is {price_stale} trading days "
                    f"stale (latest {price_asof})"
                )
                if strict:
                    raise StalePriceError(message)
                report.stale.append(message)

            quantity = sum((lot.quantity_on(day) for lot in lots), ZERO)
            # Quantised: a per-unit cost is a division, so summing across lots
            # accumulates 20-odd meaningless digits. A cost basis carrying more
            # precision than the cent it came from is false confidence.
            cost_local = quantize_money(
                sum((lot.cost_per_unit_local * lot.quantity_on(day) for lot in lots), ZERO)
            )
            cost_eur = quantize_money(
                sum(((lot.cost_per_unit_eur or ZERO) * lot.quantity_on(day) for lot in lots), ZERO)
            )

            value = value_position(quantity, price, rate_info.rate, cost_local, cost_eur)
            status = "OK" if price_stale <= MAX_STALE_DAYS else "STALE"

            conn.execute(
                """
                INSERT INTO position_daily
                    (date, account_id, instrument_id, quantity, currency,
                     price_local, price_asof, price_stale_days,
                     fx_rate, fx_asof, fx_stale_days,
                     market_value_local, market_value_eur,
                     cost_basis_local, cost_basis_eur, cost_basis_eur_at_spot,
                     unrealized_eur, unrealized_local_eur, unrealized_fx_eur,
                     valuation_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    day.isoformat(),
                    account_id,
                    instrument_id,
                    quantity,
                    currency,
                    price,
                    price_asof.isoformat(),
                    price_stale,
                    rate_info.rate,
                    rate_info.asof.isoformat(),
                    rate_info.stale_days,
                    value.market_value_local,
                    value.market_value_eur,
                    value.cost_basis_local,
                    value.cost_basis_eur,
                    value.cost_basis_eur_at_spot,
                    value.unrealized_eur,
                    value.unrealized_local_eur,
                    value.unrealized_fx_eur,
                    status,
                ),
            )
            report.rows += 1

    return report
