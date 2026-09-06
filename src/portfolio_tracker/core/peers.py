"""Instruments comparable to one you hold, and how they actually did.

The sentence this exists to produce, in the owner's own words:

    "this ETF, which has the same risk level as one of yours, over the last
    three years earned 7% more"

Both halves of that are computed here from stored prices and a stated
taxonomy. Nothing forecasts, nothing recommends, and nothing here decides what
the difference means -- a fund that returned more also took some risk to do it,
and the figures that would let a reader see that are reported alongside rather
than reduced to a score.

## What "same risk level" means

Two things have to match, and both are stated rather than fitted:

**The bucket** -- what the instrument *is*. A judgement, seeded per instrument
(``instrument.risk_bucket``) and derived crudely when absent. Volatility alone
cannot supply this: a gold-miners ETF and a world tracker can post the same
volatility for a year while being entirely different propositions, and pairing
them would produce exactly the confident nonsense this tool exists to avoid.

**The volatility band** -- how much it actually moved. Derived, with cut points
fixed as constants rather than fitted to the sample, so two scans run a day
apart stay comparable. A band that moves when the universe moves would make
yesterday's answer un-checkable.

## The trap that would make this quietly wrong

A distributing fund measured on closing prices alone against an accumulating
one measured on total return understates the first by roughly its dividend
yield -- several percentage points a year, in the direction that makes the
wrong fund look better. In the live database this is not hypothetical: the
yfinance feed supplies adjusted closes and the Turkish fund feeds do not.

So every candidate carries ``comparable``, false whenever its return basis
differs from the subject's. Those candidates sort last, carry a caveat naming
the reason, and the narration layer is required to repeat it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, cast

from portfolio_tracker.core import fx, look_through, performance
from portfolio_tracker.money import ZERO

#: Volatility cut points. Stated constants, not fitted to whatever is in the
#: database today: a band that moves with the sample makes two scans run on
#: consecutive days incomparable, and the whole value here is comparability.
VOL_BAND_CUTS: tuple[Decimal, Decimal] = (Decimal("0.10"), Decimal("0.20"))

#: Fewest observations before an instrument may appear as a candidate at all.
#: Roughly a year of trading. Below it the return figures exist but say more
#: about the window than the instrument.
MIN_HISTORY_OBSERVATIONS = 200

#: The same requirement, expressed as time rather than as rows.
#:
#: The row count alone stopped meaning "about a year" the moment candidates
#: began to be priced weekly: a full year of weekly closes is 52 rows, so a
#: 200-row floor excluded the entire comparison set and the panel went back to
#: being empty -- with nothing anywhere reporting a reason. What the rule was
#: always reaching for is a year of history, and this measures that directly.
MIN_HISTORY_DAYS = 350

#: The vocabulary the derived fallback uses. Seeded values are free text and
#: need not come from this list; it is here so a seed file has something to
#: be consistent with.
BUCKETS = (
    "EQ_GLOBAL_BROAD",
    "EQ_US_LARGE",
    "EQ_EUROPE",
    "EQ_EM",
    "EQ_SECTOR",
    "EQ_SINGLE_STOCK",
    "BOND",
    "MONEY_MARKET",
    "COMMODITY",
    "CRYPTO",
    "CASH",
    "OTHER",
)

#: Standard mapping from risk bucket to numerical risk level (1 to 7 SRI standard).
DEFAULT_RISK_LEVEL: dict[str, int] = {
    "CASH": 1,
    "MONEY_MARKET": 1,
    "BOND": 3,
    "EQ_GLOBAL_BROAD": 4,
    "EQ_US_LARGE": 4,
    "EQ_EUROPE": 4,
    "EQ_EM": 5,
    "COMMODITY": 5,
    "EQ_SECTOR": 6,
    "EQ_SINGLE_STOCK": 6,
    "CRYPTO": 7,
    "OTHER": 4,
}


class PeerError(ValueError):
    """The scan cannot be run as asked."""


@dataclass
class Candidate:
    """One instrument measured against the subject."""

    import_key: str
    name: str
    status: str
    bucket: str
    vol_band: str | None
    basis: str
    #: False when this cannot be fairly compared with the subject -- almost
    #: always because one side has dividend-adjusted history and the other
    #: does not. Such candidates are ranked last and say why.
    comparable: bool
    observations: int
    risk_level: int | None = None

    #: Shorter windows too. One year alone is a single number about a period
    #: that may have been all of the story or none of it, and with a year of
    #: history it is the only window the longer measures can fill at all.
    ret_1m: Decimal | None = None
    ret_3m: Decimal | None = None
    ret_6m: Decimal | None = None
    ret_1y: Decimal | None = None
    ret_3y_ann: Decimal | None = None
    vol_1y: Decimal | None = None
    vol_3y: Decimal | None = None
    max_dd_3y: Decimal | None = None
    sharpe_1y: Decimal | None = None
    sharpe_3y: Decimal | None = None

    ret_1y_delta: Decimal | None = None
    ret_3y_ann_delta: Decimal | None = None
    vol_3y_delta: Decimal | None = None

    ter_bps: int | None = None
    ter_bps_delta: int | None = None

    #: Weight overlap with the subject, where both have an imported
    #: composition. None means unknown, which is not the same as zero.
    overlap_pct: Decimal | None = None

    #: Fundamental valuation metrics (populated for single stocks)
    sector: str | None = None
    industry: str | None = None
    forward_pe: Decimal | None = None
    trailing_pe: Decimal | None = None
    fcf_yield: Decimal | None = None
    dividend_yield: Decimal | None = None
    price_to_book: Decimal | None = None
    ev_ebitda: Decimal | None = None
    pct_off_52w_high: Decimal | None = None
    target_upside_pct: Decimal | None = None
    value_score: int | None = None
    value_badge: str | None = None
    value_narrative: str | None = None

    caveats: list[str] = field(default_factory=list)


@dataclass
class PeerScan:
    as_of: date
    subject: Candidate
    bucket: str
    vol_band: str | None
    risk_level: int | None = None
    #: SEEDED when the risk category was stated, DERIVED when guessed from
    #: asset class. A whole-portfolio sweep shows only the stated ones.
    bucket_method: str = "DERIVED"
    mode: str = "value"
    is_stock: bool = False
    sector: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    universe_size: int = 0
    considered: int = 0
    #: (key, why) for candidates that could not be measured at all. Named
    #: rather than silently dropped: a shorter list with no explanation looks
    #: like a smaller universe.
    excluded: list[tuple[str, str]] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def vol_band_for(vol: Decimal | None) -> str | None:
    """LOW / MID / HIGH, or None when volatility could not be measured.

    None rather than a default band: putting an unmeasurable instrument in
    the middle would let it match things it may have nothing in common with.
    """
    if vol is None:
        return None
    low, high = VOL_BAND_CUTS
    if vol < low:
        return "LOW"
    return "MID" if vol < high else "HIGH"


def derive_bucket(row: sqlite3.Row) -> str:
    """A crude bucket from what the database already knows.

    Deliberately crude, and only used when nothing has been stated. It cannot
    tell a semiconductor ETF from a world tracker -- both are `ETF` with no
    sector recorded -- which is the argument for seeding the real answer.
    """
    asset_class = (row["asset_class"] or "").upper()
    if asset_class == "CASH" or row["is_cash_proxy"]:
        return "CASH"
    if asset_class == "CRYPTO":
        return "CRYPTO"
    if asset_class == "BOND":
        return "BOND"
    if asset_class == "STOCK":
        return "EQ_SINGLE_STOCK"
    if asset_class in ("ETF", "FUND"):
        # A money-market fund is the one case the tax data settles: a fund
        # with a zero partial-exemption rate holds no equity.
        exemption = row["teilfreistellung_pct"]
        if exemption is not None and Decimal(str(exemption)) == ZERO:
            return "MONEY_MARKET"
        return "EQ_SECTOR" if row["sector"] else "EQ_GLOBAL_BROAD"
    return "OTHER"


def bucket_for(row: sqlite3.Row) -> tuple[str, str]:
    """The instrument's bucket and where it came from."""
    seeded = row["risk_bucket"] if "risk_bucket" in row.keys() else None  # noqa: SIM118
    if seeded:
        return str(seeded), "SEEDED"
    return derive_bucket(row), "DERIVED"


