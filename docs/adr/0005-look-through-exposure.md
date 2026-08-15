# ADR 0005: Look-through exposure, kept separate from return attribution

**Status:** accepted
**Date:** 2026-08-15

## Context

ADR 0003 deferred ETF look-through and named the cost explicitly: currency and
concentration exposure would be reported on a **quote** basis, so a euro-quoted
S&P 500 tracker shows zero currency contribution while carrying full dollar
risk. The README listed it under *Known gaps*, and the handover put it first on
the v2 list.

The gap is larger than it reads. The v1 answer is not merely imprecise, it is a
different measurement: `dim=currency` reports the currency each instrument is
**listed** in, which for a portfolio of UCITS ETFs is almost always EUR
regardless of what those funds hold. On the example corpus every instrument is
euro- or lira-quoted, and the euro figure overstates actual euro exposure by
roughly €37,000 out of €155,000.

The same wrapper hides a second thing entirely: one company held through three
funds and directly is one exposure, and nothing computed at instrument level can
see it.

There is no feed for constituent data. Every issuer publishes its own CSV, on
its own schedule, with its own column names, under no obligation to keep any of
it stable.

## Decision

**Store published constituent lists; derive exposure from them on read.**

Two tables. `fund_holding_set` is one row per (fund, publication date) carrying
the source, the raw weight sum, the unit the file used and a hash of its bytes.
`fund_holding` is the lines. Weights are stored as fractions of fund NAV,
converted exactly once at parse time.

Four decisions inside that are the substance of this record.

### 1. The manual file is the contract, not the fallback

Same rule as prices (`ingest/providers/manual.py`) and for the same reason. A
scraper against an issuer's download endpoint is a dependency that fails
quietly; a file somebody chose to place is evidence with a provenance. `pt
look-through import` is the only way this data enters the system.

### 2. Weights are never normalised on the way in

Published files sum to 0.9997, or 1.0043, or 0.62 because somebody exported a
filtered view. The importer refuses anything outside 2% of a whole fund, stores
the raw sum, and the aggregation divides by it. So the scaling is a stated
operation on a recorded number, and `pt doctor` can check that the rows still
sum to what the file said — the one corruption nothing else would notice, since
percentages of a partial file still add to 100%.

### 3. The unit is resolved by arithmetic, and refused when ambiguous

The same column is `7.24` in one issuer's file and `0.0724` in another's.
`auto` commits only when the sum lands unmistakably near 1 or near 100;
otherwise it reports both readings and asks. This repository has already shipped
a double-conversion bug (`41.5` → `0.415` → `0.00415`) that survived review
because the wrong number was still plausible.

### 4. Four attribution bases, and the basis travels with the number

`look_through` (a constituent list) · `declared` (the owner's recorded
`economic_currency`, an assertion, currency only) · `direct` (not a fund, so the
instrument is its own exposure — a full answer, not a fallback) · `quote_only`
(a fund with neither; the v1 answer, and the honest gap).

Every response carries a `coverage` block. A breakdown that is 50% evidence and
one that is 100% evidence render identically without it, and the second is a far
stronger claim.

**Exposure does not touch the return decomposition.** `returns.py` splits a gain
into a local part and a currency part using the rate each position was actually
valued at, and that split is an exact identity checked by `doctor`. Look-through
is an estimate about risk, not an observation about what happened. Folding it in
would turn an identity into a model, and the model's error would then be
indistinguishable from a bug. Two numbers, two questions, both labelled.

**No region taxonomy.** The dimensions are the ones the files publish:
`currency`, `country`, `sector`, `asset_class`. Mapping countries onto
"Developed Europe" needs a table of judgements nobody here is qualified to
maintain, and an `UNKNOWN` bucket is more useful than a confident mistake. A
constituent with no country stays `UNKNOWN`; it is never filled in by inference.

## Consequences

**Good.** The largest honest gap in v1 closes, and the API contract barely
moves: `/api/allocation` gained a `basis` parameter and defaults to the old
behaviour. Concentration across funds becomes computable at all, which is new
information rather than a corrected number.

**Good, less obviously.** `economic_currency` was dead metadata in v1 — recorded
in the seed files and read by nothing. It is now a real, lower-ranked
attribution basis, which makes the distinction between "declared" and
"evidenced" visible instead of collapsing them.

**Bad.** Coverage is a maintenance obligation. A composition is a snapshot, it
goes stale, and nothing refreshes it. `LOOK_THROUGH_STALE` fires after 180 days
and the example corpus ships a deliberately stale file so the warning is
exercised rather than theoretical.

**A limitation, accepted.** Concentration groups on the identifier when a file
supplies one and on the upper-cased name otherwise. Two issuers publishing the
same company under different names and neither supplying an ISIN will not merge,
and the result understates concentration. Understating is the correct direction
for that error: a concentration figure that overstates is read as a warning and
acted on.

**A second limitation, accepted.** Exposure is computed against the *latest*
composition at or before the valuation date, so historical exposure uses whatever
was published then — which for most portfolios means "nothing, before the first
import". There is no back-fill and no interpolation between compositions.
