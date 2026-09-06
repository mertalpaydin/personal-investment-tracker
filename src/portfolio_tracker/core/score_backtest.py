"""Does a high score go with a better return later? Measured, not assumed.

The Value and Quality scores were written by a formula, and the formula was
written by an AI agent. Nobody has checked it against outcomes. This module
reads the dated snapshots from `core/score_snapshots.py`, waits until the
price history has moved far enough past each one, and asks three questions:

* **Rank correlation** (information coefficient, IC): on one snapshot day,
  did the stocks with higher scores earn higher forward returns? Spearman
  correlation between score and return, one number per day, then averaged
  over days. +1 is perfect foresight, 0 is noise, negative is backwards.
* **Bucket returns**: split each day's stocks into top / middle / bottom by
  score. Did the top bucket beat the bottom bucket?
* **Hit rate**: how often did an above-median score beat the benchmark?

Returns are measured **relative** to a benchmark so that a rising market does
not flatter every score. The benchmark is an instrument in this database
(a world ETF, say), stored under the setting `score_backtest_benchmark`;
without one, the median forward return of the scored stocks on that day is
used instead.

Everything here is stdlib: numpy and pandas are optional extras this project
does not load on the main path.

Honesty notes, also surfaced as ``caveats`` in the report:

* A personal watchlist is small. With 15 to 40 stocks the per-day IC is noisy;
  days with fewer than ``MIN_CROSS_SECTION`` scored stocks are skipped.
* Daily snapshots overlap: a 3-month window taken every day is the same bet
  sixty times over. The default samples one snapshot day per month.
* Returns are in each stock's own currency, so a EUR and a USD stock are
  compared without FX. The relative measure absorbs most of that, not all.
* This is a diagnostic. It cannot prove a formula works; it can show one
  that does not.
"""

from __future__ import annotations

import sqlite3
import statistics
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from portfolio_tracker.core import settings
from portfolio_tracker.core.score_snapshots import load_snapshots, snapshot_summary
from portfolio_tracker.core.stock_indicators import QUALITY_COMPONENTS, VALUE_COMPONENTS

#: Calendar days per horizon, the same convention as `performance.WINDOWS`.
HORIZONS: dict[str, int] = {"1m": 30, "3m": 91, "6m": 182, "12m": 365}

#: The end price must be at most this many days before the horizon date.
MAX_PRICE_GAP_DAYS = 7

#: A snapshot day with fewer scored stocks than this is not a cross-section.
MIN_CROSS_SECTION = 8

#: Quintiles need this many stocks on a day; below it, terciles.
QUINTILE_MIN = 15

#: Fewer snapshot days than this and the t-statistic is not worth printing.
MIN_DATES_FOR_TSTAT = 6

#: Snapshots whose fundamentals are older than this are ignored: the score
#: was computed on stale inputs, so it is not the formula being tested.
DEFAULT_MAX_FUNDAMENTALS_AGE_DAYS = 120

BENCHMARK_SETTING_KEY = "score_backtest_benchmark"

SCORE_KINDS = ("value", "quality")
SAMPLES = ("all", "weekly", "monthly")

_COMPONENT_COLUMNS = {
    "value": tuple(f"v_{c}" for c in VALUE_COMPONENTS),
    "quality": tuple(f"q_{c}" for c in QUALITY_COMPONENTS),
}


# ---------------------------------------------------------------------------
# Small statistics, stdlib only
# ---------------------------------------------------------------------------


def _ranks(values: list[float]) -> list[float]:
    """Average ranks (1-based), ties share the mean of their positions."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        mean_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = mean_rank
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return float(sxy / (sxx * syy) ** 0.5)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation, -1..1; None with fewer than three pairs or a constant side."""
    if len(xs) != len(ys):
        raise ValueError("spearman needs two lists of the same length")
    if len(xs) < 3:
        return None
    return _pearson(_ranks(xs), _ranks(ys))


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _tstat(values: list[float]) -> float | None:
    """Mean over standard error, the usual test of "is the average IC non-zero"."""
    if len(values) < MIN_DATES_FOR_TSTAT:
        return None
    sd = statistics.stdev(values)
    if sd == 0:
        return None
    return float((sum(values) / len(values)) / (sd / len(values) ** 0.5))


