"""Guessing what an instrument is, from what it is called.

`core/peers.py` will only compare holdings whose risk category was *stated*,
because its fallback -- asset class alone -- cannot tell a semiconductor fund
from a world tracker. That rule is right, and it left the owner typing a
category for every holding before any comparison worked at all.

This is the middle ground. An issuer names a fund after what it holds: "iShares
MSCI Global Semiconductors", "Lyxor Smart Overnight Return", "iShares NASDAQ
100". Reading that name is a far better guess than reading `asset_class`, and
it is a guess with a reason attached that the owner can check at a glance.

**It proposes, it never decides.** Every suggestion carries the words it
matched on, and nothing is saved until a person accepts it. The category then
becomes SEEDED like any other, because it is -- somebody looked at it and
agreed. A tool that quietly classified holdings for you would be back to
comparing a semiconductor fund with a world tracker, only with no one aware
of it.

**Order is everything, and the order is by specificity.** "iShares S&P Gold
Producers" is a fund of mining *companies*, not a claim on gold: the miner
rule has to be tried before the metal rule, or a share fund is filed as a
commodity. The rules below run in the order written, and the first to match
wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: (bucket, why, patterns). Tried in order; the first match wins. The `why` is
#: shown to the owner beside the suggestion, so it has to read as a reason.
RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # Cash-like first: "overnight" and "liquidity" funds are often named as
    # though they were bond or money funds, and they behave like neither.
    (
        "MONEY_MARKET",
        "named as an overnight or money-market fund",
        (
            "overnight",
            "money market",
            "liquidity",
            "t-bill",
            "treasury bill",
            "eonia",
            "ester",
            # Turkish: "para piyasasi" is a money-market fund.
            "para piyasas",
        ),
    ),
    # Miners before metals: a gold *producer* is a share, not a claim on gold.
    (
        "EQ_SECTOR",
        "a fund of mining or producer companies, not the metal itself",
        ("gold producer", "gold miner", "miners", "mining", "producers"),
    ),
    (
        "COMMODITY",
        "tracks a physical commodity",
        (
            "physical gold",
            "xetra-gold",
            "physical silver",
            "bullion",
            "commodity",
            "commodities",
            # Turkish: altin = gold, gumus = silver. Written without the
            # dotted capital and the cedilla so a name typed either way
            # still matches.
            "altın",
            "altin",
            "gümüş",
            "emtia",
        ),
    ),
    (
        "BOND",
        "named as a bond fund",
        (
            "bond",
            "treasury",
            "gilt",
            "aggregate",
            "corporate debt",
            "high yield",
            "government debt",
            "fixed income",
            "anleihe",
            # Turkish: tahvil = bond, borclanma = debt.
            "tahvil",
            "borçlanma",
        ),
    ),
    # Sector and theme words, before any region or index word: a European
    # banks fund is a sector fund that happens to be European.
    (
        "EQ_SECTOR",
        "named after one sector or theme",
        (
            "semiconductor",
            "bank",
            "technolog",
            "teknoloji",
            "health",
            "pharma",
            "biotech",
            "energy",
            "utilities",
            "industrial",
            "financial",
            "insurance",
            "real estate",
            "property",
            "reit",
            "infrastructure",
            "water",
            "clean ",
            "renewable",
            "automation",
            "robotic",
            "cyber",
            "cloud",
            "artificial",
            "ai & ",
            "big data",
            "digitalis",
            "gaming",
            "esports",
            "travel",
            "consumer",
            "telecom",
            "media",
            "luxury",
            "defence",
            "defense",
            "aerospace",
            "agricult",
            "sector",
        ),
    ),
    (
        "EQ_EM",
        "named after emerging markets",
        ("emerging", "em imi", "frontier", "china", "india", "brazil", "latin america"),
    ),
    (
        "EQ_US_LARGE",
        "named after a large US index",
        ("s&p 500", "sp 500", "nasdaq", "us large", "dow jones industrial", "russell 1000"),
    ),
    (
        "EQ_EUROPE",
        "named after a European index or region",
        (
            "stoxx",
            "dax",
            "mdax",
            "tecdax",
            "euro ",
            "europe",
            "european",
            "ftse 100",
            "cac",
            "smi",
            "eurozone",
        ),
    ),
    (
        "EQ_GLOBAL_BROAD",
        "named after a broad world index",
        (
            "world",
            "all-world",
            "all world",
            "acwi",
            "global",
            "developed markets",
            "msci wo",
            "ftse dev",
            "north america",
            "japan",
            "pacific",
        ),
    ),
)

#: Quote types the feed reports that settle the question on their own.
BY_ASSET_CLASS = {
    "CRYPTO": ("CRYPTO", "the feed reports it as a cryptocurrency"),
    "STOCK": ("EQ_SINGLE_STOCK", "a single company's shares"),
    "CASH": ("CASH", "a cash or deposit balance"),
    "BOND": ("BOND", "held as a bond"),
}


@dataclass(frozen=True)
class Suggestion:
    """A proposed category, and the reason to accept or reject it."""

    import_key: str
    bucket: str | None
    why: str
    #: The words in the name that produced it, so the owner can see the
    #: evidence rather than take the answer on trust.
    matched: str | None = None

    @property
    def confident(self) -> bool:
        return self.bucket is not None


def suggest(import_key: str, name: str | None, asset_class: str | None) -> Suggestion:
    """Propose a risk category. Never saves; never guesses silently.

    Asset class settles the easy cases -- a share is a share, a cryptocurrency
    is a cryptocurrency -- and the fund's own name settles the rest. Where
    neither does, the bucket is None and the owner is asked, because "I do not
    know what this is" is a better answer than a plausible wrong one.
    """
    cls = (asset_class or "").upper()
    if cls in BY_ASSET_CLASS:
        bucket, why = BY_ASSET_CLASS[cls]
        return Suggestion(import_key, bucket, why)

    text = (name or "").lower()
    if not text:
        return Suggestion(import_key, None, "nothing in its name to go on")

    for bucket, why, patterns in RULES:
        for pattern in patterns:
            if pattern in text:
                return Suggestion(import_key, bucket, why, matched=pattern.strip())

    if cls in ("ETF", "FUND"):
        # A fund whose name says nothing. Not filed as a world tracker on the
        # strength of being a fund: that is exactly the guess `core/peers.py`
        # refuses, and repeating it here with a friendlier label would be
        # laundering it.
        return Suggestion(import_key, None, "a fund, but its name does not say what it holds")
    return Suggestion(import_key, None, "nothing in its name or type to go on")


#: Words that mean the same fund is hedged back to another currency. Not a
#: category of its own -- it changes what you are exposed to, not what the
#: fund holds -- but worth surfacing beside a suggestion.
_HEDGED = re.compile(r"\bhdg\b|\bhedged\b|eur[- ]h\b", re.IGNORECASE)


def is_hedged(name: str | None) -> bool:
    """Whether the name says the fund is currency-hedged."""
    return bool(name and _HEDGED.search(name))
