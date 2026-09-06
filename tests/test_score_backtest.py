"""The score backtest: forward returns, rank correlation, buckets, hit rate.

A synthetic universe is built so that the answer is known: score and later
return are tied together by construction, so the IC must come out high, the
top bucket must beat the bottom, and a benchmark must shift every relative
return by the same amount without changing the ranking.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest

from portfolio_tracker.core import score_backtest as bt
from portfolio_tracker.core import settings
from portfolio_tracker.core.stock_indicators import SCORE_FORMULA_VERSION
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    yield conn
    conn.close()


# ------------------------------------------------------------- statistics


def test_spearman_on_known_vectors() -> None:
    assert bt.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert bt.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    # Ties share a rank: monotone with ties is still perfectly correlated.
    assert bt.spearman([1, 1, 2, 3], [5, 5, 6, 7]) == pytest.approx(1.0)
    assert bt.spearman([1, 2], [1, 2]) is None
    assert bt.spearman([3, 3, 3], [1, 2, 3]) is None
    with pytest.raises(ValueError):
        bt.spearman([1, 2, 3], [1, 2])


def test_ranks_average_ties() -> None:
    assert bt._ranks([10.0, 20.0, 20.0, 30.0]) == [1.0, 2.5, 2.5, 4.0]


# --------------------------------------------------------- forward returns


def _series(start: date, closes: list[str], tr: list[str | None] | None = None) -> bt.Series:
    dates = [start + timedelta(days=i) for i in range(len(closes))]
    return bt.Series(
        dates=dates,
        close=[Decimal(c) for c in closes],
        tr_close=[Decimal(t) if t is not None else None for t in (tr or [None] * len(closes))],
    )


def test_forward_return_waits_for_the_window_to_finish() -> None:
    s = _series(date(2026, 1, 1), ["100"] * 40)
    assert bt.forward_return(s, date(2026, 1, 1), 30, newest=date(2026, 1, 20)) is None
    assert bt.forward_return(s, date(2026, 1, 1), 30, newest=date(2026, 2, 9)) is not None


def test_forward_return_uses_the_last_close_on_or_before_each_end() -> None:
    s = _series(date(2026, 1, 1), ["100", "110", "120", "130"])
    # start on a day with no price: falls back to the day before
    s.dates[1] = date(2026, 1, 3)  # make Jan 2 missing
    s.dates[2] = date(2026, 1, 5)
    s.dates[3] = date(2026, 1, 6)
    r = bt.forward_return(s, date(2026, 1, 2), 4, newest=date(2026, 3, 1))  # Jan 1 -> Jan 6
    assert r == Decimal("0.3")


def test_forward_return_rejects_a_stale_end_price() -> None:
    s = _series(date(2026, 1, 1), ["100", "120"])  # prices stop on Jan 2
    assert bt.forward_return(s, date(2026, 1, 1), 30, newest=date(2026, 6, 1)) is None


def test_forward_return_prefers_total_return_only_when_both_ends_have_it() -> None:
    both = _series(date(2026, 1, 1), ["100", "100"], ["100", "110"])
    both.dates[1] = date(2026, 1, 31)
    assert bt.forward_return(both, date(2026, 1, 1), 30, newest=date(2026, 3, 1)) == Decimal("0.1")
    one = _series(date(2026, 1, 1), ["100", "105"], ["100", None])
    one.dates[1] = date(2026, 1, 31)
    assert bt.forward_return(one, date(2026, 1, 1), 30, newest=date(2026, 3, 1)) == Decimal("0.05")


def test_forward_return_without_a_series_is_none() -> None:
    assert bt.forward_return(None, date(2026, 1, 1), 30, newest=date(2026, 6, 1)) is None


# ------------------------------------------------------------ end to end


N_STOCKS = 10
SNAPSHOT_DAYS = [date(2026, 1, 5), date(2026, 2, 5), date(2026, 3, 5)]


def _seed_universe(conn: sqlite3.Connection, *, benchmark: bool = False) -> None:
    """Ten stocks; stock k has score 10k and drifts k% a month, so rank == outcome."""
    for k in range(1, N_STOCKS + 1):
        conn.execute(
            "INSERT INTO instrument (id, import_key, symbol, name, asset_class, currency, "
            "trading_calendar, price_source) VALUES (?, ?, ?, ?, 'STOCK', 'EUR', 'XETRA', 'yfinance')",
            (k, f"S{k}", f"S{k}", f"Stock {k}"),
        )
    if benchmark:
        conn.execute(
            "INSERT INTO instrument (id, import_key, symbol, name, asset_class, currency, "
            "trading_calendar, price_source) VALUES (99, 'BENCH', 'BENCH', 'World ETF', 'ETF', "
            "'EUR', 'XETRA', 'yfinance')"
        )
    first = date(2026, 1, 1)
    for day in range(0, 460):  # past the 12m horizon of the last snapshot
        on = first + timedelta(days=day)
        for k in range(1, N_STOCKS + 1):
            price = Decimal(100) * (Decimal(1) + Decimal(k) / Decimal(100)) ** (
                Decimal(day) / Decimal(30)
            )
            conn.execute(
                "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
                "VALUES (?, ?, ?, 'EUR', 'yfinance', '2026-01-01T00:00:00Z')",
                (k, on.isoformat(), str(round(price, 4))),
            )
        if benchmark:
            bench = Decimal(100) * (Decimal("1.05")) ** (Decimal(day) / Decimal(30))
            conn.execute(
                "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
                "VALUES (99, ?, ?, 'EUR', 'yfinance', '2026-01-01T00:00:00Z')",
                (on.isoformat(), str(round(bench, 4))),
            )
    for on in SNAPSHOT_DAYS:
        for k in range(1, N_STOCKS + 1):
            conn.execute(
                "INSERT INTO stock_score_snapshot (instrument_id, calc_date, formula_version, "
                "source, fundamentals_fetched_at, value_score, value_badge, value_raw_total, "
                "v_pe, v_fcf_yield, quality_score, q_growth, inputs_json, created_at) "
                "VALUES (?, ?, ?, 'DAILY', ?, ?, ?, ?, ?, ?, ?, ?, '{}', 'now')",
                (
                    k,
                    on.isoformat(),
                    SCORE_FORMULA_VERSION,
                    on.isoformat() + "T00:00:00Z",
                    10 * k,
                    "BARGAIN" if k > 6 else "FAIR",
                    10 * k,
                    k,  # v_pe tracks the outcome
                    N_STOCKS - k,  # v_fcf_yield is the opposite
                    100 - 10 * k,  # quality score is backwards on purpose
                    5,
                ),
            )
    conn.commit()


def test_a_score_that_is_the_outcome_gets_a_perfect_ic(db: sqlite3.Connection) -> None:
    _seed_universe(db)
    report = bt.evaluate(db, score="value", sample="all")
    assert report.benchmark_mode == "universe_median"
    assert report.coverage["snapshots"] == N_STOCKS * len(SNAPSHOT_DAYS)
    stats = {s.horizon: s for s in report.versions[SCORE_FORMULA_VERSION]}
    for horizon in ("1m", "3m", "6m", "12m"):
        st = stats[horizon]
        assert st.n_dates == len(SNAPSHOT_DAYS), horizon
        assert st.mean_ic == pytest.approx(1.0), horizon
        assert st.ic_positive_share == 1.0
        assert st.hit_rate == 1.0  # above-median scores all beat the median
        assert st.buckets[0].bucket == "Top third"  # 10 stocks: terciles
        assert st.spread_top_minus_bottom is not None and st.spread_top_minus_bottom > 0
        assert st.component_ic["v_pe"] == pytest.approx(1.0)
        assert st.component_ic["v_fcf_yield"] == pytest.approx(-1.0)
        assert st.badge_returns["BARGAIN"]["mean_rel"] > st.badge_returns["FAIR"]["mean_rel"]  # type: ignore[operator]
    assert any("thirds" in c for c in stats["1m"].caveats)
    assert any("only 3 snapshot day" in c for c in stats["1m"].caveats)


def test_a_backwards_score_gets_a_negative_ic(db: sqlite3.Connection) -> None:
    _seed_universe(db)
    report = bt.evaluate(db, score="quality", sample="all", horizons=["1m"])
    st = report.versions[SCORE_FORMULA_VERSION][0]
    assert st.mean_ic == pytest.approx(-1.0)
    assert st.hit_rate == 0.0
    assert st.badge_returns == {}  # badges belong to the value score only


def test_a_benchmark_instrument_shifts_returns_but_not_the_ranking(db: sqlite3.Connection) -> None:
    _seed_universe(db, benchmark=True)
    settings.set_value(db, bt.BENCHMARK_SETTING_KEY, "BENCH")
    report = bt.evaluate(db, score="value", sample="all", horizons=["1m"])
    assert report.benchmark == "BENCH"
    assert report.benchmark_mode == "instrument"
    st = report.versions[SCORE_FORMULA_VERSION][0]
    assert st.mean_ic == pytest.approx(1.0)
    # The benchmark drifts 5% a month: stocks 1..4 lose to it, 6..10 beat it.
    assert st.hit_rate == 1.0
    assert 0 < st.top_bucket_hit_rate <= 1.0  # type: ignore[operator]
    bottom = st.buckets[-1]
    assert bottom.mean_rel is not None and bottom.mean_rel < 0

    # An explicit --benchmark that does not exist falls back, and says so.
    other = bt.evaluate(db, score="value", sample="all", horizons=["1m"], benchmark="NOPE")
    assert other.benchmark_mode == "universe_median"
    assert any("not in the database" in c for c in other.caveats)


def test_monthly_sampling_keeps_the_first_day_of_each_month(db: sqlite3.Connection) -> None:
    _seed_universe(db)
    # add a second snapshot day in January that monthly sampling must drop
    for k in range(1, N_STOCKS + 1):
        db.execute(
            "INSERT INTO stock_score_snapshot (instrument_id, calc_date, formula_version, source, "
            "value_score, value_badge, inputs_json, created_at) "
            "VALUES (?, '2026-01-20', ?, 'DAILY', ?, 'FAIR', '{}', 'now')",
            (k, SCORE_FORMULA_VERSION, 10 * k),
        )
    db.commit()
    obs, dropped = bt.load_observations(db, sample="monthly")
    assert sorted({o.calc_date for o in obs}) == SNAPSHOT_DAYS
    assert dropped["sampled_out"] == N_STOCKS
    everything, _ = bt.load_observations(db, sample="all")
    assert len(everything) == N_STOCKS * (len(SNAPSHOT_DAYS) + 1)


def test_thin_days_are_skipped_and_counted(db: sqlite3.Connection) -> None:
    _seed_universe(db)
    db.execute(
        "DELETE FROM stock_score_snapshot WHERE calc_date = '2026-03-05' AND instrument_id > 3"
    )
    db.commit()
    report = bt.evaluate(db, score="value", sample="all", horizons=["1m"])
    st = report.versions[SCORE_FORMULA_VERSION][0]
    assert st.n_dates == 2
    assert any("skipped" in c for c in st.caveats)


def test_stale_fundamentals_are_dropped(db: sqlite3.Connection) -> None:
    _seed_universe(db)
    db.execute(
        "UPDATE stock_score_snapshot SET fundamentals_fetched_at = '2025-01-01T00:00:00Z' "
        "WHERE calc_date = '2026-03-05'"
    )
    db.commit()
    obs, dropped = bt.load_observations(db, sample="all", max_fundamentals_age_days=120)
    assert dropped["stale_fundamentals"] == N_STOCKS
    assert all(o.calc_date != date(2026, 3, 5) for o in obs)


def test_formula_versions_are_reported_apart(db: sqlite3.Connection) -> None:
    _seed_universe(db)
    db.execute(
        "UPDATE stock_score_snapshot SET formula_version = 'old-v0' WHERE calc_date = '2026-01-05'"
    )
    db.commit()
    report = bt.evaluate(db, score="value", sample="all", horizons=["1m"])
    assert set(report.versions) == {"old-v0", SCORE_FORMULA_VERSION}
    assert report.versions["old-v0"][0].n_dates == 1
    only_new = bt.evaluate(db, formula_version=SCORE_FORMULA_VERSION, sample="all", horizons=["1m"])
    assert set(only_new.versions) == {SCORE_FORMULA_VERSION}


def test_nothing_mature_yet_reports_when_the_first_reading_comes(db: sqlite3.Connection) -> None:
    _seed_universe(db)
    db.execute("DELETE FROM price WHERE date > '2026-01-10'")
    db.commit()
    report = bt.evaluate(db, score="value", sample="all")
    assert all(n == 0 for n in report.coverage["mature_by_horizon"].values())
    assert report.coverage["first_reading_by_horizon"]["1m"] == "2026-02-04"
    assert report.to_dict()["coverage"]["observations"] == N_STOCKS * len(SNAPSHOT_DAYS)


def test_empty_database_gives_an_empty_report(db: sqlite3.Connection) -> None:
    report = bt.evaluate(db)
    assert report.coverage["snapshots"] == 0
    assert report.versions == {}
    assert any("no prices" in c for c in report.caveats)


def test_bad_arguments_are_refused(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        bt.evaluate(db, score="momentum")
    with pytest.raises(ValueError):
        bt.evaluate(db, sample="hourly")
    with pytest.raises(ValueError):
        bt.evaluate(db, horizons=["2y"])