# ---------------------------------------------------------------------------
# Prices and forward returns
# ---------------------------------------------------------------------------


@dataclass
class Series:
    """One instrument's closes, sorted by date, ready for bisect."""

    dates: list[date]
    close: list[Decimal]
    tr_close: list[Decimal | None]

    def index_on_or_before(self, on: date) -> int | None:
        i = bisect_right(self.dates, on) - 1
        return i if i >= 0 else None


def _price_series(conn: sqlite3.Connection, instrument_ids: set[int]) -> dict[int, Series]:
    out: dict[int, Series] = {}
    if not instrument_ids:
        return out
    marks = ", ".join("?" for _ in instrument_ids)
    rows = conn.execute(
        f"SELECT instrument_id, date, close, total_return_close FROM price "
        f"WHERE instrument_id IN ({marks}) ORDER BY instrument_id, date",
        tuple(instrument_ids),
    ).fetchall()
    for r in rows:
        s = out.setdefault(int(r["instrument_id"]), Series([], [], []))
        s.dates.append(date.fromisoformat(str(r["date"])))
        s.close.append(Decimal(str(r["close"])))
        tr = r["total_return_close"]
        s.tr_close.append(Decimal(str(tr)) if tr is not None else None)
    return out


def forward_return(
    series: Series | None, start: date, horizon_days: int, *, newest: date
) -> Decimal | None:
    """Return from the last close on/before ``start`` to the last close on/before
    ``start + horizon``.

    None when the window has not finished yet (``newest`` is the latest price
    date in the whole database), when either endpoint is missing, or when the
    end price is more than ``MAX_PRICE_GAP_DAYS`` stale. Dividend-adjusted
    closes are used only when both endpoints have them; otherwise both use the
    plain close, never one of each.
    """
    if series is None:
        return None
    end = start + timedelta(days=horizon_days)
    if end > newest:
        return None
    i = series.index_on_or_before(start)
    j = series.index_on_or_before(end)
    if i is None or j is None or j <= i:
        return None
    if (end - series.dates[j]).days > MAX_PRICE_GAP_DAYS:
        return None
    a_tr, b_tr = series.tr_close[i], series.tr_close[j]
    if a_tr is not None and b_tr is not None and a_tr > 0:
        return b_tr / a_tr - 1
    a, b = series.close[i], series.close[j]
    return b / a - 1 if a > 0 else None


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    instrument_id: int
    symbol: str
    calc_date: date
    formula_version: str
    badge: str
    score: int
    components: dict[str, int]
    currency: str | None
    #: absolute forward return per horizon (None = not mature / no price)
    fwd: dict[str, Decimal | None] = field(default_factory=dict)
    #: forward return minus the benchmark's over the same window
    rel: dict[str, Decimal | None] = field(default_factory=dict)


def _period_key(on: date, sample: str) -> tuple[int, int]:
    if sample == "weekly":
        iso = on.isocalendar()
        return iso.year, iso.week
    return on.year, on.month


def load_observations(
    conn: sqlite3.Connection,
    *,
    score: str = "value",
    formula_version: str | None = None,
    max_fundamentals_age_days: int = DEFAULT_MAX_FUNDAMENTALS_AGE_DAYS,
    sample: str = "monthly",
) -> tuple[list[Observation], dict[str, int]]:
    """Snapshot rows as observations, with the reasons rows were dropped."""
    if score not in SCORE_KINDS:
        raise ValueError(f"score must be one of {SCORE_KINDS}")
    if sample not in SAMPLES:
        raise ValueError(f"sample must be one of {SAMPLES}")
    score_col = f"{score}_score"
    comp_cols = _COMPONENT_COLUMNS[score]
    dropped = {"no_score": 0, "stale_fundamentals": 0, "sampled_out": 0}
    kept_periods: dict[tuple[str, tuple[int, int]], date] = {}
    out: list[Observation] = []
    for r in load_snapshots(conn, formula_version=formula_version):
        if r[score_col] is None:
            dropped["no_score"] += 1
            continue
        on = date.fromisoformat(str(r["calc_date"]))
        fetched = r["fundamentals_fetched_at"]
        if fetched:
            fetched_on = date.fromisoformat(str(fetched)[:10])
            if (on - fetched_on).days > max_fundamentals_age_days:
                dropped["stale_fundamentals"] += 1
                continue
        if sample != "all":
            key = (str(r["formula_version"]), _period_key(on, sample))
            first = kept_periods.setdefault(key, on)
            if first != on:
                dropped["sampled_out"] += 1
                continue
        out.append(
            Observation(
                instrument_id=int(r["instrument_id"]),
                symbol=str(r["symbol"]),
                calc_date=on,
                formula_version=str(r["formula_version"]),
                badge=str(r["value_badge"]),
                score=int(r[score_col]),
                components={c: int(r[c]) for c in comp_cols if r[c] is not None},
                currency=str(r["price_currency"] or r["instrument_currency"] or ""),
            )
        )
    return out, dropped


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class Bucket:
    bucket: str
    n: int
    mean_rel: float | None
    mean_abs: float | None


