"""Economic exposure: what the portfolio owns, rather than what it is quoted in.

v1 could only answer "how much of this portfolio is in euro-denominated
instruments". That is not the question anybody is asking. A euro-quoted S&P 500
tracker is a dollar asset with a euro ticker, and reporting it as euro exposure
is the difference between a currency breakdown and a listing-venue breakdown.

This module answers the real question where the evidence exists and says so
where it does not. Every euro of holdings is attributed on exactly one of four
bases, and the basis travels with the number:

``LOOK_THROUGH``
    A published constituent list. The fund's market value is distributed across
    its holdings' currencies, countries, sectors and asset classes.

``DECLARED``
    No constituent list, but the owner recorded ``economic_currency`` on the
    instrument -- "this hedged share class is euro exposure", "this global
    tracker is dollar exposure". An assertion, not evidence, and only ever used
    for the currency dimension. Applying an unevidenced claim to *sector* would
    be inventing data rather than recording a judgement.

``DIRECT``
    Not a fund. A share in one company **is** its own exposure, so there is
    nothing to look through and this is a full answer, not a fallback.

``QUOTE_ONLY``
    A fund with no constituent list and no declaration. This is the v1 answer,
    and it is the one that is misleading. It is reported as its own figure so
    that "82% look-through" and "100% look-through" cannot be confused.

Two things this module deliberately does **not** do.

It does not touch the return decomposition. ``returns.py`` splits a gain into a
local part and a currency part using the rate the position was actually valued
at, and that split is an exact identity. Economic exposure is an estimate about
risk, not an observation about what happened, and folding it into a realised
figure would turn an identity into a model. Exposure informs what you might
lose next; the decomposition records what you did earn.

It does not invent a region taxonomy. The dimensions are the ones the files
themselves publish. Mapping ``country`` onto "Developed Europe" needs a table
of judgements nobody in this repository is qualified to maintain, and an
UNKNOWN bucket is more useful than a confident mistake.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from portfolio_tracker.money import ZERO

#: The dimensions a constituent file can support. ``account`` is missing on
#: purpose: where an instrument is custodied is a fact about the portfolio, not
#: about the exposure, and it has no meaning below the fund wrapper.
DIMENSIONS: tuple[str, ...] = ("currency", "country", "sector", "asset_class")

#: Attributes that stand in for the fund's own when it cannot be looked through.
#: ``country`` maps to the instrument's domicile, which for a fund is where the
#: wrapper is registered -- Ireland for most of them -- and is exactly the kind
#: of answer that needs the QUOTE_ONLY label attached.
_INSTRUMENT_COLUMN: dict[str, str] = {
    "currency": "currency",
    "country": "domicile",
    "sector": "sector",
    "asset_class": "asset_class",
}

#: The bucket a missing attribute lands in. Never inferred, never merged into
#: the largest neighbour.
UNKNOWN = "UNKNOWN"

#: Beyond this, a composition is old enough that the numbers derived from it
#: are about a fund that may no longer exist in that shape. Issuers publish
#: monthly at worst, so this is generous.
STALE_AFTER_DAYS = 180

LOOK_THROUGH = "look_through"
DECLARED = "declared"
DIRECT = "direct"
QUOTE_ONLY = "quote_only"

#: Best first. Coverage is reported in this order everywhere.
BASES: tuple[str, ...] = (LOOK_THROUGH, DECLARED, DIRECT, QUOTE_ONLY)


class NoValuation(LookupError):
    """There is no valued portfolio to report exposure for."""


@dataclass(frozen=True)
class Bucket:
    name: str
    value_eur: Decimal
    weight: Decimal


@dataclass(frozen=True)
class Attribution:
    """One holding's contribution, and on what evidence it was attributed."""

    import_key: str
    name: str
    value_eur: Decimal
    basis: str
    composition_date: date | None = None
    stale_days: int | None = None


