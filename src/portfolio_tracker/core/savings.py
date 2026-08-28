"""How much you actually saved, as opposed to how much moved.

The distinction this module exists to draw:

    money crossing INTO the perimeter from outside   ->  savings
    money moving between accounts inside it          ->  not savings
    a dividend, reinvested                           ->  not savings, it is return
    a sale, with the cash sitting in the depot       ->  not savings

The perimeter is every account with ``in_perimeter = 1``. A salary current
account tracked only for its balance sits outside it, so money arriving from
there is genuinely new.

**Reinvested income is the rule that matters most.** A dividend that buys more
shares is a return being compounded. Counting it as a contribution does two
things at once, both flattering: it understates the return, because unit
accounting issues units against it and the growth those units go on to earn is
attributed to the contribution rather than to performance; and it overstates
the saving, because money you never earned appears as money you put aside.

The same is true of an internal transfer, which is why the pairing below
exists. Tagesgeld to depot is one movement showing up as two rows, and taken
at face value it is counted as both a withdrawal and a contribution.

**A person's classification always wins.** Anything marked ``USER`` is left
alone by the classifier and by the matcher, and survives a re-import.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

# FLOW_CLASS is imported rather than repeated: the same table decides what a
# trade is when it is recorded and what it is when savings are measured, and
# two copies is two chances for a purchase to count as a contribution.
from portfolio_tracker.core.ledger import FLOW_CLASS
from portfolio_tracker.money import ZERO, quantize_money

#: Days either side of a withdrawal to look for its matching deposit. Three,
#: because a SEPA transfer between two German banks routinely takes two.
PAIRING_WINDOW_DAYS = 3

#: How close two amounts must be to be one movement. Exact to the cent: a
#: transfer does not change size in flight, and a loose tolerance would pair a
#: transfer with a coincidental deposit of a similar size.
PAIRING_TOLERANCE = Decimal("0.01")


@dataclass
class Pairing:
    """One movement found twice, and what it cost to find."""

    out_txn_id: int
    in_txn_id: int
    amount_eur: Decimal
    days_apart: int


@dataclass
class ClassifyReport:
    classified: int = 0
    paired: int = 0
    ambiguous: list[str] = field(default_factory=list)
    respected_user_choices: int = 0

    @property
    def ok(self) -> bool:
        return not self.ambiguous


@dataclass
class Period:
    """One month of saving."""

    period: str
    gross_in_eur: Decimal = ZERO
    gross_out_eur: Decimal = ZERO
    net_new_money_eur: Decimal = ZERO
    cumulative_net_eur: Decimal = ZERO
    income_received_eur: Decimal = ZERO
    market_growth_eur: Decimal = ZERO
    closing_value_eur: Decimal = ZERO
    #: The same day's value *including* uninvested cash. The identity closes
    #: on this, because ``closing_value_eur`` deliberately leaves cash out and
    #: a residual computed against an incomplete series books every purchase
    #: as performance.
    closing_basis_eur: Decimal = ZERO
    #: What the accounts were already worth on the first day of any record.
    opening_value_eur: Decimal = ZERO
    #: Money the owner already had, arriving in this ledger for the first
    #: time. Real money that really entered the portfolio -- so it is not
    #: growth -- but not something set aside this month, so it is not saving.
    opening_transfer_eur: Decimal = ZERO


@dataclass
class Summary:
    """The answer to "am I actually saving", with what it rests on."""

    as_of: date
    this_month_eur: Decimal = ZERO
    year_to_date_eur: Decimal = ZERO
    trailing_12m_eur: Decimal = ZERO
    average_monthly_12m_eur: Decimal = ZERO
    cumulative_eur: Decimal = ZERO
    income_received_eur: Decimal = ZERO
    market_growth_eur: Decimal = ZERO
    closing_value_eur: Decimal = ZERO
    #: Everything ever brought in that was already the owner's.
    opening_transfer_eur: Decimal = ZERO
    #: What the accounts held on day one, before any of this was recorded.
    opening_value_eur: Decimal = ZERO
    #: Closing value including uninvested cash -- what the identity closes on.
    closing_basis_eur: Decimal = ZERO
    periods: list[Period] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    @property
    def reconciles(self) -> Decimal:
        """What you put in, plus what the market did. Equals the closing value.

        Income is deliberately **not** in this sum. Dividends and interest are
        paid into cash, and the headline value counts holdings and deposits
        only -- adding income here would double-count the part of it that was
        reinvested and invent the part that was not. It is reported beside the
        identity rather than inside it, which is the same treatment
        `/api/portfolio` gives uninvested cash.
        """
        return quantize_money(
            self.cumulative_eur
            + self.opening_transfer_eur
            + self.opening_value_eur
            + self.market_growth_eur
        )

    @property
    def reconciliation_gap(self) -> Decimal:
        """Zero, by construction. Non-zero means a flow went unclassified."""
        return quantize_money(self.reconciles - self.closing_basis_eur)

    @property
    def uninvested_cash_eur(self) -> Decimal:
        """The gap between what the identity closes on and what is shown.

        Sale proceeds and distributions the tracker has not seen spent. Small
        on a tidy ledger, and worth naming rather than hiding: it is the whole
        reason two value columns exist.
        """
        return quantize_money(self.closing_basis_eur - self.closing_value_eur)


# ---------------------------------------------------------------------------
# Classifying
# ---------------------------------------------------------------------------


def classify(conn: sqlite3.Connection, *, person_id: int = 1) -> ClassifyReport:
    """Set ``flow_class`` and ``is_new_savings``, then pair internal transfers.

    Idempotent, and safe to run after any import. Rows a person classified by
    hand are counted and skipped.
    """
    report = ClassifyReport()

    # Rows already recognised as half of a transfer are skipped: the type-based
    # mapping would put a DEPOSIT back to EXTERNAL_IN, and the matcher below
    # ignores anything already linked -- so the pair would silently come apart
    # and reappear as new money on the next rebuild.
    rows = conn.execute(
        "SELECT t.id, t.type, t.flow_class, t.flow_class_source FROM transaction_ t "
        "JOIN account a ON a.id = t.account_id WHERE a.person_id = ? "
        "AND t.id NOT IN (SELECT out_txn_id FROM internal_transfer_link) "
        "AND t.id NOT IN (SELECT in_txn_id FROM internal_transfer_link)",
        (person_id,),
    ).fetchall()

    for row in rows:
        if row["flow_class_source"] == "USER":
            report.respected_user_choices += 1
            continue
        expected = FLOW_CLASS.get(str(row["type"]))
        if expected is None:
            # VORABPAUSCHALE and SPLIT are neither a flow nor a cost; whatever
            # the importer decided is left alone rather than guessed at.
            continue
        if expected != row["flow_class"]:
            conn.execute(
                "UPDATE transaction_ SET flow_class = ? WHERE id = ?", (expected, row["id"])
            )
            report.classified += 1

    report.paired = len(pair_transfers(conn, person_id=person_id))
    report.ambiguous = _ambiguous(conn, person_id=person_id)
    _mark_new_savings(conn, person_id=person_id)
    return report


def _mark_new_savings(conn: sqlite3.Connection, *, person_id: int) -> None:
    """``is_new_savings`` is EXTERNAL_IN that is neither half of a transfer
    nor money the owner already had.

    An opening transfer stays EXTERNAL_IN on purpose -- the money did enter
    the portfolio, and hiding the flow would make it read as return -- so it
    has to be excluded here rather than by its flow class.
    """
    conn.execute(
        """
        UPDATE transaction_ SET is_new_savings = CASE
            WHEN flow_class = 'EXTERNAL_IN'
             AND is_opening_transfer = 0
             AND id NOT IN (SELECT in_txn_id FROM internal_transfer_link)
            THEN 1 ELSE 0 END
        WHERE account_id IN (SELECT id FROM account WHERE person_id = ?)
        """,
        (person_id,),
    )


def set_opening_transfer(conn: sqlite3.Connection, *, txn_id: int, is_opening: bool = True) -> None:
    """Mark a deposit as money that was already the owner's.

    Only the owner knows. A statement's first line looks identical whether it
    is a lifetime of saving arriving at once or a genuinely new payment, and
    the tool guessing wrong in either direction is what this exists to stop.
    """
    conn.execute(
        "UPDATE transaction_ SET is_opening_transfer = ? WHERE id = ?",
        (1 if is_opening else 0, txn_id),
    )


def candidates(
    conn: sqlite3.Connection, *, person_id: int = 1
) -> list[tuple[sqlite3.Row, list[sqlite3.Row]]]:
    """Unpaired withdrawals, and the deposits each could be paired with.

    Returned rather than resolved when there is more than one: two identical
    amounts on the same day are genuinely ambiguous, and guessing would
    silently erase a real contribution or invent one.
    """
    outs = conn.execute(
        """
        SELECT t.* FROM transaction_ t JOIN account a ON a.id = t.account_id
        WHERE a.person_id = ? AND a.in_perimeter = 1 AND t.flow_class = 'EXTERNAL_OUT'
          AND t.id NOT IN (SELECT out_txn_id FROM internal_transfer_link)
        ORDER BY t.trade_date
        """,
        (person_id,),
    ).fetchall()

    out: list[tuple[sqlite3.Row, list[sqlite3.Row]]] = []
    for withdrawal in outs:
        amount = withdrawal["gross_amount_eur"]
        if amount is None:
            continue
        when = date.fromisoformat(str(withdrawal["trade_date"]))
        matches = conn.execute(
            """
            SELECT t.* FROM transaction_ t JOIN account a ON a.id = t.account_id
            WHERE a.person_id = ? AND a.in_perimeter = 1 AND t.flow_class = 'EXTERNAL_IN'
              AND t.account_id <> ?
              AND t.trade_date BETWEEN ? AND ?
              AND abs(CAST(t.gross_amount_eur AS REAL) - ?) < ?
              AND t.id NOT IN (SELECT in_txn_id FROM internal_transfer_link)
            ORDER BY t.trade_date
            """,
            (
                person_id,
                withdrawal["account_id"],
                (when - timedelta(days=PAIRING_WINDOW_DAYS)).isoformat(),
                (when + timedelta(days=PAIRING_WINDOW_DAYS)).isoformat(),
                float(amount),
                float(PAIRING_TOLERANCE),
            ),
        ).fetchall()
        if matches:
            out.append((withdrawal, matches))
    return out


def pair_transfers(conn: sqlite3.Connection, *, person_id: int = 1) -> list[Pairing]:
    """Link each withdrawal to its deposit, where exactly one candidate fits."""
    made: list[Pairing] = []
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    for withdrawal, matches in candidates(conn, person_id=person_id):
        if len(matches) != 1:
            # Ambiguous. Left for a person, and reported by `_ambiguous`.
            continue
        deposit = matches[0]
        amount = Decimal(str(withdrawal["gross_amount_eur"]))
        days = abs(
            (
                date.fromisoformat(str(deposit["trade_date"]))
                - date.fromisoformat(str(withdrawal["trade_date"]))
            ).days
        )
        conn.execute(
            "INSERT OR IGNORE INTO internal_transfer_link "
            "(out_txn_id, in_txn_id, amount_eur, matched_by, detected_at) "
            "VALUES (?, ?, ?, 'AUTO', ?)",
            (withdrawal["id"], deposit["id"], str(amount), now),
        )
        # Both halves become internal: net household external flow is zero,
        # which is the correct answer and what unit accounting needs.
        for txn_id in (withdrawal["id"], deposit["id"]):
            conn.execute(
                "UPDATE transaction_ SET flow_class = 'INTERNAL_TRANSFER' "
                "WHERE id = ? AND flow_class_source = 'AUTO'",
                (txn_id,),
            )
        made.append(
            Pairing(
                out_txn_id=int(withdrawal["id"]),
                in_txn_id=int(deposit["id"]),
                amount_eur=amount,
                days_apart=days,
            )
        )
    return made


def _ambiguous(conn: sqlite3.Connection, *, person_id: int) -> list[str]:
    return [
        f"{w['trade_date']}: EUR {Decimal(str(w['gross_amount_eur'])):,.2f} out of account "
        f"{w['account_id']} matches {len(m)} deposits. Say which, or say it is new money."
        for w, m in candidates(conn, person_id=person_id)
        if len(m) > 1
    ]


def set_flow_class(
    conn: sqlite3.Connection,
    txn_id: int,
    *,
    flow_class: str,
    savings_label: str | None = None,
) -> None:
    """Record a person's decision. Sticky: the classifier never overrides it."""
    if flow_class not in (
        "EXTERNAL_IN",
        "EXTERNAL_OUT",
        "INTERNAL_TRANSFER",
        "INVESTMENT",
        "INCOME",
        "COST",
    ):
        raise ValueError(f"{flow_class!r} is not a flow class")
    conn.execute(
        "UPDATE transaction_ SET flow_class = ?, savings_label = ?, "
        "flow_class_source = 'USER' WHERE id = ?",
        (flow_class, savings_label, txn_id),
    )
    # A row a person has reclassified is no longer half of an inferred pair.
    conn.execute(
        "DELETE FROM internal_transfer_link WHERE (out_txn_id = ? OR in_txn_id = ?) "
        "AND matched_by = 'AUTO'",
        (txn_id, txn_id),
    )


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------


