"""What a sale would cost, before you make it.

Everything else in this codebase reports what happened. This module answers the
one question the owner asks *before* acting: **if I sold this today, what would
the tax bill be, and how much allowance would be left?**

It can only answer that because the pieces exist and agree with each other --
FIFO lots with a cost basis frozen at acquisition-date FX, the German
capital-gains engine, the loss pots, the Sparerpauschbetrag, and the
Vorabpauschale relief that stops the same euros being taxed twice. This module
composes them. It computes no tax rules of its own, because a second
implementation of a tax rule is how the two come to disagree.

## The line this module does not cross

**Today's prices only. No assumed future returns, ever.**

That is not conservatism, it is the same rule the rest of the tool follows. A
Monte Carlo projection or a "what if markets do 7%" slider produces a number
with the shape of a forecast and none of the substance, and this project has
spent four ADRs refusing to fabricate data it does not have. ``core/goal.py``
already projects on contributions alone and assumes **no investment return at
all**, for exactly this reason.

So every figure here is conditional on one thing the owner can check: the last
observed price. If that price is stale, the answer says so rather than
pretending.

## Read-only, structurally

Nothing here writes. A scenario that left rows behind would be indistinguishable
from a decision afterwards, and the whole design rests on the ledger containing
only what a person confirmed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import cast

from portfolio_tracker.core import fx, lots, tax
from portfolio_tracker.money import ZERO, quantize_money

#: A gain smaller than this is not worth reporting as a harvesting candidate.
DUST = Decimal("0.01")


class ScenarioError(ValueError):
    """The scenario cannot be evaluated as asked."""


@dataclass(frozen=True)
class LotConsumed:
    """One lot a sale would eat into, and what it would realise."""

    lot_id: int
    open_date: date
    quantity: Decimal
    cost_basis_eur: Decimal
    proceeds_eur: Decimal
    gross_gain_eur: Decimal
    vap_offset_eur: Decimal
    holding_days: int


@dataclass
class SaleScenario:
    """What selling a holding today would realise, and what it would cost."""

    instrument: str
    instrument_id: int
    as_of: date
    price_local: Decimal
    price_asof: date
    price_stale_days: int
    currency: str
    fx_rate: Decimal
    quantity: Decimal
    quantity_held: Decimal

    proceeds_eur: Decimal = ZERO
    cost_basis_eur: Decimal = ZERO
    gross_gain_eur: Decimal = ZERO
    vap_offset_eur: Decimal = ZERO
    teilfreistellung_eur: Decimal = ZERO
    taxable_gain_eur: Decimal = ZERO
    pot: str = "SONSTIGE"
    in_tax_scope: bool = True

    #: What the year looks like before and after, so the marginal cost of *this*
    #: sale is visible rather than having to be inferred from two reports.
    allowance_remaining_before_eur: Decimal = ZERO
    allowance_remaining_after_eur: Decimal = ZERO
    tax_before_eur: Decimal = ZERO
    tax_after_eur: Decimal = ZERO

    consumed: list[LotConsumed] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    @property
    def marginal_tax_eur(self) -> Decimal:
        """What this sale adds to the year's bill. The number being asked for."""
        return quantize_money(self.tax_after_eur - self.tax_before_eur)

    @property
    def net_proceeds_eur(self) -> Decimal:
        return quantize_money(self.proceeds_eur - self.marginal_tax_eur)


# ---------------------------------------------------------------------------
# Pricing a hypothetical sale
# ---------------------------------------------------------------------------


def _latest_price(
    conn: sqlite3.Connection, instrument_id: int, as_of: date
) -> tuple[Decimal, date] | None:
    row = conn.execute(
        "SELECT date, close FROM price WHERE instrument_id = ? AND date <= ? "
        "ORDER BY date DESC LIMIT 1",
        (instrument_id, as_of.isoformat()),
    ).fetchone()
    if row is None:
        return None
    return Decimal(str(row["close"])), date.fromisoformat(row["date"])


def _instrument(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM instrument WHERE import_key = ? OR isin = ? OR symbol = ?",
        (key, key.upper(), key),
    ).fetchone()
    if row is None:
        raise ScenarioError(f"no instrument matches {key!r}")
    return cast("sqlite3.Row", row)


