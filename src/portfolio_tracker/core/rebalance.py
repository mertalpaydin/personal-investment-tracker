"""Target-weight portfolio rebalancing and smart cash-inflow allocation.

Given current open positions and target weights across asset classes,
risk buckets, or individual instruments, this module calculates:

1. Current allocation vs. target allocation and percentage-point drift.
2. Inflow-Only Rebalancing (Tax-Efficient): optimal distribution of a new cash
   deposit (e.g. monthly savings) to bring weights closest to targets without
   selling any holdings or triggering capital gains events.
3. Full Rebalancing (Buys & Sells): exact buy/sell amounts and unit estimates
   to achieve target weights immediately.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from portfolio_tracker.core.peers import risk_level_for
from portfolio_tracker.money import ZERO, quantize_money

#: Standard target allocation presets for quick selection.
PRESETS: dict[str, dict[str, Any]] = {
    "world_em_80_20": {
        "label": "80/20 World & Emerging Markets",
        "dimension": "risk_bucket",
        "description": "Standard passive equity split: 80% developed world, 20% emerging markets.",
        "targets": {
            "EQ_GLOBAL_BROAD": Decimal("0.80"),
            "EQ_EM": Decimal("0.20"),
        },
    },
    "classic_60_40": {
        "label": "60/40 Stocks & Bonds",
        "dimension": "asset_class",
        "description": "Traditional balanced portfolio: 60% equities, 40% fixed income.",
        "targets": {
            "ETF": Decimal("0.60"),
            "BOND": Decimal("0.40"),
        },
    },
    "all_weather_approx": {
        "label": "All-Weather Diversified",
        "dimension": "risk_bucket",
        "description": (
            "Broad economic diversification: 50% equities, 25% bonds, "
            "15% gold/commodities, 10% crypto/cash."
        ),
        "targets": {
            "EQ_GLOBAL_BROAD": Decimal("0.40"),
            "EQ_EM": Decimal("0.10"),
            "BOND_EUR_GOVT": Decimal("0.25"),
            "COMMODITY_GOLD": Decimal("0.15"),
            "CRYPTO": Decimal("0.10"),
        },
    },
}


@dataclass
class InstrumentAllocation:
    instrument: str
    name: str
    currency: str
    risk_bucket: str | None
    risk_level: int
    asset_class: str
    latest_price_local: Decimal | None
    latest_price_eur: Decimal | None
    current_quantity: Decimal
    current_value_eur: Decimal
    target_value_eur: Decimal = ZERO
    suggested_trade_eur: Decimal = ZERO
    suggested_trade_qty: Decimal | None = None
    action: str = "HOLD"


@dataclass
class BucketStatus:
    bucket: str
    label: str
    current_value_eur: Decimal
    current_weight: Decimal
    target_weight: Decimal
    target_value_eur: Decimal
    drift_pp: Decimal  # (current_weight - target_weight) * 100
    drift_eur: Decimal  # current_value_eur - target_value_eur
    inflow_allocation_eur: Decimal = ZERO
    post_inflow_weight: Decimal = ZERO
    instruments: list[InstrumentAllocation] = field(default_factory=list)


@dataclass
class RebalancePlan:
    dimension: str
    total_current_value_eur: Decimal
    cash_inflow_eur: Decimal
    total_post_value_eur: Decimal
    max_drift_pp: Decimal
    buckets: list[BucketStatus]
    suggested_orders: list[dict[str, Any]]
    presets: dict[str, dict[str, Any]] = field(default_factory=lambda: PRESETS)


def compute_rebalance(
    conn: sqlite3.Connection,
    *,
    targets: dict[str, Decimal],
    dimension: str = "risk_bucket",
    cash_inflow_eur: Decimal = ZERO,
    person_id: int = 1,
) -> RebalancePlan:
    """Compute rebalancing metrics, drift, and trade allocations.

    :param conn: Database connection
    :param targets: Mapping of bucket/category name to target weight (0.0 to 1.0)
    :param dimension: 'risk_bucket' | 'asset_class' | 'instrument'
    :param cash_inflow_eur: Optional new cash to deploy without selling
    :param person_id: Person id
    """
    # Fetch current open positions with prices
    rows = conn.execute(
        """
        SELECT
            p.instrument,
            p.quantity,
            p.market_value_eur,
            p.price_local,
            p.price_eur,
            i.name,
            i.currency,
            i.asset_class,
            i.risk_bucket,
            i.risk_level
        FROM position_daily p
        JOIN (
            SELECT person_id, MAX(date) AS latest_date
            FROM portfolio_daily
            WHERE person_id = ?
        ) l ON l.person_id = p.person_id AND p.date = l.latest_date
        JOIN instrument i ON i.import_key = p.instrument
        WHERE p.person_id = ? AND p.quantity > 0
        ORDER BY p.market_value_eur DESC
        """,
        (person_id, person_id),
    ).fetchall()

    total_current_value = sum((Decimal(str(r["market_value_eur"])) for r in rows), ZERO)
    inflow = max(cash_inflow_eur, ZERO)
    total_post_value = total_current_value + inflow

    # Group positions by the requested dimension
    by_bucket: dict[str, list[InstrumentAllocation]] = {}
    for r in rows:
        bucket_key = (
            str(r["instrument"])
            if dimension == "instrument"
            else (
                str(r["asset_class"])
                if dimension == "asset_class"
                else str(r["risk_bucket"] or "UNCLASSIFIED")
            )
        )
        val_eur = Decimal(str(r["market_value_eur"]))
        qty = Decimal(str(r["quantity"]))
        px_local = Decimal(str(r["price_local"])) if r["price_local"] is not None else None
        px_eur = Decimal(str(r["price_eur"])) if r["price_eur"] is not None else None

        inst = InstrumentAllocation(
            instrument=r["instrument"],
            name=r["name"],
            currency=r["currency"],
            risk_bucket=r["risk_bucket"],
            risk_level=risk_level_for(r),
            asset_class=r["asset_class"],
            latest_price_local=px_local,
            latest_price_eur=px_eur,
            current_quantity=qty,
            current_value_eur=val_eur,
        )
        by_bucket.setdefault(bucket_key, []).append(inst)

    # Ensure all target keys exist even if currently zero holdings
    all_keys = set(by_bucket.keys()) | set(targets.keys())

    # Normalise targets to sum to 1.0 if not empty
    target_sum = sum(targets.values(), ZERO)
    normalised_targets: dict[str, Decimal] = {}
    if target_sum > ZERO:
        for k, v in targets.items():
            normalised_targets[k] = v / target_sum
    else:
        normalised_targets = targets

    bucket_statuses: list[BucketStatus] = []
    for k in sorted(all_keys):
        insts = by_bucket.get(k, [])
        curr_val = sum((i.current_value_eur for i in insts), ZERO)
        curr_weight = (curr_val / total_current_value) if total_current_value > ZERO else ZERO
        tgt_weight = normalised_targets.get(k, ZERO)
        tgt_val = quantize_money(tgt_weight * total_current_value)
        drift_eur = quantize_money(curr_val - tgt_val)
        drift_pp = (curr_weight - tgt_weight) * Decimal(100)

        bucket_statuses.append(
            BucketStatus(
                bucket=k,
                label=k.replace("_", " ").title(),
                current_value_eur=curr_val,
                current_weight=curr_weight,
                target_weight=tgt_weight,
                target_value_eur=tgt_val,
                drift_pp=drift_pp,
                drift_eur=drift_eur,
                instruments=insts,
            )
        )

    # Smart cash inflow allocation (waterfall / deficit-based allocation)
    if inflow > ZERO and total_post_value > ZERO:
        # Determine deficits: how much each bucket is below its post-inflow target value
        deficits: dict[str, Decimal] = {}
        for b in bucket_statuses:
            post_target_val = b.target_weight * total_post_value
            deficit = max(post_target_val - b.current_value_eur, ZERO)
            deficits[b.bucket] = deficit

        total_deficit = sum(deficits.values(), ZERO)
        allocated_inflow = ZERO

        if total_deficit > ZERO:
            for b in bucket_statuses:
                share = deficits[b.bucket] / total_deficit
                alloc = quantize_money(min(inflow * share, deficits[b.bucket]))
                b.inflow_allocation_eur = alloc
                allocated_inflow += alloc

        # Distribute any rounding remainder to most underweighted bucket
        remainder = inflow - allocated_inflow
        if remainder > ZERO:
            most_underweighted = min(bucket_statuses, key=lambda b: b.drift_pp, default=None)
            if most_underweighted:
                most_underweighted.inflow_allocation_eur += remainder

        # Compute post-inflow weights
        for b in bucket_statuses:
            b.post_inflow_weight = (
                b.current_value_eur + b.inflow_allocation_eur
            ) / total_post_value
    else:
        for b in bucket_statuses:
            b.post_inflow_weight = b.current_weight

    # Generate concrete suggested orders
    suggested_orders: list[dict[str, Any]] = []
    for b in bucket_statuses:
        # If inflow is specified, we generate buy orders for the inflow
        # If no inflow is specified, we generate rebalance orders (buys & sells)
        trade_amount = b.inflow_allocation_eur if inflow > ZERO else -b.drift_eur
        if trade_amount == ZERO or not b.instruments:
            continue

        action = "BUY" if trade_amount > ZERO else "SELL"
        abs_amount = abs(trade_amount)

        # Distribute trade across instruments in bucket (pro-rata to current holdings or primary)
        primary_inst = b.instruments[0]
        px = primary_inst.latest_price_eur or primary_inst.latest_price_local
        est_qty = (abs_amount / px).quantize(Decimal("0.0001")) if px and px > ZERO else None

        suggested_orders.append(
            {
                "bucket": b.bucket,
                "instrument": primary_inst.instrument,
                "name": primary_inst.name,
                "action": action,
                "amount_eur": abs_amount,
                "estimated_price_eur": px,
                "estimated_quantity": est_qty,
                "reason": (
                    f"Inflow allocation to reduce {b.bucket} underweight"
                    if inflow > ZERO
                    else f"Rebalance {b.bucket} to {b.target_weight * 100:.1f}% target"
                ),
            }
        )

    max_drift = max((abs(b.drift_pp) for b in bucket_statuses), default=ZERO)

    return RebalancePlan(
        dimension=dimension,
        total_current_value_eur=total_current_value,
        cash_inflow_eur=inflow,
        total_post_value_eur=total_post_value,
        max_drift_pp=max_drift,
        buckets=bucket_statuses,
        suggested_orders=suggested_orders,
    )