def risk_level_for(row: sqlite3.Row, bucket: str | None = None) -> int:
    """The instrument's integer risk level (1 to 7 SRI standard)."""
    if "risk_level" in row.keys() and row["risk_level"] is not None:  # noqa: SIM118
        try:
            return int(row["risk_level"])
        except (ValueError, TypeError):
            pass
    b = bucket or (
        row["risk_bucket"]
        if "risk_bucket" in row.keys() and row["risk_bucket"]  # noqa: SIM118
        else derive_bucket(row)
    )
    return DEFAULT_RISK_LEVEL.get(str(b), 4)


# ---------------------------------------------------------------------------
# Overlap
# ---------------------------------------------------------------------------


def _composition(conn: sqlite3.Connection, instrument_id: int, on: date) -> dict[str, Decimal]:
    """The newest published composition at or before ``on``, as key -> weight.

    Normalised by the header's stated sum rather than by the summed lines: a
    published file may not add to 1, and the header is what records what it
    did add to. Grouped with `look_through.underlying_key`, so an overlap and
    an exposure figure agree about what counts as the same company.
    """
    found = look_through.composition_date(conn, instrument_id, on)
    if found is None:
        return {}
    as_of, raw_sum = found
    total = Decimal(str(raw_sum))
    if total <= ZERO:
        return {}
    weights: dict[str, Decimal] = {}
    for row in conn.execute(
        "SELECT name, identifier, weight FROM fund_holding WHERE instrument_id = ? AND as_of = ?",
        (instrument_id, as_of.isoformat()),
    ):
        key = look_through.underlying_key(row["identifier"], row["name"])
        weights[key] = weights.get(key, ZERO) + Decimal(str(row["weight"])) / total
    return weights


