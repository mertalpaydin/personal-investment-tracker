"""Reconstructing a ledger from a snapshot.

The source is a positions view, not a transaction log (see `docs/adr/0001`).
A row states a lot's *state* as of the export date, so the quantity actually
purchased is never printed anywhere. It is recovered by one rule:

    buy_quantity(key) = open_remaining(key) + sum(closed_quantity(key))

which covers all three real shapes uniformly: an ordinary open lot, a lot split
across both files, and a lot that appears twice in the closed file and never in
the open one.

The second job here is quantity refinement. The source abbreviates magnitudes at
or above 100,000, and this module recovers the true figure where the evidence
allows and reports an interval where it does not.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from enum import StrEnum

from portfolio_tracker.importers.investing_com.normalize import ClosedPosition, OpenPosition
from portfolio_tracker.importers.investing_com.parse import Currency, Interval
from portfolio_tracker.importers.report import ImportReport, Severity
from portfolio_tracker.money import plain

EXTERNAL_ID_VERSION = "invcom:v1"


class QtyConfidence(StrEnum):
    OVERRIDE = "OVERRIDE"
    EXACT = "EXACT"
    DERIVED = "DERIVED"
    ESTIMATED = "ESTIMATED"


@dataclass(frozen=True)
class RefinedQuantity:
    value: Decimal
    confidence: QtyConfidence
    interval: Interval
    note: str = ""


@dataclass(frozen=True)
class Disposal:
    qty: Decimal
    close_date: date
    close_price: Decimal
    net_pl_eur: Decimal
    occurrence: int
    line: int


@dataclass
class ReconstructedLot:
    key: str
    symbol: str
    name: str
    currency: Currency
    open_date: date
    open_price: Decimal
    quantity: RefinedQuantity  # the ORIGINAL purchase size
    remaining: Decimal
    disposals: list[Disposal] = field(default_factory=list)
    current_price: Decimal | None = None
    market_value_local: Decimal | None = None
    net_pl_eur: Decimal | None = None
    cost_basis_eur: Decimal | None = None
    basis_quality: str = "EXACT"

    @property
    def buy_external_id(self) -> str:
        return f"{EXTERNAL_ID_VERSION}:BUY:{self.key}"

    def sell_external_id(self, disposal: Disposal) -> str:
        return (
            f"{EXTERNAL_ID_VERSION}:SELL:{self.key}"
            f":{disposal.close_date.isoformat()}"
            f":{plain(disposal.close_price)}"
            f":{plain(disposal.qty)}#{disposal.occurrence}"
        )


def lot_key(symbol: str, open_date: date, open_price: Decimal) -> str:
    """``ZGLD:2025-05-07:19.88``.

    The price is normalised so ``19.88`` and ``19.880`` cannot diverge into two
    identities for one lot. Human-readable on purpose: an opaque hash makes the
    database uninspectable and duplicates invisible.
    """
    return f"{symbol}:{open_date.isoformat()}:{plain(open_price)}"


def refine_quantity(
    printed: Decimal,
    printed_interval: Interval,
    abbreviated: bool,
    *,
    market_value: Decimal | None = None,
    market_value_abbreviated: bool = False,
    market_value_interval: Interval | None = None,
    current_price: Decimal | None = None,
    override: Decimal | None = None,
) -> RefinedQuantity:
    """Recover the true quantity from the best evidence available.

    Order of preference:

    1. ``OVERRIDE`` -- an authoritative figure from a statement.
    2. ``EXACT`` -- printed in full, nothing to recover.
    3. ``DERIVED`` -- market value was printed in full, so
       ``market_value / current_price`` recovers the quantity exactly. Measured
       against the real file this reproduces the true figure to the unit, and
       shows the printed quantity is wrong by a few units.
    4. ``ESTIMATED`` -- market value was abbreviated too. Intersecting the two
       intervals still narrows the range substantially, but the midpoint is a
       midpoint and is labelled as such.
    """
    if override is not None:
        return RefinedQuantity(
            override, QtyConfidence.OVERRIDE, Interval(override, override), "user-supplied"
        )

    if not abbreviated:
        return RefinedQuantity(printed, QtyConfidence.EXACT, printed_interval)

    if market_value is None or current_price is None or current_price <= 0:
        return RefinedQuantity(
            printed, QtyConfidence.ESTIMATED, printed_interval, "no market value to derive from"
        )

    implied = market_value / current_price

    if not market_value_abbreviated:
        # The market value is exact, so the quotient is the true quantity up to
        # the rounding of the market value itself. Round to the nearest unit
        # when the quantity is large enough that fractional units are not used.
        candidate = implied.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        if printed_interval.contains(candidate):
            return RefinedQuantity(
                candidate,
                QtyConfidence.DERIVED,
                Interval(candidate, candidate),
                "market value / current price",
            )
        return RefinedQuantity(
            implied,
            QtyConfidence.ESTIMATED,
            printed_interval,
            "derived value fell outside the printed interval",
        )

    # Both abbreviated: intersect what each independently implies.
    narrowed = printed_interval
    if market_value_interval is not None:
        implied_interval = Interval(
            market_value_interval.lo / current_price, market_value_interval.hi / current_price
        )
        narrowed = printed_interval.intersect(implied_interval) or printed_interval

    # Quantise the bounds: dividing by a price yields 28 significant digits of
    # false precision, which reads as certainty the value does not have.
    narrowed = Interval(
        narrowed.lo.quantize(Decimal("0.01"), rounding=ROUND_DOWN),
        narrowed.hi.quantize(Decimal("0.01"), rounding=ROUND_UP),
    )
    midpoint = ((narrowed.lo + narrowed.hi) / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return RefinedQuantity(
        midpoint,
        QtyConfidence.ESTIMATED,
        narrowed,
        "both quantity and market value abbreviated",
    )


def build_lots(
    open_rows: list[OpenPosition],
    closed_rows: list[ClosedPosition],
    report: ImportReport,
    overrides: dict[str, Decimal] | None = None,
) -> list[ReconstructedLot]:
    """Group both files by lot identity and reconstruct each purchase."""
    overrides = overrides or {}

    by_key_open: dict[str, OpenPosition] = {}
    for open_position in open_rows:
        key = lot_key(open_position.symbol, open_position.open_date, open_position.open_price)
        if key in by_key_open:
            report.add(
                "DUPLICATE_OPEN_KEY",
                Severity.ERROR,
                f"{key} appears twice in the open file; the lot-identity assumption is broken",
                line=open_position.line,
            )
            continue
        by_key_open[key] = open_position

    by_key_closed: dict[str, list[ClosedPosition]] = defaultdict(list)
    for closed_position in closed_rows:
        closed_key = lot_key(
            closed_position.symbol, closed_position.open_date, closed_position.open_price
        )
        by_key_closed[closed_key].append(closed_position)

    # The closed file has no currency column at all, so a lot appearing only
    # there needs its currency from elsewhere. Any *other* lot of the same
    # symbol in the open file settles it; only a symbol that appears nowhere in
    # the open file is genuinely unknown.
    currency_by_symbol: dict[str, Currency] = {r.symbol: r.currency for r in open_rows}

    lots: list[ReconstructedLot] = []
    for key in sorted(set(by_key_open) | set(by_key_closed)):
        open_row = by_key_open.get(key)
        closed_for_key = sorted(by_key_closed.get(key, []), key=lambda r: (r.close_date, r.line))

        template = open_row or closed_for_key[0]
        if open_row is not None:
            currency: Currency = open_row.currency
        elif template.symbol in currency_by_symbol:
            currency = currency_by_symbol[template.symbol]
        else:
            currency = "EUR"
            report.add(
                "CURRENCY_ASSUMED",
                Severity.WARNING,
                f"{key}: symbol appears only in the closed file, which carries no currency; "
                "assuming EUR",
                key=key,
            )

        disposals = [
            Disposal(
                qty=row.quantity.value,
                close_date=row.close_date,
                close_price=row.close_price,
                net_pl_eur=row.net_pl_eur,
                occurrence=index,
                line=row.line,
            )
            for index, row in enumerate(closed_for_key)
        ]
        disposed = sum((d.qty for d in disposals), Decimal(0))

        if open_row is not None:
            refined = refine_quantity(
                open_row.quantity.value,
                open_row.quantity.interval,
                open_row.quantity.abbreviated,
                market_value=open_row.market_value.amount,
                market_value_abbreviated=open_row.market_value.abbreviated,
                market_value_interval=open_row.market_value.interval,
                current_price=open_row.current_price,
                override=overrides.get(key),
            )
            remaining = refined.value
            purchased = refined.value + disposed
        else:
            # Fully closed: the purchase is the sum of its disposals, and no
            # override path exists because there is no open row to correct.
            refined = RefinedQuantity(
                disposed, QtyConfidence.EXACT, Interval(disposed, disposed), "sum of disposals"
            )
            remaining = Decimal(0)
            purchased = disposed

        lot = ReconstructedLot(
            key=key,
            symbol=template.symbol,
            name=template.name,
            currency=currency,
            open_date=template.open_date,
            open_price=template.open_price,
            quantity=RefinedQuantity(purchased, refined.confidence, refined.interval, refined.note),
            remaining=remaining,
            disposals=disposals,
            current_price=open_row.current_price if open_row else None,
            market_value_local=open_row.market_value.amount if open_row else None,
            net_pl_eur=open_row.net_pl_eur if open_row else None,
        )

        # Cost basis. For euro rows `market value - profit` is exact to the cent
        # and implies a truer open price than the 2dp figure displayed. For lira
        # rows that route is unavailable, because the profit column is computed
        # at a single spot rate and carries no currency effect at all.
        if open_row is not None and currency == "EUR":
            lot.cost_basis_eur = open_row.market_value.amount - open_row.net_pl_eur
            lot.basis_quality = "DERIVED"
        else:
            lot.basis_quality = "ESTIMATED"

        if refined.confidence is QtyConfidence.OVERRIDE:
            _check_override_agrees(lot, open_row, refined, report)

        lots.append(lot)
        _report_shape(lot, open_row, closed_for_key, report)

    return lots


def _check_override_agrees(
    lot: ReconstructedLot,
    open_row: OpenPosition | None,
    refined: RefinedQuantity,
    report: ImportReport,
) -> None:
    """Warn when a supplied figure contradicts the arithmetic.

    The user has a statement and the importer does not, so the override still
    wins. But a silent disagreement between a typed number and the derivation
    is exactly the kind of thing a report exists to surface.
    """
    if open_row is None or not open_row.quantity.abbreviated:
        return
    if not open_row.quantity.interval.contains(refined.value):
        report.add(
            "OVERRIDE_CONTRADICTS_DERIVED",
            Severity.WARNING,
            f"{lot.key}: supplied quantity {plain(refined.value)} is outside the interval "
            f"[{plain(open_row.quantity.interval.lo)}, {plain(open_row.quantity.interval.hi)}] "
            "implied by the printed value",
            key=lot.key,
        )


def _report_shape(
    lot: ReconstructedLot,
    open_row: OpenPosition | None,
    closed_for_key: list[ClosedPosition],
    report: ImportReport,
) -> None:
    if open_row is not None and closed_for_key:
        report.add(
            "PARTIAL_CLOSE",
            Severity.INFO,
            f"{lot.key}: open {plain(lot.remaining)} + closed "
            f"{plain(lot.quantity.value - lot.remaining)} = {plain(lot.quantity.value)}",
            key=lot.key,
        )
    if len(closed_for_key) > 1:
        parts = " + ".join(plain(d.qty) for d in lot.disposals)
        report.add(
            "MULTI_DISPOSAL",
            Severity.INFO,
            f"{lot.key}: {len(closed_for_key)} disposals ({parts})",
            key=lot.key,
        )
    if open_row is None:
        report.add(
            "FULLY_CLOSED",
            Severity.INFO,
            f"{lot.key}: closed entirely; absent from the open file",
            key=lot.key,
        )
    if lot.quantity.confidence is QtyConfidence.ESTIMATED:
        report.add(
            "QUANTITY_ESTIMATED",
            Severity.WARNING,
            f"{lot.key}: quantity known only to "
            f"[{plain(lot.quantity.interval.lo)}, {plain(lot.quantity.interval.hi)}] "
            f"({lot.quantity.note}); supply an override",
            key=lot.key,
        )
    elif lot.quantity.confidence is QtyConfidence.DERIVED:
        report.add(
            "QUANTITY_DERIVED",
            Severity.INFO,
            f"{lot.key}: recovered {plain(lot.quantity.value)} from market value / price "
            "(printed value was abbreviated)",
            key=lot.key,
        )