@dataclass
class Coverage:
    """How much of the portfolio each basis accounts for."""

    total_eur: Decimal
    by_basis: dict[str, Decimal] = field(default_factory=dict)
    positions: list[Attribution] = field(default_factory=list)

    @property
    def look_through_eur(self) -> Decimal:
        return self.by_basis.get(LOOK_THROUGH, ZERO)

    @property
    def quote_only_eur(self) -> Decimal:
        return self.by_basis.get(QUOTE_ONLY, ZERO)

    @property
    def look_through_pct(self) -> Decimal:
        """The share attributed on evidence: constituents, or a share of one."""
        if self.total_eur <= ZERO:
            return ZERO
        evidenced = self.look_through_eur + self.by_basis.get(DIRECT, ZERO)
        return evidenced / self.total_eur

    @property
    def uncovered(self) -> list[Attribution]:
        """Funds reported on their wrapper because nothing better exists."""
        return [p for p in self.positions if p.basis == QUOTE_ONLY]

    @property
    def stale(self) -> list[Attribution]:
        return [
            p
            for p in self.positions
            if p.stale_days is not None and p.stale_days > STALE_AFTER_DAYS
        ]


@dataclass
class Exposure:
    as_of: date
    dimension: str
    total_eur: Decimal
    buckets: list[Bucket]
    coverage: Coverage
    caveats: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Underlying:
    """One issuer, aggregated across every fund that holds it."""

    key: str
    name: str
    identifier: str | None
    value_eur: Decimal
    weight: Decimal

    #: Which held instruments contribute, and how much each one does. This is
    #: the whole point: "EUR 4,120 of Nordwind, via ZGEQ and ZSEM".
    via: tuple[tuple[str, Decimal], ...]

    #: The distinct securities combined into this issuer -- share classes, ADRs,
    #: differently-spelled names. More than one means a judgement was made, and
    #: printing them is what makes a wrong judgement visible.
    securities: tuple[str, ...] = ()


@dataclass
class Concentration:
    as_of: date
    total_eur: Decimal
    resolved_eur: Decimal
    underlyings: list[Underlying]
    coverage: Coverage
    caveats: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reading the stored evidence
# ---------------------------------------------------------------------------


def latest_valuation_date(conn: sqlite3.Connection, person_id: int) -> date:
    row = conn.execute(
        "SELECT max(p.date) AS d FROM position_daily p JOIN account a ON a.id = p.account_id "
        "WHERE a.person_id = ?",
        (person_id,),
    ).fetchone()
    if row is None or row["d"] is None:
        raise NoValuation("no valued positions; run `pt rebuild` first")
    return date.fromisoformat(row["d"])


def composition_date(
    conn: sqlite3.Connection, instrument_id: int, on: date
) -> tuple[date, Decimal] | None:
    """The newest composition published at or before ``on``, and its raw sum.

    Bounded by the valuation date rather than by today, so a file downloaded
    this morning never rewrites what the portfolio was exposed to last year.
    """
    row = conn.execute(
        "SELECT as_of, weight_sum FROM fund_holding_set "
        "WHERE instrument_id = ? AND as_of <= ? ORDER BY as_of DESC LIMIT 1",
        (instrument_id, on.isoformat()),
    ).fetchone()
    if row is None:
        return None
    return date.fromisoformat(row["as_of"]), row["weight_sum"]


def _held(conn: sqlite3.Connection, person_id: int, on: date) -> list[sqlite3.Row]:
    """Every instrument held on ``on``, with its market value summed."""
    return conn.execute(
        """
        SELECT i.id, i.import_key, i.name, i.isin, i.currency, i.economic_currency,
               i.domicile, i.sector, i.asset_class, i.is_fund,
               p.market_value_eur
        FROM position_daily p
        JOIN instrument i ON i.id = p.instrument_id
        JOIN account a ON a.id = p.account_id
        WHERE p.date = ? AND a.person_id = ?
        """,
        (on.isoformat(), person_id),
    ).fetchall()


def _aggregate_by_instrument(rows: list[sqlite3.Row]) -> list[tuple[sqlite3.Row, Decimal]]:
    """One entry per instrument. The same fund can sit in two accounts."""
    values: dict[int, Decimal] = {}
    first: dict[int, sqlite3.Row] = {}
    for row in rows:
        key = int(row["id"])
        values[key] = values.get(key, ZERO) + (row["market_value_eur"] or ZERO)
        first.setdefault(key, row)
    return [(first[key], value) for key, value in values.items()]


