"""German capital-gains treatment.

Implements the rules as written, not as approximated. The parts most easily got
wrong, and the reason each matters:

**Teilfreistellung** applies symmetrically to gains *and* losses. Exempting 30%
of a gain but deducting 100% of a loss would be a pleasant arithmetic error and
an incorrect return.

**Vorabpauschale** (§18 InvStG) is charged on an accumulating fund that rose,
capped at the rise itself, pro-rated for a part-year holding, and taxable the
*following* January. It also has to be remembered: the amount already taxed is
deducted from the gain when the fund is eventually sold, or the same money is
taxed twice.

**Loss pots** (§20 VI EStG) are segregated. A loss on an individual share may
only offset a gain on an individual share; everything else goes to a general
pot. Merging them understates tax.

**Order of operations**: losses are offset first, and the allowance applies to
what remains. The other way round wastes allowance.

Scope is decided by ``instrument.tax_regime`` and by nothing else -- there is no
currency test anywhere in this module. Bringing an instrument into scope is a
data change, not a code change.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from portfolio_tracker.money import ZERO, quantize_money

#: The only regime this engine computes for.
IN_SCOPE_REGIME = "DE_INVESTMENT"

#: Statutory Sparerpauschbetrag, per person and jointly assessed.
ALLOWANCE_SINGLE = Decimal(1000)
ALLOWANCE_JOINT = Decimal(2000)

#: Kapitalertragsteuer plus Solidaritätszuschlag. Church tax is per person.
KEST_RATE = Decimal("0.25")
SOLI_RATE = Decimal("0.055")
COMBINED_RATE = KEST_RATE * (1 + SOLI_RATE)  # 0.26375

#: §18 InvStG: the Basisertrag uses 70% of the Basiszins.
BASISERTRAG_FACTOR = Decimal("0.7")


class TaxDataMissing(RuntimeError):
    """A figure the computation needs has not been established.

    Raised rather than defaulted. Teilfreistellung in particular cannot be
    guessed: 0.30 on a bond fund and 0.00 on an equity fund are both wrong, and
    both produce a confident number.
    """


@dataclass(frozen=True)
class DisposalTax:
    disposal_id: int
    instrument: str
    sold_on: date
    gross_gain_eur: Decimal
    vap_offset_eur: Decimal
    teilfreistellung_eur: Decimal
    taxable_gain_eur: Decimal
    pot: str  # AKTIEN | SONSTIGE
    holding_days: int


@dataclass(frozen=True)
class VorabpauschaleRow:
    instrument: str
    instrument_id: int
    account_id: int
    tax_year: int
    jan_first_price: Decimal
    year_end_price: Decimal
    quantity: Decimal
    basiszins: Decimal
    basisertrag_raw: Decimal
    cap: Decimal
    basisertrag: Decimal
    full_months_before: int
    distributions_eur: Decimal
    vap_gross_eur: Decimal
    teilfreistellung_eur: Decimal
    taxable_eur: Decimal
    entry_date: date


@dataclass
class LossPots:
    aktien: Decimal = ZERO
    sonstige: Decimal = ZERO


@dataclass
class AllowanceState:
    person_id: int
    tax_year: int
    statutory_cap_eur: Decimal
    gross_gains_eur: Decimal = ZERO
    losses_offset_eur: Decimal = ZERO
    after_losses_eur: Decimal = ZERO
    consumed_eur: Decimal = ZERO
    remaining_eur: Decimal = ZERO
    distributions_eur: Decimal = ZERO
    #: Booked Vorabpauschale charged in this year, after Teilfreistellung. A
    #: deemed distribution is income on the same footing as a real one; the
    #: only difference is that no money arrived.
    vorabpauschale_eur: Decimal = ZERO
    taxable_after_allowance_eur: Decimal = ZERO
    estimated_tax_eur: Decimal = ZERO
    carried_forward: LossPots = field(default_factory=LossPots)
    entries: list[tuple[date, str, Decimal]] = field(default_factory=list)


@dataclass
class OutOfScopeReport:
    tax_year: int
    instruments: list[str] = field(default_factory=list)
    value_eur: Decimal = ZERO
    realized_gain_eur: Decimal = ZERO
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.instruments


# ---------------------------------------------------------------------------
# Scope and rates
# ---------------------------------------------------------------------------


def in_scope(conn: sqlite3.Connection, instrument_id: int) -> bool:
    """The only scope test in this module."""
    row = conn.execute(
        "SELECT tax_regime FROM instrument WHERE id = ?", (instrument_id,)
    ).fetchone()
    return row is not None and row["tax_regime"] == IN_SCOPE_REGIME


def statutory_cap(conn: sqlite3.Connection, person_id: int) -> Decimal:
    """1,000 EUR per person, 2,000 jointly assessed."""
    row = conn.execute("SELECT joint_assessment FROM person WHERE id = ?", (person_id,)).fetchone()
    if row is None:
        raise LookupError(f"no person {person_id}")
    return ALLOWANCE_JOINT if row["joint_assessment"] else ALLOWANCE_SINGLE


def teilfreistellung(conn: sqlite3.Connection, instrument_id: int) -> Decimal:
    """The partial exemption for this instrument.

    Raises when unresolved. An equity fund is 30%, a mixed fund 15%, and a
    bond, money-market or commodity fund 0% -- and the difference between the
    first and last is 30% of the tax on the position.
    """
    row = conn.execute(
        "SELECT import_key, teilfreistellung_pct, is_fund FROM instrument WHERE id = ?",
        (instrument_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no instrument {instrument_id}")
    if row["teilfreistellung_pct"] is None:
        raise TaxDataMissing(
            f"{row['import_key']}: Teilfreistellung is unresolved. Establish it from the "
            "fund's terms (equity >=51% -> 0.30, mixed >=25% -> 0.15, otherwise 0.00) "
            "and set it in seeds/instruments.yaml."
        )
    return Decimal(str(row["teilfreistellung_pct"]))


def loss_pot_for(conn: sqlite3.Connection, instrument_id: int) -> str:
    """`AKTIEN` for an individual share, `SONSTIGE` for everything else.

    §20 VI EStG: a share loss may only offset a share gain. Putting a fund loss
    in the share pot would let it offset income it legally cannot.
    """
    row = conn.execute(
        "SELECT asset_class FROM instrument WHERE id = ?", (instrument_id,)
    ).fetchone()
    return "AKTIEN" if row is not None and row["asset_class"] == "STOCK" else "SONSTIGE"


# ---------------------------------------------------------------------------
# Disposals
# ---------------------------------------------------------------------------


def compute_disposal_tax(
    conn: sqlite3.Connection, *, tax_year: int | None = None, person_id: int | None = None
) -> list[DisposalTax]:
    """Taxable gain per disposal, for in-scope instruments only.

    Filtering by person is not optional where one exists: each taxpayer has
    their own allowance and their own loss pots, and the two never mix.
    """
    query = """
        SELECT d.id, d.qty, d.gross_gain_eur, d.holding_days, d.vap_offset_eur,
               t.trade_date, t.instrument_id, i.import_key, i.tax_regime
        FROM lot_disposal d
        JOIN transaction_ t ON t.id = d.sell_txn_id
        JOIN instrument i ON i.id = t.instrument_id
        JOIN account a ON a.id = t.account_id
        WHERE i.tax_regime = ?
    """
    params: list[object] = [IN_SCOPE_REGIME]
    if person_id is not None:
        query += " AND a.person_id = ?"
        params.append(person_id)
    if tax_year is not None:
        query += " AND t.trade_date BETWEEN ? AND ?"
        params += [f"{tax_year}-01-01", f"{tax_year}-12-31"]

    out: list[DisposalTax] = []
    for row in conn.execute(query + " ORDER BY t.trade_date, d.id", tuple(params)):
        if row["gross_gain_eur"] is None:
            continue
        gross = Decimal(str(row["gross_gain_eur"]))
        vap_offset = Decimal(str(row["vap_offset_eur"] or 0))

        # The offset only reduces a positive gain toward zero. It never turns a
        # gain into a loss, and never deepens a loss.
        if gross > ZERO:
            applied = min(vap_offset, gross)
            after_vap = gross - applied
        else:
            applied = ZERO
            after_vap = gross

        rate = teilfreistellung(conn, int(row["instrument_id"]))
        # Symmetric: the same fraction is exempt whether the figure is a gain
        # or a loss.
        exempt = quantize_money(after_vap * rate)
        taxable = quantize_money(after_vap - exempt)

        out.append(
            DisposalTax(
                disposal_id=int(row["id"]),
                instrument=row["import_key"],
                sold_on=date.fromisoformat(row["trade_date"]),
                gross_gain_eur=quantize_money(gross),
                vap_offset_eur=quantize_money(applied),
                teilfreistellung_eur=exempt,
                taxable_gain_eur=taxable,
                pot=loss_pot_for(conn, int(row["instrument_id"])),
                holding_days=int(row["holding_days"] or 0),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Vorabpauschale (§18 InvStG)
# ---------------------------------------------------------------------------


def basiszins(conn: sqlite3.Connection, tax_year: int) -> Decimal:
    row = conn.execute(
        "SELECT basiszins FROM tax_constant WHERE tax_year = ?", (tax_year,)
    ).fetchone()
    if row is None:
        raise TaxDataMissing(
            f"no Basiszins for {tax_year}; add it to tax_constant "
            "(published annually by the Bundesfinanzministerium)"
        )
    return Decimal(str(row["basiszins"]))


def _price_on_or_after(conn: sqlite3.Connection, instrument_id: int, when: date) -> Decimal | None:
    row = conn.execute(
        "SELECT close FROM price WHERE instrument_id = ? AND date >= ? ORDER BY date LIMIT 1",
        (instrument_id, when.isoformat()),
    ).fetchone()
    return Decimal(str(row["close"])) if row else None


def _price_on_or_before(conn: sqlite3.Connection, instrument_id: int, when: date) -> Decimal | None:
    row = conn.execute(
        "SELECT close FROM price WHERE instrument_id = ? AND date <= ? ORDER BY date DESC LIMIT 1",
        (instrument_id, when.isoformat()),
    ).fetchone()
    return Decimal(str(row["close"])) if row else None


def vorabpauschale_for(
    *,
    jan_first_price: Decimal,
    year_end_price: Decimal,
    quantity: Decimal,
    basiszins_rate: Decimal,
    teilfreistellung_pct: Decimal,
    full_months_before: int = 0,
    distributions_eur: Decimal = ZERO,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    """The §18 InvStG calculation. Pure, so the fixtures can exercise it directly.

    Returns ``(basisertrag_raw, cap, basisertrag, vap_gross, taxable)``.
    """
    raw = jan_first_price * quantity * basiszins_rate * BASISERTRAG_FACTOR

    # Capped at the fund's actual rise: a fund that did not gain owes nothing,
    # however high the reference rate.
    appreciation = (year_end_price - jan_first_price) * quantity
    cap = max(ZERO, appreciation) + distributions_eur

    basisertrag = min(raw, cap)
    # Pro-rated by whole months held: 1/12 removed per full month before
    # acquisition.
    basisertrag = basisertrag * (Decimal(12 - full_months_before) / Decimal(12))

    gross = max(ZERO, basisertrag - distributions_eur)
    taxable = gross * (1 - teilfreistellung_pct)
    return (
        quantize_money(raw),
        quantize_money(cap),
        quantize_money(basisertrag),
        quantize_money(gross),
        quantize_money(taxable),
    )


def forecast_vorabpauschale(
    conn: sqlite3.Connection, *, tax_year: int, as_of: date | None = None
) -> list[VorabpauschaleRow]:
    """Vorabpauschale for every in-scope fund held at the year end."""
    rate = basiszins(conn, tax_year)
    jan_first = date(tax_year, 1, 1)
    year_end = date(tax_year, 12, 31)
    measured_at = min(as_of or year_end, year_end)

    rows: list[VorabpauschaleRow] = []
    for lot in conn.execute(
        """
        SELECT l.id, l.account_id, l.instrument_id, l.open_date, l.qty_remaining,
               i.import_key, i.teilfreistellung_pct
        FROM lot l JOIN instrument i ON i.id = l.instrument_id
        WHERE i.tax_regime = ? AND i.is_fund = 1 AND l.qty_remaining > 0
          AND l.open_date <= ?
        ORDER BY i.import_key, l.open_date
        """,
        (IN_SCOPE_REGIME, year_end.isoformat()),
    ):
        opening = _price_on_or_after(conn, int(lot["instrument_id"]), jan_first)
        closing = _price_on_or_before(conn, int(lot["instrument_id"]), measured_at)
        if opening is None or closing is None:
            continue

        open_date = date.fromisoformat(lot["open_date"])
        # Only whole months *before* the acquisition month reduce the charge.
        months_before = (open_date.month - 1) if open_date.year == tax_year else 0

        exemption = teilfreistellung(conn, int(lot["instrument_id"]))
        quantity = Decimal(str(lot["qty_remaining"]))
        # The charge exists only to top a fund up to a notional minimum return,
        # so a fund that already distributed that much owes nothing further.
        from portfolio_tracker.ingest.dividends import distributions_for

        paid_out = distributions_for(
            conn, instrument_id=int(lot["instrument_id"]), tax_year=tax_year
        )
        raw, cap, basisertrag, gross, taxable = vorabpauschale_for(
            jan_first_price=opening,
            year_end_price=closing,
            quantity=quantity,
            basiszins_rate=rate,
            teilfreistellung_pct=exemption,
            full_months_before=months_before,
            distributions_eur=paid_out,
        )

        rows.append(
            VorabpauschaleRow(
                instrument=lot["import_key"],
                instrument_id=int(lot["instrument_id"]),
                account_id=int(lot["account_id"]),
                tax_year=tax_year,
                jan_first_price=opening,
                year_end_price=closing,
                quantity=quantity,
                basiszins=rate,
                basisertrag_raw=raw,
                cap=cap,
                basisertrag=basisertrag,
                full_months_before=months_before,
                distributions_eur=paid_out,
                vap_gross_eur=gross,
                teilfreistellung_eur=quantize_money(gross - taxable),
                taxable_eur=taxable,
                # Taxable in the January following the year it accrued.
                entry_date=date(tax_year + 1, 1, 2),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Allowance and loss pots
# ---------------------------------------------------------------------------


def distribution_income(
    conn: sqlite3.Connection, *, tax_year: int, person_id: int | None = None
) -> list[tuple[str, Decimal]]:
    """Distributions received in a year, after Teilfreistellung.

    In-scope instruments only, and reduced by the same partial exemption that
    applies to a gain on the same fund.
    """
    query = """
        SELECT i.id, i.import_key, t.gross_amount_eur
        FROM transaction_ t
        JOIN instrument i ON i.id = t.instrument_id
        JOIN account a ON a.id = t.account_id
        WHERE t.type = 'DIVIDEND' AND i.tax_regime = ?
          AND t.trade_date BETWEEN ? AND ?
    """
    params: list[object] = [IN_SCOPE_REGIME, f"{tax_year}-01-01", f"{tax_year}-12-31"]
    if person_id is not None:
        query += " AND a.person_id = ?"
        params.append(person_id)

    out: list[tuple[str, Decimal]] = []
    for row in conn.execute(query, tuple(params)):
        if row["gross_amount_eur"] is None:
            continue
        rate = teilfreistellung(conn, int(row["id"]))
        out.append((row["import_key"], quantize_money(row["gross_amount_eur"] * (1 - rate))))
    return out


def allowance_state(
    conn: sqlite3.Connection,
    *,
    person_id: int,
    tax_year: int,
    carried_in: LossPots | None = None,
) -> AllowanceState:
    """Where the year's allowance stands, after losses are offset.

    Order matters: losses are offset first and the allowance applies to what
    remains. Applying the allowance first would waste it against income the
    losses would have cancelled anyway.
    """
    cap = statutory_cap(conn, person_id)
    pots = carried_in or LossPots()
    state = AllowanceState(person_id=person_id, tax_year=tax_year, statutory_cap_eur=cap)

    disposals = compute_disposal_tax(conn, tax_year=tax_year, person_id=person_id)
    distributions = distribution_income(conn, tax_year=tax_year, person_id=person_id)
    share_gains = [d for d in disposals if d.pot == "AKTIEN"]
    other_gains = [d for d in disposals if d.pot == "SONSTIGE"]

    def settle(items: list[DisposalTax], carried: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        gains = sum((d.taxable_gain_eur for d in items if d.taxable_gain_eur > ZERO), ZERO)
        losses = -sum((d.taxable_gain_eur for d in items if d.taxable_gain_eur < ZERO), ZERO)
        available = carried + losses
        used = min(gains, available)
        return gains, used, available - used

    share_gain, share_used, share_left = settle(share_gains, pots.aktien)
    other_gain, other_used, other_left = settle(other_gains, pots.sonstige)

    # A distribution is income in the year it is paid, taxed on the same
    # footing as a realised gain and consuming the same allowance. Leaving it
    # out understates income and overstates remaining headroom -- which then
    # corrupts any harvesting decision made against that headroom.
    state.distributions_eur = quantize_money(sum((d for _, d in distributions), ZERO))

    # A Vorabpauschale is a deemed distribution, taxed in the January after the
    # year it accrued for. It consumes the same allowance as a real one, and
    # omitting it here would understate the year's income by exactly the amount
    # the bank is about to withhold tax on. Imported lazily: `vorabpauschale`
    # builds on this module, so a module-level import would be circular.
    from portfolio_tracker.core import vorabpauschale

    deemed = vorabpauschale.taxable_income(conn, tax_year=tax_year, person_id=person_id)
    state.vorabpauschale_eur = quantize_money(sum((d for _, d in deemed), ZERO))

    state.gross_gains_eur = quantize_money(
        share_gain + other_gain + state.distributions_eur + state.vorabpauschale_eur
    )
    state.losses_offset_eur = quantize_money(share_used + other_used)
    state.after_losses_eur = quantize_money(state.gross_gains_eur - state.losses_offset_eur)

    state.consumed_eur = quantize_money(min(state.after_losses_eur, cap))
    state.remaining_eur = quantize_money(cap - state.consumed_eur)
    state.taxable_after_allowance_eur = quantize_money(
        max(ZERO, state.after_losses_eur - state.consumed_eur)
    )
    state.estimated_tax_eur = quantize_money(state.taxable_after_allowance_eur * COMBINED_RATE)
    state.carried_forward = LossPots(
        aktien=quantize_money(share_left), sonstige=quantize_money(other_left)
    )

    for disposal in disposals:
        state.entries.append((disposal.sold_on, disposal.instrument, disposal.taxable_gain_eur))
    return state


# ---------------------------------------------------------------------------
# What this engine deliberately does not cover
# ---------------------------------------------------------------------------


def out_of_scope_report(
    conn: sqlite3.Connection, *, tax_year: int, as_of: date
) -> OutOfScopeReport:
    """Everything excluded from the computation, and why.

    Not optional. A tax figure that silently omits part of the portfolio is
    more dangerous than no figure, because it looks complete.
    """
    report = OutOfScopeReport(tax_year=tax_year)

    for row in conn.execute(
        "SELECT id, import_key, tax_regime, tax_scope_note, teilfreistellung_pct "
        "FROM instrument WHERE tax_regime <> ? ORDER BY import_key",
        (IN_SCOPE_REGIME,),
    ):
        report.instruments.append(row["import_key"])
        report.reasons[row["import_key"]] = row["tax_scope_note"] or row["tax_regime"]

    for row in conn.execute(
        "SELECT p.market_value_eur FROM position_daily p "
        "JOIN instrument i ON i.id = p.instrument_id "
        "WHERE p.date = ? AND i.tax_regime <> ?",
        (as_of.isoformat(), IN_SCOPE_REGIME),
    ):
        report.value_eur += row["market_value_eur"]

    for row in conn.execute(
        "SELECT d.gross_gain_eur FROM lot_disposal d "
        "JOIN transaction_ t ON t.id = d.sell_txn_id "
        "JOIN instrument i ON i.id = t.instrument_id "
        "WHERE i.tax_regime <> ? AND t.trade_date BETWEEN ? AND ?",
        (IN_SCOPE_REGIME, f"{tax_year}-01-01", f"{tax_year}-12-31"),
    ):
        if row["gross_gain_eur"] is not None:
            report.realized_gain_eur += row["gross_gain_eur"]

    # Instruments in scope but not yet computable are a different problem, and
    # a louder one: they belong in the figure and are missing from it.
    for row in conn.execute(
        "SELECT import_key FROM instrument WHERE tax_regime = ? AND teilfreistellung_pct IS NULL",
        (IN_SCOPE_REGIME,),
    ):
        report.instruments.append(row["import_key"])
        report.reasons[row["import_key"]] = (
            "in scope but Teilfreistellung unresolved -- excluded from the figure"
        )

    report.value_eur = quantize_money(report.value_eur)
    report.realized_gain_eur = quantize_money(report.realized_gain_eur)
    return report
