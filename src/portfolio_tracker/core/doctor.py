"""Identity checks against a built database.

Every figure this tool reports is derived, and a derived figure can be wrong in
two ways. It can be *implausible*, which someone notices. Or it can be
internally inconsistent -- a market value that is not quantity times price, a
gain split whose parts do not sum to the whole, contributions in the series that
do not match contributions in the ledger -- which nobody notices, because each
number looks fine on its own.

This module checks the second kind. The rules here are identities: statements
that must hold by construction, so a violation is a bug rather than a surprise.
Three of them have already caught real ones during development -- the flow
reconciliation caught a 1326% return, the unit identity caught 877%, and the
per-unit cost check caught a 36% understated cost basis on the largest holding.

Deliberately *not* checked here: whether the numbers match the source export.
That is the importer's job and it is a different kind of claim -- for the TRY
book the export is wrong, so agreeing with it would be the failure.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from portfolio_tracker.core import attribution, cash, fx, look_through, returns
from portfolio_tracker.core.calendars import TARGET2, grid_days
from portfolio_tracker.importers.report import Finding, Severity
from portfolio_tracker.ingest import look_through as look_through_ingest
from portfolio_tracker.money import ZERO

#: Identities are exact in principle; each side is independently rounded to the
#: cent on the way into the database, so a cent of disagreement is storage, not
#: arithmetic. Anything larger is a bug.
CENT = Decimal("0.01")

#: The unit-accounting identity accumulates rounding across a long series, so
#: it gets a slightly wider band than a single-row identity.
UNIT_TOLERANCE = Decimal("0.05")


@dataclass
class DoctorReport:
    checks_run: int = 0
    rows_examined: int = 0
    findings: list[Finding] = field(default_factory=list)

    def add(self, code: str, severity: Severity, message: str, **detail: object) -> None:
        self.findings.append(Finding(code, severity, message, dict(detail)))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def codes(self) -> set[str]:
        return {f.code for f in self.findings}

    @property
    def ok(self) -> bool:
        return not self.errors


def _d(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def _off_by(a: Decimal, b: Decimal, tolerance: Decimal = CENT) -> bool:
    return abs(a - b) > tolerance


# ---------------------------------------------------------------------------
# Checks. Each takes the connection and the report, and is independent of the
# others so one failure does not hide the next.
# ---------------------------------------------------------------------------


def check_lots(conn: sqlite3.Connection, report: DoctorReport) -> None:
    """A lot cannot go negative, and cannot be sold twice."""
    for row in conn.execute("SELECT id, qty_opened, qty_remaining FROM lot"):
        report.rows_examined += 1
        if _d(row["qty_remaining"]) < ZERO:
            report.add(
                "LOT_QTY_NEGATIVE",
                Severity.ERROR,
                f"lot {row['id']} has {row['qty_remaining']} units remaining",
                lot_id=row["id"],
            )
        if _d(row["qty_remaining"]) > _d(row["qty_opened"]):
            report.add(
                "LOT_QTY_GREW",
                Severity.ERROR,
                f"lot {row['id']} holds more than was ever bought",
                lot_id=row["id"],
            )

    for row in conn.execute(
        "SELECT l.id, l.qty_opened, l.qty_remaining, "
        "  (SELECT total(d.qty) FROM lot_disposal d WHERE d.lot_id = l.id) AS disposed "
        "FROM lot l"
    ):
        opened, remaining = _d(row["qty_opened"]), _d(row["qty_remaining"])
        disposed = Decimal(str(row["disposed"] or 0))
        if _off_by(opened, remaining + disposed, Decimal("0.0001")):
            report.add(
                "LOT_QUANTITY_UNBALANCED",
                Severity.ERROR,
                f"lot {row['id']}: opened {opened} but remaining {remaining} plus "
                f"disposed {disposed} is {remaining + disposed}",
                lot_id=row["id"],
            )


def check_position_identities(conn: sqlite3.Connection, report: DoctorReport) -> None:
    """The four identities every valued position must satisfy."""
    for row in conn.execute(
        "SELECT date, instrument_id, quantity, price_local, fx_rate, market_value_local, "
        "market_value_eur, cost_basis_eur, cost_basis_eur_at_spot, unrealized_eur, "
        "unrealized_local_eur, unrealized_fx_eur, valuation_status FROM position_daily"
    ):
        report.rows_examined += 1
        where = f"{row['instrument_id']} on {row['date']}"

        expected = _d(row["quantity"]) * _d(row["price_local"])
        if _off_by(expected, _d(row["market_value_local"])):
            report.add(
                "MV_NOT_QTY_TIMES_PRICE",
                Severity.ERROR,
                f"{where}: {row['quantity']} x {row['price_local']} is {expected:.2f}, "
                f"stored {row['market_value_local']}",
                date=row["date"],
                instrument_id=row["instrument_id"],
            )

        rate = _d(row["fx_rate"])
        if rate > ZERO and _off_by(
            _d(row["market_value_local"]) / rate, _d(row["market_value_eur"])
        ):
            report.add(
                "MV_EUR_NOT_LOCAL_OVER_RATE",
                Severity.ERROR,
                f"{where}: converting at {rate} does not reproduce {row['market_value_eur']}",
                date=row["date"],
                instrument_id=row["instrument_id"],
            )

        # The three-way split. If this fails the API is reporting a currency
        # effect that is not the difference between the two gains it shows.
        parts = _d(row["unrealized_local_eur"]) + _d(row["unrealized_fx_eur"])
        if _off_by(parts, _d(row["unrealized_eur"])):
            report.add(
                "GAIN_SPLIT_DOES_NOT_SUM",
                Severity.ERROR,
                f"{where}: local {row['unrealized_local_eur']} plus fx "
                f"{row['unrealized_fx_eur']} is {parts:.2f}, not {row['unrealized_eur']}",
                date=row["date"],
                instrument_id=row["instrument_id"],
            )

        spot_gap = _d(row["cost_basis_eur_at_spot"]) - _d(row["cost_basis_eur"])
        if _off_by(spot_gap, _d(row["unrealized_fx_eur"])):
            report.add(
                "FX_EFFECT_NOT_COST_REVALUATION",
                Severity.ERROR,
                f"{where}: the currency effect should be the cost basis re-translated "
                f"({spot_gap:.2f}), stored {row['unrealized_fx_eur']}",
                date=row["date"],
                instrument_id=row["instrument_id"],
            )


def check_fx(conn: sqlite3.Connection, report: DoctorReport) -> None:
    """One direction, always positive, and no gaps on the valuation grid."""
    for row in conn.execute("SELECT date, base, quote, rate FROM fx_rate"):
        report.rows_examined += 1
        if row["base"] != fx.BASE:
            report.add(
                "FX_BASE_NOT_EUR",
                Severity.ERROR,
                f"{row['date']} {row['base']}/{row['quote']}: rates are stored EUR-based only",
            )
        if _d(row["rate"]) <= ZERO:
            report.add(
                "FX_RATE_NOT_POSITIVE",
                Severity.ERROR,
                f"{row['date']} EUR/{row['quote']}: rate {row['rate']}",
            )

    bounds = conn.execute("SELECT min(date) AS lo, max(date) AS hi FROM position_daily").fetchone()
    if bounds is None or bounds["lo"] is None:
        return
    grid = set(
        grid_days(date.fromisoformat(bounds["lo"]), date.fromisoformat(bounds["hi"]), TARGET2)
    )
    for quote in fx.required_quotes(conn):
        have = {
            date.fromisoformat(r["date"])
            for r in conn.execute("SELECT date FROM fx_rate WHERE quote = ?", (quote,))
        }
        missing = sorted(grid - have)
        if missing:
            report.add(
                "FX_GRID_INCOMPLETE",
                Severity.WARNING,
                f"EUR/{quote}: {len(missing)} valuation day(s) have no rate, first "
                f"{missing[0]} -- those days are valued on a carried-forward rate",
                quote=quote,
                missing=len(missing),
            )


def check_portfolio(conn: sqlite3.Connection, report: DoctorReport) -> None:
    """The headline composition, the unit identity, and the flow reconciliation."""
    for person in conn.execute("SELECT id FROM person"):
        person_id = int(person["id"])
        rows = conn.execute(
            "SELECT * FROM portfolio_daily WHERE person_id = ? ORDER BY date", (person_id,)
        ).fetchall()
        if not rows:
            continue

        carried = ZERO
        for row in rows:
            report.rows_examined += 1

            # The rule the owner asked for, checked rather than trusted: the
            # headline is holdings plus deposits, and cash is not in it.
            headline = _d(row["market_value_eur"]) + _d(row["deposits_eur"])
            if _off_by(headline, _d(row["total_value_eur"])):
                report.add(
                    "TOTAL_IS_NOT_HOLDINGS_PLUS_DEPOSITS",
                    Severity.ERROR,
                    f"{row['date']}: holdings plus deposits is {headline:.2f}, "
                    f"total says {row['total_value_eur']}",
                    date=row["date"],
                )
            if _d(row["cash_eur"]) > CENT and not _off_by(
                _d(row["total_value_eur"]), headline + _d(row["cash_eur"])
            ):
                report.add(
                    "CASH_LEAKED_INTO_TOTAL",
                    Severity.ERROR,
                    f"{row['date']}: the total appears to include the uninvested cash",
                    date=row["date"],
                )

            basis = headline + _d(row["cash_eur"])
            if _off_by(basis, _d(row["valuation_basis_eur"]), UNIT_TOLERANCE):
                report.add(
                    "VALUATION_BASIS_WRONG",
                    Severity.ERROR,
                    f"{row['date']}: the return basis should be the total plus cash "
                    f"({basis:.2f}), stored {row['valuation_basis_eur']}",
                    date=row["date"],
                )

            if _d(row["income_cash_eur"]) > _d(row["cash_eur"]) + CENT:
                report.add(
                    "INCOME_EXCEEDS_CASH",
                    Severity.ERROR,
                    f"{row['date']}: income in cash {row['income_cash_eur']} exceeds the "
                    f"balance {row['cash_eur']}",
                    date=row["date"],
                )

            units, price = _d(row["units_outstanding"]), _d(row["unit_price"])
            if _off_by(units * price, _d(row["valuation_basis_eur"]), UNIT_TOLERANCE):
                report.add(
                    "UNIT_IDENTITY_BROKEN",
                    Severity.ERROR,
                    f"{row['date']}: {units} units at {price} is {units * price:.2f}, "
                    f"value {row['valuation_basis_eur']} -- the unit series has drifted "
                    "and every return figure derived from it is wrong",
                    date=row["date"],
                )
            carried += _d(row["net_external_flow_eur"])

        # The check that caught a 1326% return: a contribution dropped on its
        # way into the series is indistinguishable from investment growth.
        expected = returns.total_external_flows(conn, person_id)
        if _off_by(carried, expected, Decimal("0.05")):
            report.add(
                "FLOWS_DO_NOT_RECONCILE",
                Severity.ERROR,
                f"person {person_id}: {carried:.2f} of contributions in the daily series "
                f"against {expected:.2f} in the ledger -- the difference is being "
                "reported as return",
                person_id=person_id,
            )


def check_data_quality(conn: sqlite3.Connection, report: DoctorReport) -> None:
    """Not bugs: gaps the owner should know bound the numbers."""
    counts = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM transaction_ WHERE fees_known = 0
             AND type IN ('BUY', 'SELL')) AS no_fees,
          (SELECT count(*) FROM transaction_ WHERE quantity_confidence = 'ESTIMATED')
            AS estimated,
          (SELECT count(*) FROM transaction_ WHERE type = 'SELL'
             AND proceeds_disposition IS NULL) AS unresolved_sales,
          (SELECT count(*) FROM transaction_ WHERE confidence = 'RECONSTRUCTED'
             AND type IN ('DEPOSIT', 'WITHDRAWAL')) AS inferred_flows,
          (SELECT count(*) FROM instrument WHERE tax_regime = 'DE_INVESTMENT'
             AND teilfreistellung_pct IS NULL) AS no_teilfreistellung,
          (SELECT count(*) FROM pending_entry WHERE status = 'PROPOSED') AS pending,
          (SELECT count(*) FROM position_daily WHERE valuation_status <> 'OK'
             AND date = (SELECT max(date) FROM position_daily)) AS stale_today,
          (SELECT count(*) FROM position_daily WHERE valuation_status <> 'OK')
            AS stale_ever
        """
    ).fetchone()

    if counts["no_teilfreistellung"]:
        report.add(
            "TAX_CLASS_UNKNOWN",
            Severity.ERROR,
            f"{counts['no_teilfreistellung']} in-scope fund(s) have no Teilfreistellung "
            "rate; their tax figures are not computable and must not be defaulted to 30%",
        )
    if counts["estimated"]:
        report.add(
            "ESTIMATED_QUANTITY",
            Severity.WARNING,
            f"{counts['estimated']} trade(s) carry a quantity the source abbreviated and "
            "no override supplied",
        )
    if counts["unresolved_sales"]:
        report.add(
            "SALE_DISPOSITION_UNRESOLVED",
            Severity.WARNING,
            f"{counts['unresolved_sales']} sale(s) have no disposition; proceeds are "
            "assumed reinvested, which overstates the portfolio if they were withdrawn",
        )
    if counts["pending"]:
        report.add(
            "PENDING_UNREVIEWED",
            Severity.WARNING,
            f"{counts['pending']} inference(s) await a decision and are not in the ledger",
        )
    if counts["no_fees"]:
        report.add(
            "FEES_UNKNOWN",
            Severity.INFO,
            f"{counts['no_fees']} trade(s) have no fee data, so cost basis is understated "
            "and every computed taxable gain is biased high",
        )
    if counts["inferred_flows"]:
        report.add(
            "FLOWS_INFERRED",
            Severity.INFO,
            f"{counts['inferred_flows']} contribution(s) were reconstructed rather than "
            "recorded; XIRR rests on them",
        )
    if counts["stale_today"]:
        report.add(
            "PRICE_STALE",
            Severity.WARNING,
            f"{counts['stale_today']} position(s) are valued on a carried-forward price "
            "as of the latest valuation date",
        )
    if counts["stale_ever"]:
        # Not only today's figure: a day valued on a carried-forward price
        # feeds the return series for every period that contains it.
        report.add(
            "PRICE_STALE_IN_HISTORY",
            Severity.INFO,
            f"{counts['stale_ever']} position-day(s) across the whole history were valued "
            "without a fresh price; the daily returns for those days are not observations",
        )

    idle = conn.execute(
        "SELECT c.account_id, c.currency, c.balance_local, c.idle_days FROM cash_daily c "
        "WHERE c.date = (SELECT max(date) FROM cash_daily) AND c.idle_days > ?",
        (cash.IDLE_FLAG_DAYS,),
    ).fetchall()
    for row in idle:
        report.add(
            "CASH_IDLE",
            Severity.WARNING,
            f"account {row['account_id']}: {row['balance_local']} {row['currency']} has sat "
            f"untouched for {row['idle_days']} days -- if it was actually withdrawn, the "
            "portfolio is overstated by that much",
            account_id=row["account_id"],
        )