def sell(
    conn: sqlite3.Connection,
    *,
    instrument: str,
    quantity: Decimal | None = None,
    person_id: int = 1,
    as_of: date | None = None,
    tax_year: int | None = None,
) -> SaleScenario:
    """Price a hypothetical sale at the latest observed price.

    ``quantity=None`` sells the whole holding. FIFO decides which lots are
    consumed, which is not a detail: selling half a position realises the
    *oldest* half, and on a holding built up over two years those lots can have
    a completely different cost basis from the newest ones.
    """
    on = as_of or date.today()
    year = tax_year or on.year
    row = _instrument(conn, instrument)
    instrument_id = int(row["id"])

    priced = _latest_price(conn, instrument_id, on)
    if priced is None:
        raise ScenarioError(
            f"no price for {row['import_key']} on or before {on}; run `pt fetch-prices` first"
        )
    price, price_asof = priced

    open_lots = [
        lots.Lot(
            account_id=int(lot["account_id"]),
            instrument_id=instrument_id,
            open_txn_id=int(lot["open_txn_id"]),
            open_date=date.fromisoformat(lot["open_date"]),
            currency=lot["currency"],
            qty_opened=Decimal(str(lot["qty_opened"])),
            qty_remaining=Decimal(str(lot["qty_remaining"])),
            cost_total_local=Decimal(str(lot["cost_total_local"])),
            cost_per_unit_local=Decimal(str(lot["cost_per_unit_local"])),
            fx_rate_at_open=(
                Decimal(str(lot["fx_rate_at_open"])) if lot["fx_rate_at_open"] is not None else None
            ),
            cost_total_eur=(
                Decimal(str(lot["cost_total_eur"])) if lot["cost_total_eur"] is not None else None
            ),
            cost_per_unit_eur=(
                Decimal(str(lot["cost_per_unit_eur"]))
                if lot["cost_per_unit_eur"] is not None
                else None
            ),
            basis_quality=lot["basis_quality"],
            lot_id=int(lot["id"]),
            vap_accumulated_eur=Decimal(str(lot["vap_accumulated_eur"] or 0)),
        )
        # Oldest first: this IS the FIFO order the real sale would follow.
        for lot in conn.execute(
            "SELECT * FROM lot WHERE instrument_id = ? AND qty_remaining > 0 "
            "ORDER BY open_date, id",
            (instrument_id,),
        )
    ]
    held = sum((lot.qty_remaining for lot in open_lots), ZERO)
    if held <= ZERO:
        raise ScenarioError(f"no open position in {row['import_key']}")

    wanted = held if quantity is None else quantity
    if wanted > held:
        raise ScenarioError(f"cannot sell {wanted} of {row['import_key']}: only {held} is held")

    currency = str(row["currency"])
    rate = Decimal(1)
    if currency != fx.BASE:
        rate = fx.get_rate(conn, currency, on, strict=False).rate

    scenario = SaleScenario(
        instrument=str(row["import_key"]),
        instrument_id=instrument_id,
        as_of=on,
        price_local=price,
        price_asof=price_asof,
        price_stale_days=(on - price_asof).days,
        currency=currency,
        fx_rate=rate,
        quantity=wanted,
        quantity_held=held,
        in_tax_scope=row["tax_regime"] == tax.IN_SCOPE_REGIME,
        pot=tax.loss_pot_for(conn, instrument_id),
    )

    for lot, used in lots.fifo_consume(open_lots, wanted):
        proceeds_local = price * used
        proceeds_eur = quantize_money(fx.to_eur(proceeds_local, rate))
        cost_eur = quantize_money((lot.cost_per_unit_eur or ZERO) * used)
        gain = proceeds_eur - cost_eur
        # The relief travels with the units, exactly as `lots.py` applies it on
        # a real disposal. Computing it differently here is how a scenario comes
        # to disagree with the sale it is supposed to be modelling.
        vap = ZERO
        if lot.qty_remaining > ZERO and lot.vap_accumulated_eur > ZERO:
            vap = quantize_money(lot.vap_accumulated_eur * used / lot.qty_remaining)

        scenario.consumed.append(
            LotConsumed(
                lot_id=int(lot.lot_id or 0),
                open_date=lot.open_date,
                quantity=used,
                cost_basis_eur=cost_eur,
                proceeds_eur=proceeds_eur,
                gross_gain_eur=gain,
                vap_offset_eur=vap,
                holding_days=(on - lot.open_date).days,
            )
        )
        scenario.proceeds_eur += proceeds_eur
        scenario.cost_basis_eur += cost_eur
        scenario.gross_gain_eur += gain
        scenario.vap_offset_eur += vap

    # From here the arithmetic is the tax engine's, applied in its order: the
    # Vorabpauschale relief reduces a positive gain toward zero and never
    # deepens a loss, then Teilfreistellung applies to what is left.
    applied = min(scenario.vap_offset_eur, scenario.gross_gain_eur)
    after_vap = (
        scenario.gross_gain_eur - applied
        if scenario.gross_gain_eur > ZERO
        else (scenario.gross_gain_eur)
    )
    scenario.vap_offset_eur = quantize_money(max(applied, ZERO))

    if scenario.in_tax_scope:
        exemption = tax.teilfreistellung(conn, instrument_id)
        scenario.teilfreistellung_eur = quantize_money(after_vap * exemption)
        scenario.taxable_gain_eur = quantize_money(after_vap - scenario.teilfreistellung_eur)
    else:
        scenario.taxable_gain_eur = ZERO

    _apply_to_year(conn, scenario, person_id=person_id, tax_year=year)
    scenario.caveats = _caveats(scenario)
    return scenario