def _basis_for(row: sqlite3.Row, dimension: str, composition: object) -> str:
    if composition is not None:
        return LOOK_THROUGH
    if dimension == "currency" and row["economic_currency"]:
        return DECLARED
    return QUOTE_ONLY if row["is_fund"] else DIRECT


def _fallback_bucket(row: sqlite3.Row, dimension: str, basis: str) -> str:
    if dimension == "currency" and basis == DECLARED:
        return str(row["economic_currency"]).upper()
    value = row[_INSTRUMENT_COLUMN[dimension]]
    return str(value) if value else UNKNOWN


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------


def exposure(
    conn: sqlite3.Connection,
    *,
    person_id: int = 1,
    dimension: str = "currency",
    as_of: date | None = None,
) -> Exposure:
    """Distribute holdings across one dimension, on the best basis available."""
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown dimension {dimension!r}; expected one of {DIMENSIONS}")
    on = as_of or latest_valuation_date(conn, person_id)

    buckets: dict[str, Decimal] = {}
    coverage = Coverage(total_eur=ZERO)
    scaled: list[tuple[str, Decimal]] = []

    for row, value in _aggregate_by_instrument(_held(conn, person_id, on)):
        coverage.total_eur += value
        composition = composition_date(conn, int(row["id"]), on)
        basis = _basis_for(row, dimension, composition)
        coverage.by_basis[basis] = coverage.by_basis.get(basis, ZERO) + value

        published, stale_days = None, None
        if composition is not None:
            published, weight_sum = composition
            stale_days = (on - published).days
            if weight_sum != Decimal(1):
                scaled.append((str(row["import_key"]), weight_sum))
            for line in conn.execute(
                "SELECT weight, currency, country, sector, asset_class FROM fund_holding "
                "WHERE instrument_id = ? AND as_of = ?",
                (row["id"], published.isoformat()),
            ):
                bucket = line[dimension]
                name = str(bucket).upper() if dimension == "currency" and bucket else bucket
                key = str(name) if name else UNKNOWN
                # Divided by the published sum, not assumed to be 1. The sum is
                # stored precisely so this scaling is a stated operation.
                buckets[key] = buckets.get(key, ZERO) + value * line["weight"] / weight_sum
        else:
            key = _fallback_bucket(row, dimension, basis)
            buckets[key] = buckets.get(key, ZERO) + value

        coverage.positions.append(
            Attribution(
                import_key=str(row["import_key"]),
                name=str(row["name"]),
                value_eur=value,
                basis=basis,
                composition_date=published,
                stale_days=stale_days,
            )
        )

    total = coverage.total_eur
    ordered = sorted(
        (
            Bucket(name, value, value / total if total > ZERO else ZERO)
            for name, value in buckets.items()
        ),
        key=lambda b: (-b.value_eur, b.name),
    )
    return Exposure(
        as_of=on,
        dimension=dimension,
        total_eur=total,
        buckets=ordered,
        coverage=coverage,
        caveats=_caveats(coverage, dimension, scaled),
    )


def _caveats(coverage: Coverage, dimension: str, scaled: list[tuple[str, Decimal]]) -> list[str]:
    """What bounds this breakdown. Ordered by how badly it misleads."""
    out: list[str] = []
    total = coverage.total_eur
    uncovered = coverage.uncovered
    if uncovered and total > ZERO:
        share = coverage.quote_only_eur / total
        names = ", ".join(sorted(p.import_key for p in uncovered))
        out.append(
            f"{share:.1%} of holdings ({names}) has no published composition and is "
            f"reported on the fund wrapper's own {dimension}, which is a listing fact "
            "rather than an economic one"
        )
    declared = coverage.by_basis.get(DECLARED, ZERO)
    if declared > ZERO and total > ZERO:
        out.append(
            f"{declared / total:.1%} rests on a declared economic_currency -- an assertion "
            "recorded by the owner, not a constituent list"
        )
    for entry in coverage.stale:
        out.append(
            f"{entry.import_key}'s composition is from {entry.composition_date} "
            f"({entry.stale_days} days before this valuation) and may no longer describe it"
        )
    for key, weight_sum in scaled:
        out.append(
            f"{key}'s published weights sum to {weight_sum:.6f}, not 1.0; the breakdown "
            "scales them to the whole fund"
        )
    if dimension == "country":
        out.append(
            "Country is where each constituent is domiciled or listed, as its issuer "
            "publishes it. It is not a revenue-exposure measure."
        )
    return out