#: A fund claiming the equity exemption asserts its terms require at least this
#: much equity. The rate follows the Anlagebedingungen, not the holdings -- but
#: a fund claiming 30% while holding 20% shares is worth a second look.
EQUITY_EXEMPTION = Decimal("0.30")
EQUITY_MINIMUM = Decimal("0.51")

#: Constituent asset-class labels that count as equity. Issuers are not
#: consistent, so this matches loosely and the check stays a warning.
EQUITY_LABELS = ("equity", "share", "stock", "aktie")


def check_teilfreistellung_against_holdings(conn: sqlite3.Connection, report: DoctorReport) -> None:
    """Whether a fund's claimed exemption is consistent with what it holds.

    Only possible now that both look-through and the tax engine exist, and it is
    the one place they touch. A 30% Teilfreistellung is a claim that the fund's
    *Anlagebedingungen* require a 51% equity minimum -- so this is **not**
    authoritative and never an error: the rate follows the terms, not the
    portfolio, and a compliant equity fund can dip below 51% temporarily.

    But 30% versus 0% is 30% of the tax on the position, the classification is
    one a person entered by hand, and a fund claiming the equity exemption while
    holding mostly bonds is worth looking at before the figure reaches an
    *Anlage KAP*.
    """
    for row in conn.execute(
        "SELECT i.id, i.import_key, i.teilfreistellung_pct FROM instrument i "
        "WHERE i.is_fund = 1 AND i.teilfreistellung_pct IS NOT NULL"
    ):
        claimed = _d(row["teilfreistellung_pct"])
        if claimed < EQUITY_EXEMPTION:
            continue
        composition = look_through.composition_date(conn, int(row["id"]), date.today())
        if composition is None:
            continue

        published, weight_sum = composition
        if weight_sum <= ZERO:
            continue
        report.rows_examined += 1

        equity = ZERO
        for line in conn.execute(
            "SELECT weight, asset_class FROM fund_holding WHERE instrument_id = ? AND as_of = ?",
            (row["id"], published.isoformat()),
        ):
            label = str(line["asset_class"] or "").lower()
            if any(word in label for word in EQUITY_LABELS):
                equity += _d(line["weight"])

        # No asset-class column at all is a gap in the file, not a finding
        # about the fund. Reporting it as one would punish the wrong dialect.
        if equity <= ZERO:
            continue
        share = equity / weight_sum
        if share < EQUITY_MINIMUM:
            report.add(
                "TEILFREISTELLUNG_LOOKS_WRONG",
                Severity.WARNING,
                f"{row['import_key']} claims a {claimed:.0%} Teilfreistellung, which asserts "
                f"a {EQUITY_MINIMUM:.0%} equity minimum in its terms, but its published "
                f"composition of {published} is {share:.0%} equity. Check the KID or the "
                "Grundlagenbescheid -- the rate follows the fund's terms, not its holdings, "
                "so this is a prompt rather than a correction",
                instrument_id=row["id"],
            )


