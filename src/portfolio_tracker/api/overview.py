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
    """Each holding's performance over the window: price gain, dividends, and total gain.

    Every metric strictly respects the selected window:
    - If window is ALL:
      Price gain is the life-to-date capital gain (unrealized_eur against cost basis).
      Dividends is all dividends ever received.
      Total gain is Price gain + Dividends.
    - If window is a time range (1D, 1W, 1M, 3M, 6M, YTD, 1Y):
      Price gain is the economic capital gain during the window:
      (now_val - then_val) - buys_eur + sells_eur.
      Dividends is dividends received between window.start and window.end.
      Total gain is Price gain + Dividends.
    All percentage returns are strictly additive:
      Total return (%) = Price return (%) + Dividend return (%).
    """
    rows = conn.execute(
        """
        SELECT i.id AS instrument_id, i.import_key, i.name, i.currency,
               p.account_id, p.quantity, p.market_value_eur, p.cost_basis_eur,
               p.unrealized_eur, p.valuation_status,
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
            window.end.isoformat(),
            person_id,
        ),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        inst_id = r["instrument_id"]
        account_id = r["account_id"]
        now_value = Decimal(str(r["market_value_eur"]))
        now_qty = Decimal(str(r["quantity"])) if r["quantity"] is not None else ZERO
        cost_basis = Decimal(str(r["cost_basis_eur"])) if r["cost_basis_eur"] else ZERO
        unrealized = Decimal(str(r["unrealized_eur"])) if r["unrealized_eur"] else ZERO

        # Open lots currently remaining for this holding
        open_lots = conn.execute(
            """
            SELECT id, open_date, qty_remaining, cost_per_unit_eur, cost_total_eur
            FROM lot
            WHERE instrument_id = ? AND account_id = ? AND qty_remaining > 0
            """,
            (inst_id, account_id),
        ).fetchall()

        # Price gain amounting to current holdings
        if window.key == "ALL":
            price_gain = unrealized
            base = cost_basis
        else:
            # Position at start of window
            pos_then = conn.execute(
                """
                SELECT market_value_eur, quantity, date
                FROM position_daily
                WHERE instrument_id = ? AND account_id = ? AND date <= ?
                ORDER BY date DESC LIMIT 1
                """,
                (inst_id, account_id, window.start.isoformat()),
            ).fetchone()

            p_start = (
                Decimal(str(pos_then["market_value_eur"])) / Decimal(str(pos_then["quantity"]))
                if (
                    pos_then
                    and pos_then["quantity"]
                    and Decimal(str(pos_then["quantity"])) > ZERO
                    and pos_then["date"] == window.start.isoformat()
                )
                else None
            )

            base = ZERO
            for lot in open_lots:
                q_rem = Decimal(str(lot["qty_remaining"]))
                lot_open_date = lot["open_date"]
                if lot_open_date <= window.start.isoformat() and p_start is not None:
                    base += q_rem * p_start
                else:
                    # Bought within window
                    cost_unit = (
                        Decimal(str(lot["cost_per_unit_eur"]))
                        if lot["cost_per_unit_eur"]
                        else (now_value / now_qty if now_qty > ZERO else ZERO)
                    )
                    base += q_rem * cost_unit

            price_gain = now_value - base

        # Dividends earned by currently held shares during the window
        if window.key == "ALL":
            div_txns = conn.execute(
                """
                SELECT trade_date, gross_amount_eur
                FROM transaction_ t
                JOIN account atxn ON atxn.id = t.account_id
                WHERE t.instrument_id = ? AND atxn.person_id = ?
                  AND t.type = 'DIVIDEND'
                  AND t.trade_date <= ?
                ORDER BY t.trade_date ASC
                """,
                (inst_id, person_id, window.end.isoformat()),
            ).fetchall()
        else:
            div_txns = conn.execute(
                """
                SELECT trade_date, gross_amount_eur
                FROM transaction_ t
                JOIN account atxn ON atxn.id = t.account_id
                WHERE t.instrument_id = ? AND atxn.person_id = ?
                  AND t.type = 'DIVIDEND'
                  AND t.trade_date > ? AND t.trade_date <= ?
                ORDER BY t.trade_date ASC
                """,
                (inst_id, person_id, window.start.isoformat(), window.end.isoformat()),
            ).fetchall()

        holding_divs = ZERO
        for dt in div_txns:
            if not dt["gross_amount_eur"]:
                continue
            amt = Decimal(str(dt["gross_amount_eur"]))
            pos_at_div = conn.execute(
                """
                SELECT quantity FROM position_daily
                WHERE instrument_id = ? AND account_id = ? AND date <= ?
                ORDER BY date DESC LIMIT 1
                """,
                (inst_id, account_id, dt["trade_date"]),
            ).fetchone()
            if not pos_at_div or Decimal(str(pos_at_div["quantity"])) <= ZERO:
                continue
            total_held_then = Decimal(str(pos_at_div["quantity"]))
            dps = amt / total_held_then
            curr_held_then = sum(
                (
                    Decimal(str(lot["qty_remaining"]))
                    for lot in open_lots
                    if lot["open_date"] <= dt["trade_date"]
                ),
                ZERO,
            )
            holding_divs += curr_held_then * dps

        price_ret = (price_gain / base) if base > ZERO else None
        div_ret = (holding_divs / base) if base > ZERO else None
        total_gain = price_gain + holding_divs
        total_ret = (
            (price_ret + div_ret)
            if (price_ret is not None and div_ret is not None)
            else (price_ret or div_ret)
        )

        out.append(
            {
                "instrument": r["import_key"],
                "name": r["name"],
                "currency": r["currency"],
                "value_eur": _num(now_value),
                "value_change_eur": _num(total_gain),
                "cost_basis_eur": _num(cost_basis),
                "own_return": _num(total_ret),
                "held_from": r["held_from"],
                "unrealized_eur": _num(r["unrealized_eur"]),
                "price_gain_eur": _num(price_gain),
                "price_return": _num(price_ret),
                "dividends_eur": _num(holding_divs),
                "dividend_return": _num(div_ret),
                "total_gain_eur": _num(total_gain),
                "total_return": _num(total_ret),
                "valuation_status": r["valuation_status"],
            }
        )
    return out


def _cumulative_dividends(conn: sqlite3.Connection, person_id: int) -> dict[str, Any]:
    """All dividends received so far across the portfolio, including closed positions."""
    rows = conn.execute(
        """
        SELECT i.id, i.import_key, i.name, i.currency,
               COUNT(t.id) AS payout_count,
               SUM(t.gross_amount_eur) AS total_amount_eur,
               MIN(t.trade_date) AS first_payout,
               MAX(t.trade_date) AS last_payout,
               COALESCE((
                   SELECT p.quantity FROM position_daily p
                   WHERE p.instrument_id = i.id AND p.date = (SELECT MAX(date) FROM position_daily)
               ), 0) AS current_quantity
        FROM transaction_ t
        JOIN instrument i ON i.id = t.instrument_id
        JOIN account a ON a.id = t.account_id
        WHERE a.person_id = ? AND t.type = 'DIVIDEND'
        GROUP BY i.id
        ORDER BY last_payout DESC, total_amount_eur DESC
        """,
        (person_id,),
    ).fetchall()

    items = []
    total_eur = Decimal(0)
    for r in rows:
        amt = Decimal(str(r["total_amount_eur"])) if r["total_amount_eur"] else Decimal(0)
        total_eur += amt
        curr_qty = Decimal(str(r["current_quantity"])) if r["current_quantity"] else Decimal(0)
        items.append(
            {
                "instrument": r["import_key"],
                "name": r["name"],
                "currency": r["currency"],
                "is_open": curr_qty > ZERO,
                "current_quantity": _num(curr_qty),
                "payout_count": r["payout_count"],
                "total_amount_eur": _num(amt),
                "first_payout": r["first_payout"],
                "last_payout": r["last_payout"],
            }
        )

    return {
        "total_dividends_eur": _num(total_eur),
        "items": items,
    }


def _closed_trades(conn: sqlite3.Connection, person_id: int) -> dict[str, Any]:
    """Closed trades (realized disposals) with exact profit/loss from FIFO lot accounting."""
    rows = conn.execute(
        """
        SELECT t.trade_date,
               i.id AS instrument_id, i.import_key, i.name, i.currency,
               SUM(ld.qty) AS qty_sold,
               SUM(ld.proceeds_eur) AS proceeds_eur,
               SUM(ld.cost_basis_eur) AS cost_basis_eur,
               SUM(ld.gross_gain_eur) AS gross_gain_eur,
               MIN(ld.holding_days) AS min_holding_days,
               MAX(ld.holding_days) AS max_holding_days,
               COUNT(ld.id) AS lot_count,
               COALESCE((
                   SELECT p.quantity FROM position_daily p
                   WHERE p.instrument_id = i.id AND p.date = (SELECT MAX(date) FROM position_daily)
               ), 0) AS current_quantity
        FROM lot_disposal ld
        JOIN transaction_ t ON t.id = ld.sell_txn_id
        JOIN instrument i ON i.id = t.instrument_id
        JOIN account a ON a.id = t.account_id
        WHERE a.person_id = ?
        GROUP BY t.trade_date, i.id
        ORDER BY t.trade_date DESC, t.id DESC
        """,
        (person_id,),
    ).fetchall()

    trades = []
    total_realized_eur = Decimal(0)
    for r in rows:
        proceeds = Decimal(str(r["proceeds_eur"])) if r["proceeds_eur"] else Decimal(0)
        cost = Decimal(str(r["cost_basis_eur"])) if r["cost_basis_eur"] else Decimal(0)
        gain = Decimal(str(r["gross_gain_eur"])) if r["gross_gain_eur"] else Decimal(0)
        total_realized_eur += gain
        ret_pct = (gain / cost) if cost > ZERO else None
        curr_qty = Decimal(str(r["current_quantity"])) if r["current_quantity"] else Decimal(0)

        trades.append(
            {
                "trade_date": r["trade_date"],
                "instrument": r["import_key"],
                "name": r["name"],
                "currency": r["currency"],
                "is_closed_position": curr_qty <= ZERO,
                "current_quantity": _num(curr_qty),
                "qty_sold": _num(Decimal(str(r["qty_sold"]))),
                "proceeds_eur": _num(proceeds),
                "cost_basis_eur": _num(cost),
                "realized_gain_eur": _num(gain),
                "realized_return": _num(ret_pct),
                "min_holding_days": r["min_holding_days"],
                "max_holding_days": r["max_holding_days"],
                "lot_count": r["lot_count"],
            }
        )

    return {
        "total_realized_gain_eur": _num(total_realized_eur),
        "trades": trades,
    }


def _multi_window_returns(conn: sqlite3.Connection, person_id: int) -> list[dict[str, Any]]:
    """Compute performance for 1W, 1M, 3M, 6M, and 12M windows."""
    windows_to_calc = [
        ("1W", "1W"),
        ("1M", "1M"),
        ("3M", "3M"),
        ("6M", "6M"),
        ("12M", "1Y"),
    ]
    out: list[dict[str, Any]] = []
    for label, range_key in windows_to_calc:
        try:
            w = resolve(conn, person_id, range_key)
            s = returns.summarise(conn, person_id=person_id, start=w.start, end=w.end)
            r_end = conn.execute(
                "SELECT valuation_basis_eur FROM portfolio_daily WHERE person_id = ? AND date = ?",
                (person_id, w.end.isoformat()),
            ).fetchone()
            r_start = conn.execute(
                "SELECT valuation_basis_eur FROM portfolio_daily WHERE person_id = ? AND date = ?",
                (person_id, w.start.isoformat()),
            ).fetchone()
            v_end = (
                Decimal(str(r_end["valuation_basis_eur"]))
                if r_end and r_end["valuation_basis_eur"] is not None
                else ZERO
            )
            v_start = (
                Decimal(str(r_start["valuation_basis_eur"]))
                if r_start and r_start["valuation_basis_eur"] is not None
                else ZERO
            )
            gain_eur = v_end - v_start - s.external_flows_eur
            out.append(
                {
                    "key": label,
                    "range_key": range_key,
                    "label": label,
                    "from": w.start.isoformat(),
                    "to": w.end.isoformat(),
                    "twr": _num(s.twr),
                    "value_eur": _num(gain_eur),
                }
            )
        except Exception:
            continue
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

    cash_row = conn.execute(
        "SELECT cash_eur FROM portfolio_daily WHERE person_id = ? AND date = ?",
        (person_id, window.end.isoformat()),
    ).fetchone()
    cash_balance = (
        Decimal(str(cash_row["cash_eur"]))
        if cash_row and cash_row["cash_eur"] is not None
        else ZERO
    )

    body: dict[str, Any] = {
        "range": key,
        "cash_eur": _num(cash_balance),
        "top_returns": _multi_window_returns(conn, person_id),
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
        "cumulative_dividends": _cumulative_dividends(conn, person_id),
        "closed_trades": _closed_trades(conn, person_id),
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


@router.get("/api/returns/aggregated")
def aggregated_returns(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
    range_key: str = Query("6M", alias="range"),
) -> dict[str, Any]:
    """Monthly and weekly aggregated returns over a chosen window (strictly >= 6 months).

    Each month and week computes:
    - Time-weighted return (TWR) per unit, neutralising external flows.
    - True EUR value gain: ending valuation basis - starting valuation basis - net external flows.
    """
    key = range_key.upper()
    if key not in ("6M", "1Y", "ALL"):
        key = "6M"

    row = conn.execute(
        "SELECT max(date) AS d, min(date) AS f FROM portfolio_daily WHERE person_id = ?",
        (person_id,),
    ).fetchone()
    if row is None or row["d"] is None:
        raise HTTPException(404, "no portfolio history")

    end_date = date.fromisoformat(str(row["d"]))
    first_date = date.fromisoformat(str(row["f"]))

    if key == "ALL":
        filter_start = first_date
    elif key == "1Y":
        filter_start = max(first_date, end_date - timedelta(days=366))
    else:  # 6M
        filter_start = max(first_date, end_date - timedelta(days=183))

    rows = conn.execute(
        """
        SELECT date, valuation_basis_eur, unit_price, net_external_flow_eur
        FROM portfolio_daily
        WHERE person_id = ?
        ORDER BY date
        """,
        (person_id,),
    ).fetchall()

    if not rows:
        return {
            "range": key,
            "from": filter_start.isoformat(),
            "to": end_date.isoformat(),
            "monthly": [],
            "weekly": [],
        }

    # Group by month (YYYY-MM)
    by_month: dict[str, list[Any]] = {}
    for r in rows:
        by_month.setdefault(str(r["date"])[:7], []).append(r)

    months = sorted(by_month.keys())
    monthly_out: list[dict[str, Any]] = []
    for idx, m in enumerate(months):
        m_rows = by_month[m]
        d_end = date.fromisoformat(str(m_rows[-1]["date"]))
        if d_end < filter_start:
            continue
        prev_row = by_month[months[idx - 1]][-1] if idx > 0 else m_rows[0]
        last_row = m_rows[-1]

        u_start = Decimal(str(prev_row["unit_price"]))
        u_end = Decimal(str(last_row["unit_price"]))
        twr_val = (u_end / u_start - 1) if u_start > ZERO else ZERO

        v_start = Decimal(str(prev_row["valuation_basis_eur"]))
        v_end = Decimal(str(last_row["valuation_basis_eur"]))
        flows = sum((Decimal(str(x["net_external_flow_eur"] or 0)) for x in m_rows), ZERO)
        val_gain = v_end - v_start - flows

        d_first = date.fromisoformat(str(m_rows[0]["date"]))
        monthly_out.append(
            {
                "key": m,
                "label": d_first.strftime("%B %Y"),
                "short_label": d_first.strftime("%b '%y"),
                "start_date": m_rows[0]["date"],
                "end_date": m_rows[-1]["date"],
                "twr": _num(twr_val),
                "value_eur": _num(val_gain),
            }
        )

    # Group by ISO week (YYYY-Www)
    by_week: dict[str, list[Any]] = {}
    for r in rows:
        d = date.fromisoformat(str(r["date"]))
        iso = d.isocalendar()
        k = f"{iso.year}-W{iso.week:02d}"
        by_week.setdefault(k, []).append(r)

    weeks = sorted(by_week.keys())
    weekly_out: list[dict[str, Any]] = []
    for idx, w in enumerate(weeks):
        w_rows = by_week[w]
        d_end = date.fromisoformat(str(w_rows[-1]["date"]))
        if d_end < filter_start:
            continue
        prev_row = by_week[weeks[idx - 1]][-1] if idx > 0 else w_rows[0]
        last_row = w_rows[-1]

        u_start = Decimal(str(prev_row["unit_price"]))
        u_end = Decimal(str(last_row["unit_price"]))
        twr_val = (u_end / u_start - 1) if u_start > ZERO else ZERO

        v_start = Decimal(str(prev_row["valuation_basis_eur"]))
        v_end = Decimal(str(last_row["valuation_basis_eur"]))
        flows = sum((Decimal(str(x["net_external_flow_eur"] or 0)) for x in w_rows), ZERO)
        val_gain = v_end - v_start - flows

        d_first = date.fromisoformat(str(w_rows[0]["date"]))
        iso_week = int(w.split("-W")[1])
        date_range_str = f"{d_first.strftime('%d %b')} - {d_end.strftime('%d %b %Y')}"
        weekly_out.append(
            {
                "key": w,
                "label": f"Week {iso_week} ({date_range_str})",
                "short_label": f"W{iso_week:02d}",
                "start_date": w_rows[0]["date"],
                "end_date": w_rows[-1]["date"],
                "twr": _num(twr_val),
                "value_eur": _num(val_gain),
            }
        )

    return {
        "range": key,
        "from": filter_start.isoformat(),
        "to": end_date.isoformat(),
        "monthly": monthly_out,
        "weekly": weekly_out,
    }