# ---------------------------------------------------------------------------
# Concentration
# ---------------------------------------------------------------------------


def concentration(
    conn: sqlite3.Connection,
    *,
    person_id: int = 1,
    top: int | None = None,
    as_of: date | None = None,
) -> Concentration:
    """Underlying issuers, aggregated across every fund and direct holding.

    The number this exists to produce: a position in one company held through
    three different funds and directly is one exposure, and no view built on
    instruments can show it.

    Weights are against **total holdings**, including the part that could not be
    resolved. Weighting against the resolved part alone would inflate every
    figure by exactly the amount that is missing, which is the direction an
    honest concentration measure must not err in.
    """
    on = as_of or latest_valuation_date(conn, person_id)

    values: dict[str, Decimal] = {}
    names: dict[str, str] = {}
    identifiers: dict[str, str | None] = {}
    via: dict[str, dict[str, Decimal]] = {}
    coverage = Coverage(total_eur=ZERO)
    resolved = ZERO

    for row, value in _aggregate_by_instrument(_held(conn, person_id, on)):
        coverage.total_eur += value
        composition = composition_date(conn, int(row["id"]), on)
        # Concentration has no per-dimension fallback: an unlooked-through fund
        # is not an issuer and must not be listed as if it were one.
        basis = LOOK_THROUGH if composition else (DIRECT if not row["is_fund"] else QUOTE_ONLY)
        coverage.by_basis[basis] = coverage.by_basis.get(basis, ZERO) + value

        published, stale_days = None, None
        if composition is not None:
            published, weight_sum = composition
            stale_days = (on - published).days
            for line in conn.execute(
                "SELECT name, identifier, weight FROM fund_holding "
                "WHERE instrument_id = ? AND as_of = ?",
                (row["id"], published.isoformat()),
            ):
                contribution = value * line["weight"] / weight_sum
                key = underlying_key(line["identifier"], line["name"])
                values[key] = values.get(key, ZERO) + contribution
                names.setdefault(key, str(line["name"]))
                identifiers.setdefault(key, line["identifier"])
                holder = via.setdefault(key, {})
                holder[str(row["import_key"])] = (
                    holder.get(str(row["import_key"]), ZERO) + contribution
                )
                resolved += contribution
        elif basis == DIRECT:
            key = underlying_key(row["isin"], row["name"])
            values[key] = values.get(key, ZERO) + value
            names.setdefault(key, str(row["name"]))
            identifiers.setdefault(key, row["isin"])
            holder = via.setdefault(key, {})
            holder[str(row["import_key"])] = holder.get(str(row["import_key"]), ZERO) + value
            resolved += value

        coverage.positions.append(
            Attribution(
                import_key=str(row["import_key"]),
                name=str(row["name"]),
                value_eur=value,
                basis=basis,
                composition_date=published,
                stale_days=stale_days,
            )
        )

    total = coverage.total_eur
    ranked = _merge_by_issuer(values, names, identifiers, via, total)

    caveats = []
    if coverage.quote_only_eur > ZERO and total > ZERO:
        names_missing = ", ".join(sorted(p.import_key for p in coverage.uncovered))
        caveats.append(
            f"{coverage.quote_only_eur / total:.1%} of holdings ({names_missing}) has no "
            "published composition and contributes to no issuer below; the real "
            "concentration in anything it holds is higher than shown"
        )
    caveats.append(
        "Weights are against total holdings, so an unresolved fund dilutes every figure "
        "rather than inflating it."
    )
    combined = [u for u in ranked if len(u.securities) > 1]
    if combined:
        caveats.append(
            "Share classes and differently-spelled names are combined into one issuer -- "
            f"{', '.join(u.name for u in combined[:4])}"
            + (f" and {len(combined) - 4} more" if len(combined) > 4 else "")
            + ". Each line lists the securities it merged, so a wrong merge is visible."
        )
    for entry in coverage.stale:
        caveats.append(
            f"{entry.import_key}'s composition is {entry.stale_days} days old at this valuation"
        )

    return Concentration(
        as_of=on,
        total_eur=total,
        resolved_eur=resolved,
        underlyings=ranked[:top] if top else ranked,
        coverage=coverage,
        caveats=caveats,
    )