def check_vorabpauschale(conn: sqlite3.Connection, report: DoctorReport) -> None:
    """Whether the relief that stops double taxation is actually attached.

    Every euro of booked Vorabpauschale must be sitting on an open lot or have
    been claimed against a disposal. A euro that is in neither place has been
    taxed as a deemed distribution and will be taxed again as a capital gain,
    and nothing else in the system would notice: both figures are individually
    correct and the loss only appears years later, in a number nobody can
    reconcile.
    """
    for row in conn.execute(
        """
        SELECT t.account_id, t.instrument_id, i.import_key,
               total(t.gross_amount_eur) AS booked,
               (SELECT total(l.vap_accumulated_eur) FROM lot l
                  WHERE l.account_id = t.account_id AND l.instrument_id = t.instrument_id)
                 AS carried,
               (SELECT total(d.vap_offset_eur) FROM lot_disposal d
                  JOIN lot l2 ON l2.id = d.lot_id
                  WHERE l2.account_id = t.account_id AND l2.instrument_id = t.instrument_id)
                 AS claimed
        FROM transaction_ t JOIN instrument i ON i.id = t.instrument_id
        WHERE t.type = 'VORABPAUSCHALE'
        GROUP BY t.account_id, t.instrument_id
        """
    ):
        report.rows_examined += 1
        booked = Decimal(str(row["booked"] or 0))
        accounted = Decimal(str(row["carried"] or 0)) + Decimal(str(row["claimed"] or 0))
        if _off_by(booked, accounted, CENT):
            report.add(
                "VAP_RELIEF_LOST",
                Severity.ERROR,
                f"{row['import_key']}: EUR {booked} of Vorabpauschale booked but only "
                f"EUR {accounted} is carried on open lots or claimed against a disposal. "
                f"EUR {booked - accounted} will be taxed a second time on sale",
                instrument_id=row["instrument_id"],
            )

    # A forecast that was never proposed is not an error -- it is a decision
    # nobody has been asked to make yet -- but it is a tax charge the owner does
    # not know about, which is worth saying out loud.
    unbooked = conn.execute(
        "SELECT count(*) AS n, total(taxable_eur) AS eur FROM vorabpauschale "
        "WHERE status = 'FORECAST' AND taxable_eur > 0"
    ).fetchone()
    if unbooked and unbooked["n"]:
        report.add(
            "VAP_FORECAST_UNBOOKED",
            Severity.WARNING,
            f"{unbooked['n']} Vorabpauschale forecast(s) worth EUR "
            f"{Decimal(str(unbooked['eur'] or 0)):,.2f} of taxable income are not in the "
            "ledger; the tax figures do not include them",
        )