def build_periods(conn: sqlite3.Connection, *, person_id: int = 1) -> int:
    """Recompute `savings_period` from the ledger. Returns rows written."""
    conn.execute("DELETE FROM savings_period WHERE person_id = ?", (person_id,))

    flows = conn.execute(
        """
        SELECT substr(t.trade_date, 1, 7) AS period,
               total(CASE WHEN t.flow_class = 'EXTERNAL_IN'
                           AND t.is_opening_transfer = 0
                          THEN CAST(t.gross_amount_eur AS REAL) ELSE 0 END) AS gross_in,
               total(CASE WHEN t.is_opening_transfer = 1
                          THEN CAST(t.gross_amount_eur AS REAL) ELSE 0 END) AS opening_in,
               total(CASE WHEN t.flow_class = 'EXTERNAL_OUT'
                          THEN CAST(t.gross_amount_eur AS REAL) ELSE 0 END) AS gross_out,
               total(CASE WHEN t.flow_class = 'INCOME'
                          THEN CAST(t.gross_amount_eur AS REAL) ELSE 0 END) AS income
        FROM transaction_ t JOIN account a ON a.id = t.account_id
        WHERE a.person_id = ? AND a.in_perimeter = 1
        GROUP BY period ORDER BY period
        """,
        (person_id,),
    ).fetchall()

    # Closing figures per month, from the last valuation in each. Both
    # columns: the headline one to show, the basis one to reconcile against.
    values: dict[str, tuple[Decimal, Decimal]] = {
        str(r["period"]): (Decimal(str(r["v"])), Decimal(str(r["basis"])))
        for r in conn.execute(
            """
            SELECT substr(date, 1, 7) AS period, total_value_eur AS v,
                   valuation_basis_eur AS basis
            FROM portfolio_daily
            WHERE person_id = ? AND date IN (
                SELECT max(date) FROM portfolio_daily WHERE person_id = ?
                GROUP BY substr(date, 1, 7))
            """,
            (person_id, person_id),
        )
    }

    # What the accounts were already worth on the first day of any record.
    # Everything before it is outside what this tool claims to know, so it
    # belongs in "already yours" -- not in a residual that would call it
    # performance the portfolio never earned.
    opening_row = conn.execute(
        "SELECT date, valuation_basis_eur FROM portfolio_daily WHERE person_id = ? "
        "ORDER BY date LIMIT 1",
        (person_id,),
    ).fetchone()
    opening_value = ZERO
    if opening_row is not None:
        # Net of anything already recorded by the close of that first day.
        # The column is an end-of-day figure, so a deposit made that morning
        # is inside it; counting both would put the same money in the sum
        # twice and drive growth negative by exactly that amount.
        already = conn.execute(
            """
            SELECT total(CASE WHEN t.flow_class = 'EXTERNAL_IN'
                              THEN CAST(t.gross_amount_eur AS REAL)
                              WHEN t.flow_class = 'EXTERNAL_OUT'
                              THEN -CAST(t.gross_amount_eur AS REAL) ELSE 0 END) AS net
            FROM transaction_ t JOIN account a ON a.id = t.account_id
            WHERE a.person_id = ? AND a.in_perimeter = 1 AND t.trade_date <= ?
            """,
            (person_id, str(opening_row["date"])),
        ).fetchone()["net"]
        opening_value = quantize_money(
            Decimal(str(opening_row["valuation_basis_eur"])) - Decimal(str(already or 0))
        )

    # Every month that has either a movement or a valuation. Driving this off
    # the flows alone skipped months where nothing was paid in, which left the
    # last row -- and so the headline value the identity closes on -- stale
    # whenever the most recent month happened to be a quiet one.
    by_period = {str(r["period"]): r for r in flows}
    if not by_period and not values:
        return 0

    written = 0
    cumulative = ZERO
    previous_value = ZERO
    first = True
    for period in sorted(set(by_period) | set(values)):
        row = by_period.get(period)
        gross_in = quantize_money(Decimal(str(row["gross_in"]))) if row else ZERO
        gross_out = quantize_money(Decimal(str(row["gross_out"]))) if row else ZERO
        opening_in = quantize_money(Decimal(str(row["opening_in"]))) if row else ZERO
        income = quantize_money(Decimal(str(row["income"]))) if row else ZERO
        net = gross_in - gross_out
        cumulative += net
        opening_here = opening_value if first else ZERO
        first = False
        closing, basis = values.get(period, (previous_value, previous_value))
        # The residual: whatever the value did that was not money moving in or
        # out. Derived rather than summed from prices, so the parts add up to
        # the value by construction -- which is also why an opening transfer
        # has to be subtracted here. Leave it in and a lifetime of saving
        # arriving in one month is reported as a month of market growth.
        growth = quantize_money(basis - previous_value - net - opening_in - opening_here)

        conn.execute(
            """
            INSERT INTO savings_period (
                person_id, period, gross_in_eur, gross_out_eur, net_new_money_eur,
                cumulative_net_eur, income_received_eur, market_growth_eur,
                closing_value_eur, opening_transfer_eur, closing_basis_eur,
                opening_value_eur
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                person_id,
                period,
                str(gross_in),
                str(gross_out),
                str(net),
                str(cumulative),
                str(income),
                str(growth),
                str(closing),
                str(opening_in),
                str(basis),
                str(opening_here),
            ),
        )
        # The basis, not the headline: the next month's growth is measured
        # against a complete series or it inherits the same error.
        previous_value = basis
        written += 1
    return written


def summarise(conn: sqlite3.Connection, *, person_id: int = 1) -> Summary:
    """Net new money over the windows that matter, and what it rests on."""
    rows = conn.execute(
        "SELECT * FROM savings_period WHERE person_id = ? ORDER BY period", (person_id,)
    ).fetchall()
    if not rows:
        return Summary(as_of=date.today(), caveats=["No savings history has been computed yet."])

    periods = [
        Period(
            period=str(r["period"]),
            gross_in_eur=Decimal(str(r["gross_in_eur"])),
            gross_out_eur=Decimal(str(r["gross_out_eur"])),
            net_new_money_eur=Decimal(str(r["net_new_money_eur"])),
            cumulative_net_eur=Decimal(str(r["cumulative_net_eur"])),
            income_received_eur=Decimal(str(r["income_received_eur"])),
            market_growth_eur=Decimal(str(r["market_growth_eur"])),
            closing_value_eur=Decimal(str(r["closing_value_eur"])),
            opening_transfer_eur=Decimal(str(r["opening_transfer_eur"])),
            closing_basis_eur=Decimal(str(r["closing_basis_eur"])),
            opening_value_eur=Decimal(str(r["opening_value_eur"])),
        )
        for r in rows
    ]

    latest = periods[-1]
    year = latest.period[:4]
    trailing = periods[-12:]

    out = Summary(
        as_of=date.today(),
        this_month_eur=latest.net_new_money_eur,
        year_to_date_eur=quantize_money(
            sum((p.net_new_money_eur for p in periods if p.period.startswith(year)), ZERO)
        ),
        trailing_12m_eur=quantize_money(sum((p.net_new_money_eur for p in trailing), ZERO)),
        cumulative_eur=latest.cumulative_net_eur,
        income_received_eur=quantize_money(sum((p.income_received_eur for p in periods), ZERO)),
        market_growth_eur=quantize_money(sum((p.market_growth_eur for p in periods), ZERO)),
        closing_value_eur=latest.closing_value_eur,
        opening_transfer_eur=quantize_money(sum((p.opening_transfer_eur for p in periods), ZERO)),
        opening_value_eur=quantize_money(sum((p.opening_value_eur for p in periods), ZERO)),
        closing_basis_eur=latest.closing_basis_eur,
        periods=periods,
    )
    out.average_monthly_12m_eur = (
        quantize_money(out.trailing_12m_eur / Decimal(len(trailing))) if trailing else ZERO
    )

    inferred = conn.execute(
        "SELECT count(*) AS n FROM transaction_ t JOIN account a ON a.id = t.account_id "
        "WHERE a.person_id = ? AND t.confidence = 'RECONSTRUCTED' "
        "AND t.flow_class IN ('EXTERNAL_IN', 'EXTERNAL_OUT')",
        (person_id,),
    ).fetchone()["n"]
    if inferred:
        out.caveats.append(
            f"{inferred} of these movements were inferred from purchases rather than "
            "recorded, so they are dated the day the money was invested, not the day "
            "it arrived."
        )

    linked = conn.execute("SELECT count(*) AS n FROM internal_transfer_link").fetchone()["n"]
    if linked:
        out.caveats.append(
            f"{linked} pair(s) of movements were recognised as one transfer between "
            "your own accounts, and are not counted as new money."
        )
    if out.opening_transfer_eur:
        months = sorted({p.period for p in periods if p.opening_transfer_eur})
        out.caveats.append(
            f"EUR {out.opening_transfer_eur:,.2f} arriving in {', '.join(months)} was "
            "money you already had, brought into this tool when the account joined it. "
            "It is counted in what you are worth, and not counted as saving -- you set "
            "it aside before any of this was being recorded."
        )
    out.caveats.append(
        "Dividends and interest are shown separately and are never counted as "
        "savings: money the portfolio earned is a return, not something you put aside."
    )
    if abs(out.reconciliation_gap) > Decimal("0.01"):
        # Should be impossible: market growth is the residual, so the parts add
        # up by construction. If it ever fires, a flow was classified as
        # something the sum does not account for.
        out.caveats.append(
            f"BREAK: money in plus market growth comes to EUR "
            f"{out.reconciles:,.2f}, and the accounts hold EUR "
            f"{out.closing_basis_eur:,.2f}. The difference is a flow this "
            "does not account for."
        )
    return out