def overlap(
    conn: sqlite3.Connection, a_id: int, b_id: int, *, on: date | None = None
) -> Decimal | None:
    """Share of weight the two funds hold in common, or None if unknowable.

    ``sum(min(weight_a, weight_b))`` over matched underlyings. None when
    either side has no imported composition -- which is not zero, and the
    difference matters: "these two hold nothing in common" and "nobody has
    told me what these hold" are opposite conclusions.
    """
    when = on or date.today()
    left = _composition(conn, a_id, when)
    right = _composition(conn, b_id, when)
    if not left or not right:
        return None
    shared = sum((min(left[k], right[k]) for k in left.keys() & right.keys()), ZERO)
    return shared


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def _instrument(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM instrument WHERE import_key = ? OR isin = ? OR symbol = ?",
        (key, key.upper(), key),
    ).fetchone()
    if row is None:
        raise PeerError(f"no instrument matches {key!r}")
    return cast("sqlite3.Row", row)


def _delta(candidate: Decimal | None, subject: Decimal | None) -> Decimal | None:
    return None if candidate is None or subject is None else candidate - subject


def _to_dec(val: Any) -> Decimal | None:
    if val is None or val == "" or val != val:
        return None
    try:
        return Decimal(str(val))
    except (ValueError, TypeError):
        return None