def check_attribution(conn: sqlite3.Connection, report: DoctorReport) -> None:
    """Whether the return's parts still add up to the return.

    Two identities, at two different levels, and they fail for different
    reasons. The daily one breaks if the decomposition and the unit series stop
    describing the same day. The period one breaks if the linking is wrong --
    and a wrong linking is invisible, because every daily figure it consumes is
    still correct and every number it produces is still plausible. Naive
    chaining on this portfolio understates the total by 11 percentage points and
    nothing about the output looks unusual.

    The period check also compares two independently computed period returns:
    the compounded daily returns from the attribution, and the unit price the
    valuation produced. They come from different code and must agree.
    """
    for row in conn.execute(
        "SELECT date, person_id, daily_return, ret_local_contrib, ret_fx_contrib, "
        "ret_income_contrib, ret_residual FROM portfolio_daily "
        "WHERE ret_local_contrib IS NOT NULL AND daily_return IS NOT NULL"
    ):
        report.rows_examined += 1
        parts = (
            _d(row["ret_local_contrib"])
            + _d(row["ret_fx_contrib"])
            + _d(row["ret_income_contrib"])
            + _d(row["ret_residual"])
        )
        if _off_by(parts, _d(row["daily_return"]), Decimal("0.0000001")):
            report.add(
                "ATTRIBUTION_DOES_NOT_SUM",
                Severity.ERROR,
                f"{row['date']}: price {row['ret_local_contrib']} plus currency "
                f"{row['ret_fx_contrib']} plus income {row['ret_income_contrib']} plus "
                f"unexplained {row['ret_residual']} is {parts}, not the day's return "
                f"{row['daily_return']}",
                date=row["date"],
            )

    for person in conn.execute("SELECT id FROM person"):
        person_id = int(person["id"])
        state = attribution.summarise(conn, person_id=person_id)
        if state is None:
            continue

        if abs(state.residual) > attribution.LINKING_TOLERANCE:
            report.add(
                "ATTRIBUTION_LINKING_RESIDUAL",
                Severity.ERROR,
                f"person {person_id}: the linked parts sum to "
                f"{state.total - state.residual} against a period return of {state.total} "
                f"-- a gap of {state.residual}. Cariño linking is exact, so a residual "
                "here means the smoothing is not being applied",
                person_id=person_id,
            )

        # Two code paths, one number. The unit series and the attribution never
        # look at each other, so agreement is evidence rather than tautology.
        summary = returns.summarise(conn, person_id=person_id)
        if summary.twr is not None and _off_by(state.total, summary.twr, Decimal("0.0001")):
            report.add(
                "ATTRIBUTION_TOTAL_DISAGREES_WITH_TWR",
                Severity.ERROR,
                f"person {person_id}: compounding the attributed daily returns gives "
                f"{state.total:.6%}, the unit series gives {summary.twr:.6%} -- one of "
                "the two is describing a different portfolio",
                person_id=person_id,
            )