@dataclass
class HorizonStats:
    horizon: str
    n_obs: int
    n_dates: int
    mean_ic: float | None
    ic_std: float | None
    ic_tstat: float | None
    ic_positive_share: float | None
    hit_rate: float | None
    top_bucket_hit_rate: float | None
    buckets: list[Bucket]
    spread_top_minus_bottom: float | None
    component_ic: dict[str, float | None]
    badge_returns: dict[str, dict[str, float | int | None]]
    caveats: list[str]


@dataclass
class BacktestReport:
    score: str
    sample: str
    benchmark: str | None
    benchmark_mode: str
    coverage: dict[str, Any]
    versions: dict[str, list[HorizonStats]]
    caveats: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resolve_benchmark(
    conn: sqlite3.Connection, benchmark: str | None
) -> tuple[str | None, int | None, str | None]:
    """(import_key, instrument_id, currency) of the benchmark, or Nones."""
    key = benchmark or settings.get(conn, BENCHMARK_SETTING_KEY)
    if not key:
        return None, None, None
    row = conn.execute(
        "SELECT id, currency FROM instrument WHERE import_key = ?", (key,)
    ).fetchone()
    if row is None:
        return key, None, None
    return key, int(row["id"]), str(row["currency"])


def _bucket_labels(n: int) -> tuple[list[str], int]:
    if n >= QUINTILE_MIN:
        return ["Q5 (top)", "Q4", "Q3", "Q2", "Q1 (bottom)"], 5
    return ["Top third", "Middle", "Bottom third"], 3


def _assign_buckets(scores: list[float], k: int) -> list[int]:
    """Bucket index per item, 0 = highest score, k-1 = lowest; ties by rank."""
    ranks = _ranks(scores)
    n = len(scores)
    out = []
    for r in ranks:
        # rank 1 = lowest; highest scores go to bucket 0
        position = (n - r) / n  # 0 for the top, ~1 for the bottom
        out.append(min(k - 1, int(position * k)))
    return out