def _candidate(
    row: sqlite3.Row,
    m: performance.Metrics,
    bucket: str,
    *,
    risk_level: int | None = None,
    subject: performance.Metrics | None = None,
    fundamentals: dict[str, Any] | None = None,
) -> Candidate:
    band = vol_band_for(m.vol_3y or m.vol_1y)
    comparable = subject is None or m.basis == subject.basis
    level = risk_level if risk_level is not None else risk_level_for(row, bucket)

    out = Candidate(
        import_key=row["import_key"],
        name=row["name"],
        status=row["status"],
        bucket=bucket,
        risk_level=level,
        vol_band=band,
        basis=m.basis,
        comparable=comparable,
        observations=m.observations,
        ret_1m=m.ret_1m,
        ret_3m=m.ret_3m,
        ret_6m=m.ret_6m,
        ret_1y=m.ret_1y,
        ret_3y_ann=m.ret_3y_ann,
        vol_1y=m.vol_1y,
        vol_3y=m.vol_3y,
        max_dd_3y=m.max_dd_3y,
        sharpe_1y=m.sharpe_1y,
        sharpe_3y=m.sharpe_3y,
        ter_bps=row["ter_bps"],
        caveats=list(m.caveats),
    )
    if fundamentals is not None:
        out.sector = fundamentals.get("sector")
        out.industry = fundamentals.get("industry")
        out.forward_pe = _to_dec(fundamentals.get("forward_pe"))
        out.trailing_pe = _to_dec(fundamentals.get("trailing_pe"))
        out.fcf_yield = _to_dec(fundamentals.get("fcf_yield"))
        out.dividend_yield = _to_dec(fundamentals.get("dividend_yield"))
        out.price_to_book = _to_dec(fundamentals.get("price_to_book"))
        out.ev_ebitda = _to_dec(fundamentals.get("ev_ebitda"))
        out.pct_off_52w_high = _to_dec(fundamentals.get("pct_off_52w_high"))
        out.target_upside_pct = _to_dec(fundamentals.get("target_upside_pct"))
        out.value_score = (
            int(fundamentals["value_score"])
            if fundamentals.get("value_score") is not None
            else None
        )
        out.value_badge = fundamentals.get("value_badge")
        out.value_narrative = fundamentals.get("value_narrative")
    elif "sector" in row.keys() and row["sector"]:  # noqa: SIM118
        out.sector = row["sector"]

    if m.vol_3y is None and m.vol_1y is not None:
        out.caveats.append(
            "Risk band taken from one year of history rather than three, "
            "because three are not stored."
        )
    if subject is not None:
        out.ret_1y_delta = _delta(m.ret_1y, subject.ret_1y)
        out.ret_3y_ann_delta = _delta(m.ret_3y_ann, subject.ret_3y_ann)
        out.vol_3y_delta = _delta(m.vol_3y, subject.vol_3y)
        if not comparable:
            out.caveats.append(
                f"Not directly comparable: this is measured on {_basis_words(m.basis)} "
                f"and your holding on {_basis_words(subject.basis)}. A fund measured "
                "on price alone understates its total return by roughly what it pays out."
            )
    return out


def _has_enough_history(m: performance.Metrics, horizon: date, min_observations: int) -> bool:
    """Whether the series covers long enough to say anything about the fund.

    Either measure will do. A daily series clears the row count; a weekly one
    clears the span. Requiring both would exclude everything sampled weekly,
    and requiring neither would let a fortnight of prices rank as a peer.
    """
    if m.observations >= min_observations:
        return True
    if m.history_from is None:
        return False
    return (horizon - m.history_from).days >= MIN_HISTORY_DAYS


def _basis_words(basis: str) -> str:
    return "total return" if basis == "TOTAL_RETURN" else "price alone"