def check_look_through(conn: sqlite3.Connection, report: DoctorReport) -> None:
    """Whether a stored composition still describes a whole fund.

    A holdings file is the one input here that arrives as a bulk load and is
    then used as a divisor. If rows go missing afterwards -- a partial delete, a
    half-applied import, a hand-edit -- the stored ``weight_sum`` no longer
    matches the rows it summarises, and every exposure figure derived from it is
    scaled by a number that describes a file that is no longer there. Nothing
    else in the system would notice: the percentages still add to 100%.
    """
    for row in conn.execute(
        "SELECT s.instrument_id, s.as_of, s.weight_sum, s.holdings, i.import_key, "
        "  (SELECT count(*) FROM fund_holding h "
        "     WHERE h.instrument_id = s.instrument_id AND h.as_of = s.as_of) AS rows_stored "
        "FROM fund_holding_set s JOIN instrument i ON i.id = s.instrument_id"
    ):
        report.rows_examined += 1
        where = f"{row['import_key']} as of {row['as_of']}"

        if row["rows_stored"] != row["holdings"]:
            report.add(
                "LOOK_THROUGH_ROWS_MISSING",
                Severity.ERROR,
                f"{where}: the file recorded {row['holdings']} holding(s) and "
                f"{row['rows_stored']} are stored -- the composition is incomplete and "
                "every exposure figure from it is scaled by a sum it no longer has",
                instrument_id=row["instrument_id"],
                as_of=row["as_of"],
            )

        stored = conn.execute(
            "SELECT weight FROM fund_holding WHERE instrument_id = ? AND as_of = ?",
            (row["instrument_id"], row["as_of"]),
        ).fetchall()
        actual = sum((_d(r["weight"]) for r in stored), ZERO)
        if _off_by(actual, _d(row["weight_sum"]), Decimal("0.000001")):
            report.add(
                "LOOK_THROUGH_SUM_MISMATCH",
                Severity.ERROR,
                f"{where}: the rows sum to {actual} but the set records {row['weight_sum']}",
                instrument_id=row["instrument_id"],
                as_of=row["as_of"],
            )

        # The tolerance the importer enforces. A row that violates it now was
        # either loaded by an older rule or edited since.
        if abs(_d(row["weight_sum"]) - Decimal(1)) > look_through_ingest.DEFAULT_TOLERANCE:
            report.add(
                "LOOK_THROUGH_WEIGHTS_UNBALANCED",
                Severity.ERROR,
                f"{where}: published weights sum to {row['weight_sum']}, which is not a "
                "whole fund; the breakdown scales a partial file up to 100%",
                instrument_id=row["instrument_id"],
                as_of=row["as_of"],
            )

    # Coverage is a property of the *portfolio*, not of any stored row, so it
    # is reported per person against what is actually held.
    for person in conn.execute("SELECT id FROM person"):
        try:
            funds = look_through.fund_coverage(conn, person_id=int(person["id"]))
        except look_through.NoValuation:
            continue
        held = sum((f.value_eur for f in funds), ZERO)
        if held <= ZERO:
            continue

        uncovered = [f for f in funds if f.is_fund and not f.covered]
        if uncovered:
            value = sum((f.value_eur for f in uncovered), ZERO)
            report.add(
                "LOOK_THROUGH_MISSING",
                Severity.WARNING,
                f"{len(uncovered)} fund(s) worth EUR {value:,.2f} ({value / held:.1%} of "
                f"holdings) have no published composition: "
                f"{', '.join(sorted(f.import_key for f in uncovered))} -- their currency "
                "and sector exposure is reported on the wrapper, not on what they hold",
                person_id=person["id"],
            )
        for fund in funds:
            if fund.stale:
                report.add(
                    "LOOK_THROUGH_STALE",
                    Severity.WARNING,
                    f"{fund.import_key}: composition is from {fund.composition_date}, "
                    f"{fund.stale_days} days before the latest valuation",
                    instrument_id=fund.instrument_id,
                )