def _horizon_stats(
    obs: list[Observation], horizon: str, comp_cols: tuple[str, ...], score_kind: str
) -> HorizonStats:
    caveats: list[str] = []
    by_date: dict[date, list[Observation]] = {}
    for o in obs:
        if o.rel.get(horizon) is not None:
            by_date.setdefault(o.calc_date, []).append(o)

    ics: list[float] = []
    comp_ics: dict[str, list[float]] = {c: [] for c in comp_cols}
    hits = 0
    hit_total = 0
    top_hits = 0
    top_total = 0
    bucket_rel: dict[int, list[float]] = {}
    bucket_abs: dict[int, list[float]] = {}
    skipped_small = sum(1 for rows in by_date.values() if len(rows) < MIN_CROSS_SECTION)
    kept_sizes = [len(rows) for rows in by_date.values() if len(rows) >= MIN_CROSS_SECTION]
    # One bucket count for the whole horizon, set by the thinnest day, so
    # "top bucket" means the same thing on every day that is averaged.
    labels, k = _bucket_labels(min(kept_sizes)) if kept_sizes else ([], 0)
    n_obs = 0

    for _on, rows in sorted(by_date.items()):
        if len(rows) < MIN_CROSS_SECTION:
            continue
        n_obs += len(rows)
        scores = [float(o.score) for o in rows]
        rels = [float(o.rel[horizon]) for o in rows]  # type: ignore[arg-type]
        ic = spearman(scores, rels)
        if ic is not None:
            ics.append(ic)
        for c in comp_cols:
            vals = [float(o.components.get(c, 0)) for o in rows]
            cic = spearman(vals, rels)
            if cic is not None:
                comp_ics[c].append(cic)
        median_score = statistics.median(scores)
        for s, r in zip(scores, rels, strict=True):
            if s > median_score:
                hit_total += 1
                hits += int(r > 0)
        for idx, o in zip(_assign_buckets(scores, k), rows, strict=True):
            bucket_rel.setdefault(idx, []).append(float(o.rel[horizon]))  # type: ignore[arg-type]
            bucket_abs.setdefault(idx, []).append(float(o.fwd[horizon]))  # type: ignore[arg-type]
            if idx == 0:
                top_total += 1
                top_hits += int(o.rel[horizon] > 0)  # type: ignore[operator]

    n_dates = len(ics)
    if skipped_small:
        caveats.append(
            f"{skipped_small} snapshot day(s) skipped: fewer than {MIN_CROSS_SECTION} scored stocks"
        )
    if k == 3:
        caveats.append(
            f"buckets are thirds, not quintiles: fewer than {QUINTILE_MIN} stocks on a day"
        )
    if n_dates and n_dates < MIN_DATES_FOR_TSTAT:
        caveats.append(f"only {n_dates} snapshot day(s); the average IC is not yet reliable")

    buckets: list[Bucket] = []
    if k:
        for idx, label in enumerate(labels):
            rel_vals = bucket_rel.get(idx, [])
            buckets.append(
                Bucket(
                    bucket=label,
                    n=len(rel_vals),
                    mean_rel=_mean(rel_vals),
                    mean_abs=_mean(bucket_abs.get(idx, [])),
                )
            )
    spread = None
    if buckets and buckets[0].mean_rel is not None and buckets[-1].mean_rel is not None:
        spread = buckets[0].mean_rel - buckets[-1].mean_rel

    badge_returns: dict[str, dict[str, float | int | None]] = {}
    if score_kind == "value":
        by_badge: dict[str, list[float]] = {}
        for rows in by_date.values():
            if len(rows) < MIN_CROSS_SECTION:
                continue
            for o in rows:
                by_badge.setdefault(o.badge, []).append(float(o.rel[horizon]))  # type: ignore[arg-type]
        badge_returns = {
            b: {"n": len(v), "mean_rel": _mean(v)} for b, v in sorted(by_badge.items())
        }

    return HorizonStats(
        horizon=horizon,
        n_obs=n_obs,
        n_dates=n_dates,
        mean_ic=_mean(ics),
        ic_std=statistics.stdev(ics) if len(ics) >= 2 else None,
        ic_tstat=_tstat(ics),
        ic_positive_share=(sum(1 for x in ics if x > 0) / len(ics)) if ics else None,
        hit_rate=(hits / hit_total) if hit_total else None,
        top_bucket_hit_rate=(top_hits / top_total) if top_total else None,
        buckets=buckets,
        spread_top_minus_bottom=spread,
        component_ic={c: _mean(v) for c, v in comp_ics.items()},
        badge_returns=badge_returns,
        caveats=caveats,
    )