#: Legal-form suffixes issuers append inconsistently. "Nordwind Technologies"
#: and "NORDWIND TECHNOLOGIES AG" are one company, and counting them as two
#: makes the largest single-issuer exposure read LOWER than it is -- which is
#: the dangerous direction for a concentration figure.
LEGAL_SUFFIXES: frozenset[str] = frozenset(
    {
        "ag", "nv", "n v", "sa", "s a", "se", "plc", "inc", "incorporated", "corp",
        "corporation", "co", "company", "ltd", "limited", "llc", "lp", "spa", "s p a",
        "asa", "ab", "publ", "oyj", "as", "a s", "bv", "b v", "gmbh", "kgaa", "sas",
        "pte", "pcl", "tbk", "sae", "aps", "holding", "holdings", "group",
    }
)  # fmt: skip

#: Share-class and listing markers. The same company's A and B shares are the
#: same *issuer*, which is what concentration measures.
CLASS_MARKERS: frozenset[str] = frozenset(
    {
        "class", "cl", "ord", "ordinary", "reg", "registered", "shs", "shares", "share",
        "com", "common", "adr", "gdr", "sponsored", "unsponsored", "npv", "sp",
        "a", "b", "c", "i", "acc", "dist",
    }
)  # fmt: skip

#: Removed without leaving a gap, so ``N.V.`` becomes ``nv`` and ``A/S``
#: becomes ``as`` -- both of which the suffix list then recognises. Turning
#: them into ``n v`` instead leaves a stray token that stops the trim.
_ABBREVIATION_MARKS = re.compile(r"[./]")
_NAME_NOISE = re.compile(r"[^a-z0-9 ]+")


def normalise_issuer(name: str) -> str:
    """Reduce an issuer name to something two files can agree on.

    Strips punctuation, legal form and share-class markers, then collapses
    whitespace. ``Nordwind Technologies AG``, ``NORDWIND TECHNOLOGIES``, and
    ``Nordwind Technologies Corp. - Class A`` all become ``nordwind
    technologies``.

    Deliberately conservative about what it removes. Two genuinely different
    companies merged into one would *overstate* concentration, and a
    concentration figure that overstates gets acted on. Everything in the lists
    above is a legal form or a share class, never a distinguishing word --
    which is why the trailing-token loop stops at the first token it does not
    recognise rather than filtering the whole name.
    """
    cleaned = _NAME_NOISE.sub(" ", _ABBREVIATION_MARKS.sub("", name.lower()))
    tokens = cleaned.split()
    # Only from the end: "Group Nordwind" must keep its first word, and a
    # company genuinely called "Class Editori" must keep its name.
    while len(tokens) > 1 and (tokens[-1] in LEGAL_SUFFIXES or tokens[-1] in CLASS_MARKERS):
        tokens.pop()
    return " ".join(tokens) or cleaned.strip()