def _apply_to_year(
    conn: sqlite3.Connection, scenario: SaleScenario, *, person_id: int, tax_year: int
) -> None:
    """Where the year's position stands with and without this sale.

    The marginal figure is the point. A gain of EUR 900 costs nothing when the
    allowance is untouched and EUR 237 when it is already spent, and no
    per-sale number can express that on its own.
    """
    before = tax.allowance_state(conn, person_id=person_id, tax_year=tax_year)
    scenario.allowance_remaining_before_eur = before.remaining_eur
    scenario.tax_before_eur = before.estimated_tax_eur

    if not scenario.in_tax_scope:
        scenario.allowance_remaining_after_eur = before.remaining_eur
        scenario.tax_after_eur = before.estimated_tax_eur
        return

    # The same settlement the engine performs, with this one disposal added.
    # Losses offset first, then the allowance applies to what remains.
    gains = before.after_losses_eur + max(scenario.taxable_gain_eur, ZERO)
    if scenario.taxable_gain_eur < ZERO:
        # A realised loss offsets gains already booked this year, down to zero.
        gains = max(ZERO, before.after_losses_eur + scenario.taxable_gain_eur)

    cap = before.statutory_cap_eur
    consumed = min(gains, cap)
    taxable = max(ZERO, gains - consumed)
    scenario.allowance_remaining_after_eur = quantize_money(cap - consumed)
    scenario.tax_after_eur = quantize_money(taxable * tax.COMBINED_RATE)


def _caveats(scenario: SaleScenario) -> list[str]:
    out = [
        "Priced at the last observed close. No future return is assumed anywhere in this figure.",
    ]
    if scenario.price_stale_days > 0:
        out.append(
            f"The price is {scenario.price_stale_days} day(s) old "
            f"({scenario.price_asof}); the real proceeds will differ."
        )
    if not scenario.in_tax_scope:
        out.append(
            f"{scenario.instrument} is outside the section 20 engine, so no tax is "
            "computed for it here. That is not the same as owing none."
        )
    if scenario.vap_offset_eur > ZERO:
        out.append(
            f"EUR {scenario.vap_offset_eur:,.2f} of Vorabpauschale already taxed is "
            "deducted from the gain, so it is not taxed a second time."
        )
    if scenario.taxable_gain_eur < ZERO:
        out.append(
            f"This is a realised loss. It offsets gains in the {scenario.pot} pot only, "
            "and carries forward if there are none."
        )
    out.append(
        "A planning figure. Fees are not in the source data, so real proceeds are "
        "lower and the real gain is smaller than shown."
    )
    return out


