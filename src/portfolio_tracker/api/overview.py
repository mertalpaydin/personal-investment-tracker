"""Everything the front page shows, over one chosen window.

A single endpoint rather than six, because the window has to be the same for
all of them. A page whose headline says "today" while the chart beside it says
"since inception" invites the two to be read together, and they are not
comparable.

The window is resolved from the **last valuation date**, never from today.
`position_daily` and `portfolio_daily` carry a row per business day, so
anchoring to today makes every figure vanish at the weekend.

Two figures are reported per holding and they are easy to confuse, so both are
always present and named differently:

``own_return``
    what that holding did, on its own money. A holding up 99% shows 99%.

``contribution``
    how many percentage points of the *portfolio's* return came from it. The
    same holding at 6% of the portfolio contributes about 6.8 points.

Showing only the second -- which this page used to -- reads as though a
doubled holding had barely moved.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from portfolio_tracker.api.deps import get_db
from portfolio_tracker.core import attribution, cash, goal, returns
from portfolio_tracker.money import ZERO

router = APIRouter(tags=["overview"])

#: The windows the page offers, longest label first in the UI's own order.
RANGES: tuple[str, ...] = ("1D", "1W", "1M", "3M", "6M", "YTD", "1Y", "ALL")

#: What each one means, in days back from the last valuation. YTD and ALL are
#: special-cased below.
_DAYS: dict[str, int] = {"1W": 7, "1M": 31, "3M": 92, "6M": 183, "1Y": 366}

_LABELS: dict[str, str] = {
    "1D": "since the previous day",
    "1W": "over the last week",
    "1M": "over the last month",
    "3M": "over three months",
    "6M": "over six months",
    "YTD": "so far this year",
    "1Y": "over the last year",
    "ALL": "since you started",
}


@dataclass(frozen=True)
class Window:
    start: date
    end: date
    label: str
    key: str


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, Decimal) else None


def resolve(conn: sqlite3.Connection, person_id: int, key: str) -> Window:
    """Turn a range name into two dates that exist in the data.

    ``start`` is snapped back to a day the portfolio was actually valued, so a
    window boundary landing on a weekend compares against the Friday rather
    than against nothing.
    """
    row = conn.execute(
        "SELECT max(date) AS d, min(date) AS f FROM portfolio_daily WHERE person_id = ?",
        (person_id,),
    ).fetchone()
    if row is None or row["d"] is None:
        raise HTTPException(404, "no portfolio history; recompute first")

    end = date.fromisoformat(str(row["d"]))
    first = date.fromisoformat(str(row["f"]))

    if key == "ALL":
        start = first
    elif key == "YTD":
        start = max(first, date(end.year, 1, 1))
    elif key == "1D":
        previous = conn.execute(
            "SELECT max(date) AS d FROM portfolio_daily WHERE person_id = ? AND date < ?",
            (person_id, end.isoformat()),
        ).fetchone()
        start = date.fromisoformat(str(previous["d"])) if previous and previous["d"] else first
    else:
        start = max(first, end - timedelta(days=_DAYS.get(key, 31)))

    # Snap to a day that exists, so the comparison has something to compare to.
    snapped = conn.execute(
        "SELECT max(date) AS d FROM portfolio_daily WHERE person_id = ? AND date <= ?",
        (person_id, start.isoformat()),
    ).fetchone()
    if snapped and snapped["d"]:
        start = date.fromisoformat(str(snapped["d"]))

    return Window(start=start, end=end, label=_LABELS.get(key, key), key=key)


def _value_on(conn: sqlite3.Connection, person_id: int, on: date) -> Decimal:
    row = conn.execute(
        "SELECT total_value_eur FROM portfolio_daily WHERE person_id = ? AND date = ?",
        (person_id, on.isoformat()),
    ).fetchone()
    return Decimal(str(row["total_value_eur"])) if row else ZERO


def _holdings(conn: sqlite3.Connection, person_id: int, window: Window) -> list[dict[str, Any]]:
    """Each holding's own return over the window, and its value change.

    Measured **per unit** -- market value divided by quantity -- so buying more
    of something during the window does not read as a gain.

    A holding first bought inside the window is measured from the first day it
    was held rather than reported as unknown. "Bought three weeks ago, up 4%
    since" is the answer somebody wants; a dash where a number belongs reads as
    a fault. `held_from` says which it is.
    """
    rows = conn.execute(
        """
        SELECT i.import_key, i.name, i.currency, p.quantity, p.market_value_eur,
               p.unrealized_eur, p.valuation_status,
               -- The position as the window opened, or on the first day it
               -- existed if it was bought part-way through.
               COALESCE(
                 (SELECT q.market_value_eur FROM position_daily q
                   WHERE q.instrument_id = p.instrument_id AND q.account_id = p.account_id
                     AND q.date <= ? ORDER BY q.date DESC LIMIT 1),
                 (SELECT q.market_value_eur FROM position_daily q
                   WHERE q.instrument_id = p.instrument_id AND q.account_id = p.account_id
                     AND q.date >= ? ORDER BY q.date ASC LIMIT 1)
               ) AS then_value,
               COALESCE(
                 (SELECT q.quantity FROM position_daily q
                   WHERE q.instrument_id = p.instrument_id AND q.account_id = p.account_id
                     AND q.date <= ? ORDER BY q.date DESC LIMIT 1),
                 (SELECT q.quantity FROM position_daily q
                   WHERE q.instrument_id = p.instrument_id AND q.account_id = p.account_id
                     AND q.date >= ? ORDER BY q.date ASC LIMIT 1)
               ) AS then_qty,
               COALESCE(
                 (SELECT q.date FROM position_daily q
                   WHERE q.instrument_id = p.instrument_id AND q.account_id = p.account_id
                     AND q.date <= ? ORDER BY q.date DESC LIMIT 1),
                 (SELECT q.date FROM position_daily q
                   WHERE q.instrument_id = p.instrument_id AND q.account_id = p.account_id
                     AND q.date >= ? ORDER BY q.date ASC LIMIT 1)
               ) AS held_from
        FROM position_daily p
        JOIN instrument i ON i.id = p.instrument_id
        JOIN account a ON a.id = p.account_id
        WHERE p.date = ? AND a.person_id = ?
        ORDER BY p.market_value_eur DESC
        """,
        (
            window.start.isoformat(),
            window.start.isoformat(),
            window.start.isoformat(),
            window.start.isoformat(),
            window.start.isoformat(),
            window.start.isoformat(),
            window.end.isoformat(),
            person_id,
        ),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        now_value = Decimal(str(r["market_value_eur"]))
        now_qty = Decimal(str(r["quantity"]))
        then_value = Decimal(str(r["then_value"])) if r["then_value"] is not None else None
        then_qty = Decimal(str(r["then_qty"])) if r["then_qty"] is not None else None

        own: Decimal | None = None
        if then_value is not None and then_qty and now_qty and then_qty > ZERO:
            before, after = then_value / then_qty, now_value / now_qty
            if before > ZERO:
                own = after / before - 1

        out.append(
            {
                "instrument": r["import_key"],
                "name": r["name"],
                "currency": r["currency"],
                "value_eur": _num(now_value),
                "value_change_eur": (
                    _num(now_value - then_value) if then_value is not None else None
                ),
                # What this holding did, on its own money.
                "own_return": _num(own),
                # When the measurement actually starts. Later than the window
                # for anything bought inside it.
                "held_from": r["held_from"],
                "unrealized_eur": _num(r["unrealized_eur"]),
                "valuation_status": r["valuation_status"],
            }
        )
    return out


@router.get("/api/overview")
def overview(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
    range_key: str = Query("1D", alias="range"),
) -> dict[str, Any]:
    """The front page, measured over one window."""
    key = range_key.upper()
    if key not in RANGES:
        raise HTTPException(422, f"range must be one of {', '.join(RANGES)}")

    window = resolve(conn, person_id, key)
    opening = _value_on(conn, person_id, window.start)
    closing = _value_on(conn, person_id, window.end)

    summary = returns.summarise(conn, person_id=person_id, start=window.start, end=window.end)
    split = attribution.summarise(conn, person_id=person_id, start=window.start, end=window.end)
    per_holding = attribution.by_holding(
        conn, person_id=person_id, start=window.start, end=window.end
    )
    contributions = (
        {h.instrument: h for h in per_holding.holdings} if per_holding is not None else {}
    )

    holdings = _holdings(conn, person_id, window)
    for entry in holdings:
        found = contributions.get(entry["instrument"])
        # Percentage points of the portfolio's return, which is a different
        # question from `own_return` and is labelled as one on the page.
        entry["contribution"] = _num(found.total) if found else None

    unfunded = cash.compute_flows(conn).unfunded
    unexplained = sum((event.amount for event in unfunded if event.currency == "EUR"), ZERO)

    body: dict[str, Any] = {
        "range": key,
        "window": {
            "from": window.start.isoformat(),
            "to": window.end.isoformat(),
            "label": window.label,
            "days": (window.end - window.start).days,
        },
        "value": {
            "now_eur": _num(closing),
            "then_eur": _num(opening),
            "change_eur": _num(closing - opening),
            # Not a return: contributions move this too, which is why the
            # time-weighted figure sits beside it rather than instead of it.
            "change_pct": _num((closing / opening - 1) if opening > ZERO else None),
        },
        "return": {
            "twr": _num(summary.twr),
            "twr_annualised": _num(summary.twr_annualised),
            "volatility": _num(summary.volatility),
            "max_drawdown": _num(summary.max_drawdown),
            # A return computed over money the tool cannot account for is
            # wrong, not merely uncertain. Said plainly rather than shown.
            "reliable": not unfunded,
            "unreliable_because": (
                None
                if not unfunded
                else (
                    f"{len(unfunded)} purchase(s) worth about EUR {unexplained:,.0f} were "
                    "made with money this tool has no record of, so that money is being "
                    "counted as return."
                )
            ),
            "caveats": summary.caveats,
        },
        "attribution": (
            {
                "asset_prices": _num(split.local),
                "currency": _num(split.currency),
                "income": _num(split.income),
                "trading": _num(split.unexplained),
                "total": _num(split.total),
            }
            if split is not None
            else None
        ),
        "holdings": holdings,
    }

    try:
        progress = goal.progress(conn, person_id=person_id)
        body["goal"] = {
            "target_eur": _num(progress.target_eur),
            "current_eur": _num(progress.current_eur),
            "remaining_eur": _num(progress.remaining_eur),
            "progress_pct": _num(progress.progress_pct),
        }
    except Exception:
        body["goal"] = None

    return body