def evaluate(
    conn: sqlite3.Connection,
    *,
    score: str = "value",
    horizons: list[str] | None = None,
    formula_version: str | None = None,
    sample: str = "monthly",
    benchmark: str | None = None,
    max_fundamentals_age_days: int = DEFAULT_MAX_FUNDAMENTALS_AGE_DAYS,
) -> BacktestReport:
    """The whole report: coverage, then per formula version, per horizon."""
    horizons = horizons or list(HORIZONS)
    unknown = [h for h in horizons if h not in HORIZONS]
    if unknown:
        raise ValueError(f"unknown horizon(s) {unknown}; choose from {list(HORIZONS)}")

    obs, dropped = load_observations(
        conn,
        score=score,
        formula_version=formula_version,
        max_fundamentals_age_days=max_fundamentals_age_days,
        sample=sample,
    )
    caveats: list[str] = []
    bench_key, bench_id, bench_ccy = _resolve_benchmark(conn, benchmark)
    benchmark_mode = "instrument" if bench_id is not None else "universe_median"
    if bench_key and bench_id is None:
        caveats.append(f"benchmark {bench_key!r} is not in the database; using the universe median")
    elif bench_id is None:
        caveats.append("no benchmark set; returns are relative to the median of the scored stocks")

    ids = {o.instrument_id for o in obs}
    if bench_id is not None:
        ids.add(bench_id)
    prices = _price_series(conn, ids)
    newest_row = conn.execute("SELECT max(date) AS d FROM price").fetchone()
    newest = date.fromisoformat(str(newest_row["d"])) if newest_row and newest_row["d"] else None

    dropped_no_price = 0
    if newest is not None:
        bench_series = prices.get(bench_id) if bench_id is not None else None
        for o in obs:
            s = prices.get(o.instrument_id)
            if s is None:
                dropped_no_price += 1
            for h in horizons:
                o.fwd[h] = forward_return(s, o.calc_date, HORIZONS[h], newest=newest)
        # relative returns
        fallback_used = False
        for h in horizons:
            by_date: dict[date, list[Observation]] = {}
            for o in obs:
                if o.fwd.get(h) is not None:
                    by_date.setdefault(o.calc_date, []).append(o)
            for on, rows in by_date.items():
                bench_ret = (
                    forward_return(bench_series, on, HORIZONS[h], newest=newest)
                    if bench_id is not None
                    else None
                )
                if bench_ret is None:
                    if bench_id is not None:
                        fallback_used = True
                    known = [f for f in (o.fwd[h] for o in rows) if f is not None]
                    bench_ret = statistics.median(known)
                for o in rows:
                    o.rel[h] = o.fwd[h] - bench_ret  # type: ignore[operator]
        if fallback_used:
            caveats.append(
                "benchmark prices were missing for some windows; the universe median was used there"
            )
        if bench_ccy and any(o.currency and o.currency != bench_ccy for o in obs):
            caveats.append(
                f"some stocks are not quoted in {bench_ccy}; returns are compared without FX"
            )
    else:
        caveats.append("no prices on file; nothing can be measured yet")

    comp_cols = _COMPONENT_COLUMNS[score]
    versions: dict[str, list[HorizonStats]] = {}
    for version in sorted({o.formula_version for o in obs}):
        v_obs = [o for o in obs if o.formula_version == version]
        versions[version] = [_horizon_stats(v_obs, h, comp_cols, score) for h in horizons]

    summary = snapshot_summary(conn)
    mature = {h: sum(1 for o in obs if o.fwd.get(h) is not None) for h in horizons}
    first = summary["first_date"]
    first_readings = {}
    if first:
        first_on = date.fromisoformat(str(first))
        first_readings = {
            h: (first_on + timedelta(days=d)).isoformat() for h, d in HORIZONS.items()
        }
    ages = []
    for r in load_snapshots(conn, formula_version=formula_version):
        if r["fundamentals_fetched_at"]:
            on = date.fromisoformat(str(r["calc_date"]))
            fetched_on = date.fromisoformat(str(r["fundamentals_fetched_at"])[:10])
            ages.append((on - fetched_on).days)
    coverage = {
        **summary,
        "observations": len(obs),
        "dropped": {**dropped, "no_price": dropped_no_price},
        "mature_by_horizon": mature,
        "first_reading_by_horizon": first_readings,
        "median_fundamentals_age_days": int(statistics.median(ages)) if ages else None,
        "newest_price_date": newest.isoformat() if newest else None,
    }
    caveats.append("diagnostic, not proof: a small watchlist gives noisy readings")
    return BacktestReport(
        score=score,
        sample=sample,
        benchmark=bench_key,
        benchmark_mode=benchmark_mode,
        coverage=coverage,
        versions=versions,
        caveats=caveats,
    )
