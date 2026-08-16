"""The savings target, and honest arithmetic about reaching it.

Progress is measured against the **headline total** -- holdings plus deposit
balances -- for the same reason the headline excludes uninvested cash: money
whose fate is unknown should not move a progress bar.

The projection is the part that needs care. A naive "at your current rate you
arrive in March 2029" reads as a forecast, and people plan around forecasts. The
rate here is measured contribution history and nothing else: no assumed return,
no compounding, no market view. That makes the date deliberately pessimistic and,
more importantly, falsifiable -- it is an extrapolation of what the owner has
actually done, and it is labelled as one.

When there is too little history to measure a rate, the projection is ``None``.
A date computed from three weeks of data is not a worse estimate than one
computed from three years; it is a different kind of object, and returning it
in the same field would let the caller treat them alike.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from portfolio_tracker.money import ZERO, quantize_money

GOAL_KEY = "goal_target_eur"

#: Below this, a measured monthly rate is noise. Six months is the point at
#: which a single unusual month stops dominating the average.
MIN_HISTORY_DAYS = 180

#: A projection further out than this says nothing useful; the inputs cannot
#: support it and the number would only invite belief.
MAX_PROJECTION_YEARS = 50


@dataclass(frozen=True)
class GoalProgress:
    target_eur: Decimal
    current_eur: Decimal
    remaining_eur: Decimal
    progress_pct: Decimal
    as_of: date
    monthly_contribution_eur: Decimal | None
    months_at_current_rate: int | None
    projected_date: date | None
    basis: str
    caveats: list[str]


def get_target(conn: sqlite3.Connection) -> Decimal:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (GOAL_KEY,)).fetchone()
    if row is None:
        raise LookupError("no goal target set")
    try:
        return Decimal(str(row["value"]))
    except InvalidOperation as exc:  # pragma: no cover - only if the row is hand-edited
        raise ValueError(f"goal target is not a number: {row['value']!r}") from exc


def set_target(conn: sqlite3.Connection, target: Decimal) -> Decimal:
    """Store a new target. Rejects zero and negatives rather than storing them.

    A target of zero would make progress infinite and a negative one would make
    it meaningless; both are typos, and a typo that persists is worse than one
    that is refused.
    """
    if target <= ZERO:
        raise ValueError("a goal target must be positive")
    conn.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (GOAL_KEY, str(quantize_money(target)), datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    return quantize_money(target)


def _months_between(start: date, end: date) -> Decimal:
    return Decimal((end - start).days) / Decimal("30.436875")  # mean Gregorian month


def progress(conn: sqlite3.Connection, *, person_id: int = 1) -> GoalProgress:
    """Where the portfolio stands against the target, and what that rests on."""
    target = get_target(conn)
    row = conn.execute(
        "SELECT date, total_value_eur FROM portfolio_daily WHERE person_id = ? "
        "ORDER BY date DESC LIMIT 1",
        (person_id,),
    ).fetchone()
    if row is None:
        raise LookupError("no portfolio history; run `pt rebuild` first")

    as_of = date.fromisoformat(row["date"])
    current = Decimal(str(row["total_value_eur"]))
    remaining = quantize_money(max(ZERO, target - current))
    caveats: list[str] = [
        "Progress is measured against holdings plus deposits. Uninvested cash is "
        "excluded, because whether it is still held is not knowable from the source data."
    ]

    first = conn.execute(
        "SELECT min(date) AS lo FROM portfolio_daily WHERE person_id = ?", (person_id,)
    ).fetchone()["lo"]
    start = date.fromisoformat(first)
    history_days = (as_of - start).days

    monthly: Decimal | None = None
    months: int | None = None
    projected: date | None = None

    if history_days >= MIN_HISTORY_DAYS:
        contributed = ZERO
        for flow in conn.execute(
            "SELECT net_external_flow_eur FROM portfolio_daily WHERE person_id = ?", (person_id,)
        ):
            contributed += Decimal(str(flow["net_external_flow_eur"]))
        elapsed = _months_between(start, as_of)
        if elapsed > ZERO and contributed > ZERO:
            monthly = quantize_money(contributed / elapsed)
            caveats.append(
                f"The monthly rate is the average of {history_days} days of actual "
                "contributions -- several of which were inferred, not recorded."
            )
            if remaining > ZERO and monthly > ZERO:
                raw = remaining / monthly
                if raw <= Decimal(MAX_PROJECTION_YEARS * 12):
                    months = int(raw.to_integral_value(rounding="ROUND_CEILING"))
                    projected = add_months(as_of, months)
                    caveats.append(
                        "The projected date assumes contributions continue at that rate "
                        "and counts no investment return at all. It is an extrapolation "
                        "of the past, not a forecast."
                    )
                else:
                    caveats.append(
                        "At the measured contribution rate the target is more than "
                        f"{MAX_PROJECTION_YEARS} years away, so no date is given."
                    )
    else:
        caveats.append(
            f"Only {history_days} days of history: too few to measure a contribution "
            f"rate, so no projection is offered (needs {MIN_HISTORY_DAYS})."
        )

    return GoalProgress(
        target_eur=quantize_money(target),
        current_eur=quantize_money(current),
        remaining_eur=remaining,
        progress_pct=(current / target) if target > ZERO else ZERO,
        as_of=as_of,
        monthly_contribution_eur=monthly,
        months_at_current_rate=months,
        projected_date=projected,
        basis="holdings plus deposit balances",
        caveats=caveats,
    )


def add_months(start: date, months: int) -> date:
    """Calendar-correct month addition, clamping to the end of a short month."""
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(
        start.day,
        [31, 29 if _leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1],
    )
    return date(year, month, day)


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
