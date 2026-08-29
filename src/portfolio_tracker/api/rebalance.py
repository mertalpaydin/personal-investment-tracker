"""Rebalancing calculator and allocation target API endpoints."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from portfolio_tracker.api.deps import get_db
from portfolio_tracker.core import rebalance

router = APIRouter(prefix="/api/rebalance", tags=["rebalance"])


class RebalanceRequest(BaseModel):
    targets: dict[str, Decimal] = Field(
        default_factory=dict,
        description=(
            "Target weights mapping category/bucket names to decimals "
            "(e.g. {'EQ_GLOBAL_BROAD': 0.8, 'EQ_EM': 0.2})"
        ),
    )
    dimension: str = Field(
        default="risk_bucket",
        description="Dimension to group and balance by: 'risk_bucket', 'asset_class', 'instrument'",
    )
    cash_inflow_eur: Decimal = Field(
        default=Decimal(0),
        ge=0,
        description="Optional cash deposit to allocate across underweighted holdings",
    )


@router.get("/presets")
def get_presets() -> dict[str, Any]:
    """Pre-configured standard allocation models (e.g. 80/20 World/EM, 60/40)."""
    return {
        "presets": [
            {
                "id": k,
                "label": v["label"],
                "dimension": v["dimension"],
                "description": v["description"],
                "targets": {tk: float(tv) for tk, tv in v["targets"].items()},
            }
            for k, v in rebalance.PRESETS.items()
        ]
    }


@router.post("/calculate")
def calculate_rebalance(
    req: RebalanceRequest,
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Calculate portfolio drift, rebalancing trades, and smart cash inflow allocations."""
    plan = rebalance.compute_rebalance(
        conn,
        targets=req.targets,
        dimension=req.dimension,
        cash_inflow_eur=req.cash_inflow_eur,
        person_id=person_id,
    )

    return {
        "dimension": plan.dimension,
        "total_current_value_eur": float(plan.total_current_value_eur),
        "cash_inflow_eur": float(plan.cash_inflow_eur),
        "total_post_value_eur": float(plan.total_post_value_eur),
        "max_drift_pp": float(plan.max_drift_pp),
        "buckets": [
            {
                "bucket": b.bucket,
                "label": b.label,
                "current_value_eur": float(b.current_value_eur),
                "current_weight": float(b.current_weight),
                "target_weight": float(b.target_weight),
                "target_value_eur": float(b.target_value_eur),
                "drift_pp": float(b.drift_pp),
                "drift_eur": float(b.drift_eur),
                "inflow_allocation_eur": float(b.inflow_allocation_eur),
                "post_inflow_weight": float(b.post_inflow_weight),
                "instruments": [
                    {
                        "instrument": i.instrument,
                        "name": i.name,
                        "currency": i.currency,
                        "risk_bucket": i.risk_bucket,
                        "risk_level": i.risk_level,
                        "asset_class": i.asset_class,
                        "current_quantity": float(i.current_quantity),
                        "current_value_eur": float(i.current_value_eur),
                        "latest_price_local": float(i.latest_price_local)
                        if i.latest_price_local is not None
                        else None,
                        "latest_price_eur": float(i.latest_price_eur)
                        if i.latest_price_eur is not None
                        else None,
                    }
                    for i in b.instruments
                ],
            }
            for b in plan.buckets
        ],
        "suggested_orders": [
            {
                "bucket": o["bucket"],
                "instrument": o["instrument"],
                "name": o["name"],
                "action": o["action"],
                "amount_eur": float(o["amount_eur"]),
                "estimated_price_eur": float(o["estimated_price_eur"])
                if o.get("estimated_price_eur") is not None
                else None,
                "estimated_quantity": float(o["estimated_quantity"])
                if o.get("estimated_quantity") is not None
                else None,
                "reason": o["reason"],
            }
            for o in plan.suggested_orders
        ],
    }