def scan(
    conn: sqlite3.Connection,
    *,
    instrument: str,
    top: int = 5,
    as_of: date | None = None,
    include_owned: bool = False,
    min_observations: int = MIN_HISTORY_OBSERVATIONS,
    mode: str | None = None,
) -> PeerScan:
    """Instruments in the same bucket and band as ``instrument``, ranked.

    For ETFs: ranked by three-year return difference (or one-year).
    For Stocks: when ``mode='value'`` (default), ranked by sector-alignment,
    composite value score, and forward P/E discount to surface cheap peers.
    When ``mode='momentum'``, ranked by past return difference.
    """
    horizon = as_of or date.today()
    subject_row = _instrument(conn, instrument)
    subject_id = int(subject_row["id"])
    is_stock = (subject_row["asset_class"] or "").upper() == "STOCK"
    scan_mode = mode or ("value" if is_stock else "momentum")

    fundamentals_by_id: dict[int, dict[str, Any]] = {}
    if is_stock:
        try:
            fund_rows = conn.execute("SELECT * FROM stock_fundamental").fetchall()
            fundamentals_by_id = {int(r["instrument_id"]): dict(r) for r in fund_rows}
        except sqlite3.OperationalError:
            fundamentals_by_id = {}

    subject_metrics = performance.metrics(
        conn,
        subject_id,
        import_key=subject_row["import_key"],
        currency=subject_row["currency"],
        as_of=horizon,
    )
    subject_bucket, method = bucket_for(subject_row)
    subject_level = risk_level_for(subject_row, subject_bucket)
    subject = _candidate(
        subject_row,
        subject_metrics,
        subject_bucket,
        risk_level=subject_level,
        fundamentals=fundamentals_by_id.get(subject_id),
    )
    band = subject.vol_band

    result = PeerScan(
        as_of=horizon,
        subject=subject,
        bucket=subject_bucket,
        risk_level=subject_level,
        vol_band=band,
        bucket_method=method,
        mode=scan_mode,
        is_stock=is_stock,
        sector=subject.sector,
    )
    if method == "DERIVED":
        result.caveats.append(
            f"No risk category is recorded for {subject.import_key}, so one was "
            "guessed from its asset class. Setting it makes these comparisons "
            "much sharper."
        )

    rows = conn.execute(
        "SELECT * FROM instrument WHERE id <> ? AND asset_class NOT IN ('CASH') "
        "ORDER BY import_key",
        (subject_id,),
    ).fetchall()
    result.universe_size = len(rows)

    candidates: list[Candidate] = []
    for row in rows:
        if row["status"] == "OWNED" and not include_owned:
            continue
        row_bucket, _ = bucket_for(row)
        if row_bucket != subject_bucket:
            continue

        try:
            m = performance.metrics(
                conn,
                int(row["id"]),
                import_key=row["import_key"],
                currency=row["currency"],
                as_of=horizon,
            )
        except fx.FxUnavailable:
            result.excluded.append((str(row["import_key"]), "no exchange rate for its currency"))
            continue
        if not _has_enough_history(m, horizon, min_observations):
            continue
        result.considered += 1

        row_level = risk_level_for(row, row_bucket)
        candidate = _candidate(
            row,
            m,
            row_bucket,
            risk_level=row_level,
            subject=subject_metrics,
            fundamentals=fundamentals_by_id.get(int(row["id"])),
        )
        if band is not None and candidate.vol_band is not None and candidate.vol_band != band:
            candidate.caveats.append(
                f"Moves {_band_words(candidate.vol_band)} than your holding "
                f"({candidate.vol_band.lower()} vs {band.lower()})."
            )
        if subject_row["ter_bps"] is not None and candidate.ter_bps is not None:
            candidate.ter_bps_delta = candidate.ter_bps - int(subject_row["ter_bps"])
        candidate.overlap_pct = overlap(conn, subject_id, int(row["id"]), on=horizon)
        candidates.append(candidate)

    if is_stock and scan_mode == "value":

        def rank_val(c: Candidate) -> tuple[int, int, int, int, Decimal, Decimal]:
            not_comp = 0 if c.comparable else 1
            # 1. Prefer same sector if subject sector is known
            same_sector = 0 if (subject.sector and c.sector and c.sector == subject.sector) else 1
            # 2. Prefer same volatility band
            same_band = 0 if (band is None or c.vol_band == band) else 1
            # 3. Higher composite value score is better (negated)
            score = -(c.value_score if c.value_score is not None else -1)
            # 4. Lower forward P/E is better
            pe = c.forward_pe if (c.forward_pe is not None and c.forward_pe > 0) else Decimal("999")
            # 5. Higher 1Y return as tie-breaker
            ret = -(c.ret_1y if c.ret_1y is not None else Decimal("-999"))
            return (not_comp, same_sector, same_band, score, pe, ret)

        candidates.sort(key=rank_val)
    else:

        def rank_mom(c: Candidate) -> tuple[int, int, Decimal, int]:
            same_band = 0 if (band is None or c.vol_band == band) else 1
            delta = c.ret_3y_ann_delta if c.ret_3y_ann_delta is not None else c.ret_1y_delta
            return (
                0 if c.comparable else 1,  # incomparable last, however well it scored
                same_band,
                -(delta if delta is not None else Decimal("-999")),
                c.ter_bps if c.ter_bps is not None else 9999,
            )

        candidates.sort(key=rank_mom)

    result.candidates = candidates[:top]

    if not candidates:
        result.caveats.append(
            "Nothing to compare against yet. Add instruments to your watchlist "
            "and they will be measured against your holdings here."
        )
    if any(c.overlap_pct is None for c in result.candidates):
        result.caveats.append(
            "Overlap is unknown for some candidates: it needs a published "
            "holdings file for both funds, and none has been imported."
        )
    if subject_row["ter_bps"] is None and not is_stock:
        result.caveats.append(
            f"No ongoing charge is recorded for {subject.import_key}, so fee "
            "differences are not shown. It is on the fund's key information document."
        )
    return result