def check_provider_ticker_alignment(conn: sqlite3.Connection, report: DoctorReport) -> None:
    """Flag instruments where a provider resolved a ticker that disagrees with the ledger.

    A mismatch typically means an instrument was seeded with an ISIN belonging
    to a different share class or a different fund entirely.
    """
    try:
        rows = conn.execute(
            """
            SELECT i.import_key, i.price_symbol, o.resolved_symbol
            FROM onvista_notation o
            JOIN instrument i ON i.id = o.instrument_id
            WHERE o.resolved_symbol IS NOT NULL
              AND (i.price_symbol IS NOT NULL OR i.import_key IS NOT NULL)
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return

    for row in rows:
        expected = (row["price_symbol"] or row["import_key"]).removesuffix(".DE").strip().upper()
        resolved = row["resolved_symbol"].strip().upper()
        if expected and resolved != expected:
            report.add(
                "PROVIDER_TICKER_MISMATCH",
                Severity.WARNING,
                f"{row['import_key']}: Onvista resolved ticker {row['resolved_symbol']!r} "
                f"does not match expected ticker {expected!r}. "
                "Check whether the instrument's ISIN points to the intended asset.",
            )


CHECKS = (
    check_lots,
    check_position_identities,
    check_fx,
    check_portfolio,
    check_data_quality,
    check_attribution,
    check_look_through,
    check_vorabpauschale,
    check_teilfreistellung_against_holdings,
    check_provider_ticker_alignment,
)


def run(conn: sqlite3.Connection) -> DoctorReport:
    """Run every check. Independent, so one failure does not mask the next."""
    report = DoctorReport()
    for check in CHECKS:
        check(conn, report)
        report.checks_run += 1
    return report