def _merge_by_issuer(
    values: dict[str, Decimal],
    names: dict[str, str],
    identifiers: dict[str, str | None],
    via: dict[str, dict[str, Decimal]],
    total: Decimal,
) -> list[Underlying]:
    """Combine securities into issuers, then rank them.

    Two passes, and the second one is the point. Grouping on the identifier is
    right for matching *securities* across files and wrong for the question
    concentration answers, because **an ISIN identifies a security, not a
    company**. Alphabet's A and C shares have different ISINs; so does any
    company held both directly and as an ADR inside a fund.

    Left at one pass, a portfolio holding Alphabet directly and through two
    funds reported it on three separate lines -- the largest showing 31.8% where
    the true exposure was 36.2%. Under-reporting concentration is the direction
    nobody checks, because the number looks reassuring.

    So groups whose names normalise alike are merged, and every merged line
    carries the securities that went into it. That visibility is the safeguard:
    the merge is a judgement about names, and a judgement nobody can inspect is
    one that gets trusted further than it should be.
    """
    canonical: dict[str, str] = {}
    by_issuer: dict[str, str] = {}
    # Largest first, so the surviving line takes the biggest holding's name and
    # the result does not depend on which file happened to be read first.
    for key in sorted(values, key=lambda k: (-values[k], k)):
        issuer = normalise_issuer(names[key])
        canonical[key] = by_issuer.setdefault(issuer, key)

    merged_value: dict[str, Decimal] = {}
    merged_via: dict[str, dict[str, Decimal]] = {}
    merged_ids: dict[str, list[str]] = {}
    for key, value in values.items():
        target = canonical[key]
        merged_value[target] = merged_value.get(target, ZERO) + value
        holders = merged_via.setdefault(target, {})
        for holder, amount in via[key].items():
            holders[holder] = holders.get(holder, ZERO) + amount
        merged_ids.setdefault(target, []).append(identifiers[key] or names[key])

    return sorted(
        (
            Underlying(
                key=key,
                name=names[key],
                identifier=identifiers[key],
                value_eur=value,
                weight=value / total if total > ZERO else ZERO,
                via=tuple(sorted(merged_via[key].items(), key=lambda kv: (-kv[1], kv[0]))),
                securities=tuple(sorted(set(merged_ids[key]))),
            )
            for key, value in merged_value.items()
        ),
        key=lambda u: (-u.value_eur, u.name),
    )


def underlying_key(identifier: object, name: object) -> str:
    """Group on the identifier when there is one, otherwise the normalised name.

    An ISIN is proof; a name is a guess. When two files supply neither, the
    names are normalised so that a legal-form suffix or a share-class marker
    does not split one company into two -- which understates the exposure, and
    understating is the direction that gets missed.
    """
    if identifier:
        return f"id:{str(identifier).upper()}"
    return f"name:{normalise_issuer(str(name))}"


# ---------------------------------------------------------------------------
# What data exists
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FundCoverage:
    """One held fund and the composition available for it, if any."""

    instrument_id: int
    import_key: str
    name: str
    value_eur: Decimal
    is_fund: bool
    composition_date: date | None
    holdings: int | None
    weight_sum: Decimal | None
    source: str | None
    stale_days: int | None

    @property
    def covered(self) -> bool:
        return self.composition_date is not None

    @property
    def stale(self) -> bool:
        return self.stale_days is not None and self.stale_days > STALE_AFTER_DAYS


def fund_coverage(
    conn: sqlite3.Connection, *, person_id: int = 1, as_of: date | None = None
) -> list[FundCoverage]:
    """Every held instrument, with the composition that would be used for it."""
    on = as_of or latest_valuation_date(conn, person_id)
    out: list[FundCoverage] = []
    for row, value in _aggregate_by_instrument(_held(conn, person_id, on)):
        composition = composition_date(conn, int(row["id"]), on)
        detail = None
        if composition is not None:
            detail = conn.execute(
                "SELECT holdings, weight_sum, source FROM fund_holding_set "
                "WHERE instrument_id = ? AND as_of = ?",
                (row["id"], composition[0].isoformat()),
            ).fetchone()
        out.append(
            FundCoverage(
                instrument_id=int(row["id"]),
                import_key=str(row["import_key"]),
                name=str(row["name"]),
                value_eur=value,
                is_fund=bool(row["is_fund"]),
                composition_date=composition[0] if composition else None,
                holdings=detail["holdings"] if detail else None,
                weight_sum=detail["weight_sum"] if detail else None,
                source=detail["source"] if detail else None,
                stale_days=(on - composition[0]).days if composition else None,
            )
        )
    return sorted(out, key=lambda f: (-f.value_eur, f.import_key))
