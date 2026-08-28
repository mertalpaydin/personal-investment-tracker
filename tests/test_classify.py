"""Reading what a fund is from what it is called.

Every test here is a way the suggestion could be confidently wrong, which is
worse than being absent: `core/peers.py` refuses to compare on a guess
precisely so that a semiconductor fund is never measured against a world
tracker, and a bad proposal accepted at a glance would put that back.

``test_a_gold_producer_fund_is_shares_not_a_commodity``
    The ordering trap. "iShares S&P Gold Producers" holds mining companies;
    matching the metal first files an equity fund as a commodity.

``test_a_fund_that_says_nothing_is_left_alone``
    A fund with an uninformative name must come back as None. Filing it as a
    world tracker on the strength of being a fund is the exact guess the peer
    engine exists to refuse.
"""

from __future__ import annotations

import pytest

from portfolio_tracker.core import classify, peers


def bucket(name: str, asset_class: str = "ETF") -> str | None:
    return classify.suggest("X", name, asset_class).bucket


# ---------------------------------------------------------------------------
# What the asset class settles on its own
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("asset_class", "expected"),
    [
        ("STOCK", "EQ_SINGLE_STOCK"),
        ("CRYPTO", "CRYPTO"),
        ("CASH", "CASH"),
        ("BOND", "BOND"),
    ],
)
def test_the_asset_class_answers_the_easy_cases(asset_class: str, expected: str) -> None:
    assert classify.suggest("X", "Anything at all", asset_class).bucket == expected


# ---------------------------------------------------------------------------
# Order of the rules
# ---------------------------------------------------------------------------


def test_a_gold_producer_fund_is_shares_not_a_commodity() -> None:
    """A miner is a company. Its shares fall when equities fall."""
    assert bucket("iShares S&P Gold Producers UCITS") == "EQ_SECTOR"
    assert bucket("VanEck Gold Miners UCITS ETF") == "EQ_SECTOR"
    # And the metal itself still is one.
    assert bucket("Xetra-Gold") == "COMMODITY"
    assert bucket("iShares Physical Gold ETC") == "COMMODITY"


def test_a_european_sector_fund_is_a_sector_fund() -> None:
    """The sector is the more specific fact, so it wins over the region."""
    assert bucket("Lyxor UCITS Stoxx Banks C-EUR") == "EQ_SECTOR"
    assert bucket("iShares STOXX Europe 600 Technology") == "EQ_SECTOR"
    # With no sector word, the region decides.
    assert bucket("iShares STOXX Europe 600 UCITS ETF") == "EQ_EUROPE"


def test_an_overnight_fund_is_cash_like_not_a_bond_fund() -> None:
    assert bucket("MULTI-UNITS LUXEMBOURG - Lyxor Smart Overnight Ret") == "MONEY_MARKET"
    assert bucket("Xtrackers II EUR Overnight Rate Swap") == "MONEY_MARKET"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("iShares VII PLC - iShares NASDAQ 100 ETF EUR Hdg Acc", "EQ_US_LARGE"),
        ("iShares Core S&P 500 UCITS ETF Acc", "EQ_US_LARGE"),
        ("iShares Core MSCI World UCITS ETF", "EQ_GLOBAL_BROAD"),
        ("Vanguard FTSE All-World UCITS ETF Acc", "EQ_GLOBAL_BROAD"),
        ("iShares Core MSCI EM IMI UCITS ETF", "EQ_EM"),
        ("iShares Core Euro Aggregate Bond", "BOND"),
        ("iShares MSCI Global Semiconductors UCITS USD Acc", "EQ_SECTOR"),
    ],
)
def test_index_and_asset_names_land_in_the_right_bucket(name: str, expected: str) -> None:
    assert bucket(name) == expected


# ---------------------------------------------------------------------------
# Turkish names, in their own spelling
# ---------------------------------------------------------------------------


def test_turkish_fund_names_are_read_too() -> None:
    """Two of these holdings are Turkish, and transliterating would not match."""
    assert bucket("İş Portföy Altın Fonu", "FUND") == "COMMODITY"
    assert bucket("Ak Portföy Yeni Teknolojiler Fonu", "FUND") == "EQ_SECTOR"


# ---------------------------------------------------------------------------
# Refusing to guess
# ---------------------------------------------------------------------------


def test_a_fund_that_says_nothing_is_left_alone() -> None:
    found = classify.suggest("X", "Some Capital Partners Fund II", "ETF")
    assert found.bucket is None
    assert found.confident is False
    assert "does not say" in found.why


def test_an_instrument_with_no_name_is_left_alone() -> None:
    assert classify.suggest("X", None, "ETF").bucket is None
    assert classify.suggest("X", "", None).bucket is None


# ---------------------------------------------------------------------------
# The contract with the peer engine
# ---------------------------------------------------------------------------


def test_every_bucket_it_can_propose_is_one_peers_knows() -> None:
    """A proposal the engine cannot match on would be silently useless."""
    proposed = {b for b, _, _ in classify.RULES}
    proposed |= {b for b, _ in classify.BY_ASSET_CLASS.values()}
    assert proposed <= set(peers.BUCKETS), sorted(proposed - set(peers.BUCKETS))


def test_a_suggestion_shows_the_words_it_matched() -> None:
    """Accepting should be a judgement about evidence, not trust."""
    found = classify.suggest("SEC0", "iShares MSCI Global Semiconductors UCITS", "ETF")
    assert found.matched == "semiconductor"


def test_a_hedged_fund_is_noticed() -> None:
    assert classify.is_hedged("iShares NASDAQ 100 ETF EUR Hdg Acc")
    assert classify.is_hedged("Vanguard Global Aggregate Bond EUR-H")
    assert not classify.is_hedged("iShares Core MSCI World UCITS ETF")