def leaderboard(
    conn: sqlite3.Connection,
    *,
    risk_level: int | None = None,
    risk_bucket: str | None = None,
    top: int | None = 10,
    as_of: date | None = None,
    include_owned: bool = True,
    min_observations: int = 60,
) -> list[Candidate]:
    """Top-performing instruments for a given risk level or bucket.

    Ranked primarily by 3-year Sharpe / return where available, falling back
    gracefully to 1-year Sharpe / return so that instruments with shorter
    history are never dropped or disqualified.
    """
    horizon = as_of or date.today()
    where_clauses = ["asset_class NOT IN ('CASH')"]
    params: list[object] = []

    if risk_level is not None:
        where_clauses.append("risk_level = ?")
        params.append(risk_level)
    elif risk_bucket is not None:
        where_clauses.append("risk_bucket = ?")
        params.append(risk_bucket.upper())

    if not include_owned:
        where_clauses.append("status <> 'OWNED'")

    sql = f"SELECT * FROM instrument WHERE {' AND '.join(where_clauses)} ORDER BY import_key"
    rows = conn.execute(sql, params).fetchall()

    candidates: list[Candidate] = []
    for row in rows:
        bucket, _ = bucket_for(row)
        level = risk_level_for(row, bucket)
        if risk_level is not None and level != risk_level:
            continue
        try:
            m = performance.metrics(
                conn,
                int(row["id"]),
                import_key=row["import_key"],
                currency=row["currency"],
                as_of=horizon,
            )
        except fx.FxUnavailable:
            continue

        if not _has_enough_history(m, horizon, min_observations):
            continue

        c = _candidate(row, m, bucket, risk_level=level)
        candidates.append(c)

    def rank_key(c: Candidate) -> tuple[int, Decimal, Decimal]:
        sharpe = (
            c.sharpe_3y
            if c.sharpe_3y is not None
            else (c.sharpe_1y if c.sharpe_1y is not None else Decimal("-999"))
        )
        ret = (
            c.ret_3y_ann
            if c.ret_3y_ann is not None
            else (c.ret_1y if c.ret_1y is not None else Decimal("-999"))
        )
        return (
            0 if c.comparable else 1,
            -sharpe,
            -ret,
        )

    candidates.sort(key=rank_key)
    return candidates[:top] if top is not None else candidates


def _band_words(band: str) -> str:
    return {"LOW": "less", "MID": "somewhat differently", "HIGH": "more"}.get(band, "differently")


@dataclass
class PortfolioPeers:
    """Every comparison worth showing, and what is stopping the rest."""

    scans: list[PeerScan] = field(default_factory=list)
    #: Holdings skipped because nobody has said what kind of thing they are.
    needs_category: list[str] = field(default_factory=list)


def scan_portfolio(
    conn: sqlite3.Connection,
    *,
    person_id: int = 1,
    top: int = 3,
    as_of: date | None = None,
    mode: str | None = None,
) -> PortfolioPeers:
    """One scan per holding whose risk category has actually been stated.

    A holding with only a *guessed* category is listed rather than compared.
    The fallback cannot tell a semiconductor ETF from a world tracker -- both
    are `ETF` with no sector recorded -- and comparing them anyway produces a
    confident, precise, meaningless number. Naming what is missing is more
    use than answering the wrong question.
    """
    horizon = as_of or date.today()
    try:
        latest = look_through.latest_valuation_date(conn, person_id)
    except look_through.NoValuation:
        return PortfolioPeers()

    rows = conn.execute(
        """
        SELECT i.import_key FROM position_daily p
        JOIN instrument i ON i.id = p.instrument_id
        JOIN account a ON a.id = p.account_id
        WHERE p.date = ? AND a.person_id = ? AND i.asset_class NOT IN ('CASH')
        ORDER BY p.market_value_eur DESC
        """,
        (latest.isoformat(), person_id),
    ).fetchall()

    out = PortfolioPeers()
    for row in rows:
        try:
            found = scan(conn, instrument=row["import_key"], top=top, as_of=horizon, mode=mode)
        except PeerError:
            continue
        if found.bucket_method != "SEEDED":
            out.needs_category.append(row["import_key"])
            continue
        if found.candidates:
            out.scans.append(found)
    return out
