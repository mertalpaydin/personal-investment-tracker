"""German tax vocabulary, in English.

The tax engine is correct and its prose is already English, but the statutory
nouns were left untranslated everywhere: JSON keys, table headings, CLI output.
For a reader who does not speak German, ``Vorabpauschale`` is not a term of art,
it is a wall.

Two rules govern everything here.

**Plain English first, German in parentheses, never German alone.** The German
is kept because it is what appears on the annual tax certificate the figures
have to be reconciled against, and what any search for help will be phrased in.
Dropping it would trade one kind of unreadable for another.

**Nothing renames a key.** The API's existing field names are unchanged;
endpoints gain ``labels`` and ``glossary`` blocks alongside them. Renaming
would touch the tax engine, the CLI, four test modules and two database
columns, to solve a presentation problem in the wrong layer.

``detail`` is written for someone encountering the concept for the first time,
and is what the narration layer is given -- so a model explaining the tax
position is working from the same words as the page.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

#: Attached wherever a partial-exemption rate has been assumed rather than
#: confirmed. Repeated verbatim rather than paraphrased, so the same caution
#: reads the same way everywhere it appears.
ASSUMED_RATE_CAVEAT = (
    "This rate is an assumption. Check the fund's key information document, or "
    "its official tax classification notice (Grundlagenbescheid), before filing."
)

#: The disclaimer that belongs on anything tax-shaped leaving this tool.
PLANNING_ONLY = (
    "Planning worksheet. Your bank's annual tax certificate (Steuerbescheinigung) "
    "is the authoritative document."
)


@dataclass(frozen=True)
class Term:
    """One statutory concept, in the several lengths the interfaces need."""

    key: str
    #: Plain English with the German in parentheses. Table headings, prose.
    label: str
    #: The English alone, for places already carrying the German nearby.
    plain: str
    german: str
    #: One sentence. Tooltips, and the `glossary` block in API responses.
    one_line: str
    #: Two to four sentences, for someone meeting the idea for the first time.
    detail: str
    statute: str | None = None


def _term(
    key: str,
    plain: str,
    german: str,
    one_line: str,
    detail: str,
    statute: str | None = None,
) -> Term:
    return Term(
        key=key,
        label=f"{plain} ({german})",
        plain=plain,
        german=german,
        one_line=one_line,
        detail=" ".join(detail.split()),
        statute=statute,
    )


TERMS: dict[str, Term] = {
    t.key: t
    for t in (
        _term(
            "vorabpauschale",
            "Advance lump-sum tax charge",
            "Vorabpauschale",
            "Tax on a small notional return from an accumulating fund, charged "
            "even though nothing was paid out.",
            """
            Each January, German tax treats an accumulating fund that rose in value
            as if it had paid you a small distribution, and your bank withholds tax
            on that invented amount even though no money reached you. It is capped
            at the fund's actual gain for the year, so a fund that fell is charged
            nothing. Confirming the charge also raises the recorded cost of your
            holding, so the same euros are not taxed a second time when you sell.
            """,
            "§18 InvStG",
        ),
        _term(
            "basisertrag",
            "Deemed income",
            "Basisertrag",
            "The invented income figure the advance charge is calculated from.",
            """
            Your holding's value on 1 January, multiplied by a rate the government
            publishes, of which only 70% counts. It is then capped at the fund's
            actual increase in value over the year, and reduced by anything the
            fund did pay out. If the fund did not gain, this is zero.
            """,
            "§18 (1) InvStG",
        ),
        _term(
            "basiszins",
            "Government base rate",
            "Basiszins",
            "The rate the German finance ministry publishes each January for this calculation.",
            """
            Set once a year from long-dated German government bond yields, and
            published in the Federal Gazette in January. Only 70% of it is used.
            Until the rate for a year is published, the advance charge for that
            year genuinely cannot be computed -- which is why this tool refuses to
            show a figure rather than showing zero.
            """,
            "§18 (4) InvStG",
        ),
        _term(
            "teilfreistellung",
            "Partial tax exemption",
            "Teilfreistellung",
            "A fixed share of a fund's gains that is simply not taxed.",
            """
            Because a fund has already paid tax at company level, part of what it
            returns to you is exempt: 30% for an equity fund, 15% for a mixed fund,
            and nothing for a bond or money-market fund. It follows the fund's own
            stated rules rather than what it happens to hold today, and it applies
            to losses as well as gains -- a EUR 1,000 loss on an equity fund is
            only EUR 700 of deductible loss.
            """,
            "§20 InvStG",
        ),
        _term(
            "sparerpauschbetrag",
            "Tax-free allowance on investment income",
            "Sparerpauschbetrag",
            "The first EUR 1,000 of investment income each year is tax-free; "
            "EUR 2,000 for a jointly assessed couple.",
            """
            It covers interest, dividends, realised gains and advance charges
            together, in the order they arise during the year. It does not carry
            over: whatever is unused on 31 December is gone. That is what makes it
            worth deliberately realising a gain in December if room is left.
            """,
            "§20 (9) EStG",
        ),
        _term(
            "freistellungsauftrag",
            "Standing instruction to a bank to apply your allowance",
            "Freistellungsauftrag",
            "You have to tell each bank how much of your allowance to use there.",
            """
            The allowance is yours, but banks only apply it if instructed, and the
            amounts you file across all your banks must not exceed your total
            allowance. File too little at a bank and it withholds tax you did not
            owe, recoverable only by filing a return; file too much across banks
            and the split is wrong. One bank's instruction being exhausted while
            another sits unused is the common, expensive mistake.
            """,
            "§44a EStG",
        ),
        _term(
            "aktien_pot",
            "Losses on individual shares",
            "Aktienverluste",
            "A loss on an individual share can only cancel a gain on another individual share.",
            """
            German law keeps losses on individual shares in their own pot. They
            cannot reduce tax on fund gains, dividends or interest -- only on gains
            from selling other individual shares. Unused amounts carry forward
            indefinitely.
            """,
            "§20 (6) EStG",
        ),
        _term(
            "sonstige_pot",
            "Losses on everything else",
            "sonstige Verluste",
            "Losses on funds, bonds and derivatives can cancel any investment "
            "income except individual-share gains.",
            """
            The second and more useful pot. Anything that is not an individual
            share goes here, and it can be set against dividends, interest and fund
            gains alike. Unused amounts carry forward indefinitely.
            """,
            "§20 (6) EStG",
        ),
        _term(
            "kapitalertragsteuer",
            "Flat tax on investment income",
            "Kapitalertragsteuer plus Solidaritätszuschlag",
            "25%, plus a 5.5% surcharge on that tax: 26.375% in total.",
            """
            Withheld by the bank at source on anything above your allowance. The
            surcharge is charged on the tax, not on the income, which is why the
            combined figure is 26.375% and not 30.5%. Church tax, if you pay it,
            comes on top and is charged on the tax as well.
            """,
            "§32d EStG",
        ),
        _term(
            "grundlagenbescheid",
            "A fund's official tax classification notice",
            "Grundlagenbescheid",
            "The document that fixes which partial-exemption rate a fund gets.",
            """
            Until you have it, or at least the fund's key information document, the
            exemption rate shown here is an assumption. Getting it wrong changes
            every gain figure for that fund by up to 30%.
            """,
        ),
        _term(
            "steuerbescheinigung",
            "Your bank's annual tax certificate",
            "Steuerbescheinigung",
            "The authoritative statement of what your bank actually withheld.",
            """
            Issued after the end of each tax year. Where this tool and that
            document disagree, the document is right and this tool has a bug or is
            missing an input. Nothing here is a substitute for it.
            """,
        ),
        _term(
            "anlage_kap",
            "The investment-income page of a German tax return",
            "Anlage KAP",
            "Where these figures end up if you file a return.",
            """
            Filing is optional if your banks withheld correctly and your allowance
            was allocated properly. It is worth doing when allowance was left unused
            at one bank while another withheld tax, or when losses at one bank
            should offset gains at another.
            """,
        ),
        _term(
            "anschaffungskosten",
            "Original purchase cost in euros",
            "Anschaffungskosten",
            "Frozen at the exchange rate on the day you bought, not today's rate.",
            """
            For a foreign-currency holding this is why the euro gain differs from
            the local gain converted at today's rate. The law compares proceeds at
            the sale-date rate against cost at the purchase-date rate, so exchange
            rate movement between the two is part of the taxable gain.
            """,
            "§20 (4) EStG",
        ),
        _term(
            "tagesgeld",
            "Instant-access savings account",
            "Tagesgeld",
            "Savings you can withdraw at any time; the rate can change without notice.",
            """
            Interest accrues daily and is usually credited monthly or quarterly.
            Because the rate is variable, any projection of what it will pay is an
            assumption rather than a contract.
            """,
        ),
        _term(
            "festgeld",
            "Fixed-term deposit",
            "Festgeld",
            "Money locked away for an agreed term at an agreed rate.",
            """
            Both the rate and the maturity date are contractual, so the interest is
            known in advance rather than estimated. What happens at maturity is not:
            money that rolls over automatically at whatever rate is then on offer is
            the thing worth being reminded about before it happens.
            """,
        ),
    )
}

#: Field and database values mapped to the term that explains them. Lets a
#: caller hand over the keys it is about to render and get back exactly the
#: glossary entries needed, rather than the whole dictionary.
FIELD_TERMS: dict[str, str] = {
    "vorabpauschale_eur": "vorabpauschale",
    "taxable_eur": "vorabpauschale",
    "basisertrag_eur": "basisertrag",
    "basiszins": "basiszins",
    "teilfreistellung_eur": "teilfreistellung",
    "teilfreistellung_pct": "teilfreistellung",
    "statutory_cap_eur": "sparerpauschbetrag",
    "allowance_used_eur": "sparerpauschbetrag",
    "allowance_remaining_eur": "sparerpauschbetrag",
    "freistellungsauftrag_eur": "freistellungsauftrag",
    "AKTIEN": "aktien_pot",
    "SONSTIGE": "sonstige_pot",
    "estimated_tax_eur": "kapitalertragsteuer",
    "tax_rate": "kapitalertragsteuer",
    "cost_basis_eur": "anschaffungskosten",
    "TAGESGELD": "tagesgeld",
    "FESTGELD": "festgeld",
}


class UnknownTerm(KeyError):
    """Asked for a term this module does not define."""


def describe(key: str) -> Term:
    """The full term for a glossary key or a field name."""
    resolved = TERMS.get(key) or TERMS.get(FIELD_TERMS.get(key, ""))
    if resolved is None:
        raise UnknownTerm(key)
    return resolved


def label(key: str, *, default: str | None = None) -> str:
    """Plain English with the German in parentheses.

    Falls back to ``default`` (or the key itself) rather than raising, because
    a heading is not worth a 500.
    """
    try:
        return describe(key).label
    except UnknownTerm:
        return default if default is not None else key


def plain(key: str, *, default: str | None = None) -> str:
    """The English alone, for contexts already showing the German."""
    try:
        return describe(key).plain
    except UnknownTerm:
        return default if default is not None else key


def glossary_for(keys: Iterable[str]) -> dict[str, dict[str, str]]:
    """Glossary entries for the keys a response is about to use.

    Keyed by the *requested* key, so a caller can look up the field it holds
    without knowing which term explains it. Unknown keys are skipped: a
    response is not the place to discover a typo.
    """
    out: dict[str, dict[str, str]] = {}
    for key in keys:
        try:
            term = describe(key)
        except UnknownTerm:
            continue
        out[key] = {
            "label": term.label,
            "plain": term.plain,
            "german": term.german,
            "one_line": term.one_line,
            "detail": term.detail,
            **({"statute": term.statute} if term.statute else {}),
        }
    return out


def labels_for(keys: Iterable[str]) -> dict[str, str]:
    """Just the display labels, for a response that only needs headings."""
    return {key: describe(key).label for key in keys if _known(key)}


def _known(key: str) -> bool:
    try:
        describe(key)
    except UnknownTerm:
        return False
    return True