# ---------------------------------------------------------------------------
# Using up the allowance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """A holding, and what selling all of it today would realise."""

    instrument: str
    instrument_id: int
    quantity: Decimal
    market_value_eur: Decimal
    gross_gain_eur: Decimal
    taxable_gain_eur: Decimal
    pot: str

    @property
    def is_gain(self) -> bool:
        return self.taxable_gain_eur > DUST

    @property
    def is_loss(self) -> bool:
        return self.taxable_gain_eur < -DUST


@dataclass
class Harvest:
    """The year's headroom, and what could be sold to use it."""

    tax_year: int
    as_of: date
    allowance_cap_eur: Decimal
    allowance_used_eur: Decimal
    allowance_remaining_eur: Decimal
    candidates: list[Candidate] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    @property
    def gains(self) -> list[Candidate]:
        return [c for c in self.candidates if c.is_gain]

    @property
    def losses(self) -> list[Candidate]:
        return [c for c in self.candidates if c.is_loss]

    @property
    def realisable_free_eur(self) -> Decimal:
        """Gain that could be realised without paying any tax on it."""
        return self.allowance_remaining_eur


def harvest(
    conn: sqlite3.Connection,
    *,
    person_id: int = 1,
    tax_year: int | None = None,
    as_of: date | None = None,
) -> Harvest:
    """What could be sold to use up the year's Sparerpauschbetrag.

    The allowance does not carry forward. Every euro of it unused on 31
    December is gone, and using it costs nothing but the transaction -- so
    "which position could I realise a gain on for free" is a question with a
    deadline and an exact answer.

    Deliberately a *list*, not a recommendation. Which holding to sell depends
    on what the owner wants to keep owning, and this module knows the tax and
    nothing else.
    """
    on = as_of or date.today()
    year = tax_year or on.year
    state = tax.allowance_state(conn, person_id=person_id, tax_year=year)

    report = Harvest(
        tax_year=year,
        as_of=on,
        allowance_cap_eur=state.statutory_cap_eur,
        allowance_used_eur=state.consumed_eur,
        allowance_remaining_eur=state.remaining_eur,
    )

    for row in conn.execute(
        "SELECT DISTINCT i.import_key FROM lot l JOIN instrument i ON i.id = l.instrument_id "
        "JOIN account a ON a.id = l.account_id "
        "WHERE l.qty_remaining > 0 AND a.person_id = ? ORDER BY i.import_key",
        (person_id,),
    ):
        try:
            priced = sell(
                conn,
                instrument=str(row["import_key"]),
                person_id=person_id,
                as_of=on,
                tax_year=year,
            )
        except (ScenarioError, tax.TaxDataMissing, fx.FxUnavailable):
            # A holding that cannot be priced or classified is skipped rather
            # than shown at zero: a candidate list with a silent hole in it is
            # worse than a shorter one.
            continue
        if not priced.in_tax_scope:
            continue
        report.candidates.append(
            Candidate(
                instrument=priced.instrument,
                instrument_id=priced.instrument_id,
                quantity=priced.quantity,
                market_value_eur=priced.proceeds_eur,
                gross_gain_eur=priced.gross_gain_eur,
                taxable_gain_eur=priced.taxable_gain_eur,
                pot=priced.pot,
            )
        )

    report.candidates.sort(key=lambda c: -c.taxable_gain_eur)
    report.caveats = [
        "Allowance does not carry forward: whatever is unused on 31 December is lost.",
        "Every figure sells the WHOLE holding. Selling part of one realises the "
        "oldest lots first, which usually carries a different gain per unit.",
        "A list, not a recommendation. Which of these to sell is a question about "
        "what you want to keep owning, which this tool cannot answer.",
        "Fees are absent from the source data, so every gain here is biased high.",
    ]
    if report.allowance_remaining_eur <= ZERO:
        report.caveats.insert(
            0,
            f"The {year} allowance is already used up. Realising a further gain "
            "costs tax at 26.375%.",
        )
    return report
