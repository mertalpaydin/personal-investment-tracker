# ADR 0009: A goal projection with growth in it, that is still not a forecast

**Status:** accepted
**Date:** 2026-08-16
**Relates to:** [0003](0003-scope-cut.md) (cut Monte Carlo), [0008](0008-tax-scenarios-without-forecasting.md) (refused it again)

## Context

ADR 0008 refused to forecast, and was right to — for the question it was
answering. "What would this sale cost me in tax?" has an exact answer, and a
simulation could only degrade it.

The owner then pointed out that the argument had been over-generalised. *"When
do I reach €250,000?"* is a different question:

- It is **inherently about the future**. There is no exact answer to degrade.
- The current answer is not neutral, it is **wrong in a known direction**.
  `core/goal.py` assumes no investment return at all, so for any portfolio that
  compounds it always names a date too late. Refusing to model growth is not
  the conservative choice; it is a different model, silently applied.

So the honest position was not "no projections". It was that a projection must
not present an assumption as a finding.

## Decision

**Answer the question two ways, and let neither become a prediction.**

### 1. A sensitivity table, which is the headline

The reach date at 0%, 2%, 4%, 6% and 8% a year. This is **not five forecasts**.
It is the statement *"the answer depends on this, and here is how much"*.

The rates are spread wide on purpose. A table clustered around one plausible
figure reads as a forecast with error bars, which is precisely the impression to
avoid. And **the tool never picks a row** — there is no `projected_date` field
anywhere in the output, because a single date would be a number nobody chose
presented as one somebody did. `test_the_tool_never_picks_a_rate` asserts the
absence.

The most useful output is often that the spread is *narrow*. On the example
corpus, contributions dominate so heavily that 0% and 8% differ by three months
— which tells the owner the return assumption is not the thing to worry about.
No single projected date can say that.

### 2. A block bootstrap over the portfolio's own returns

Resampling the daily returns this portfolio actually produced, in 21-day blocks
so a month behaves like a month. This *is* a model and is labelled as one.

Its sample window is printed beside every figure derived from it, because
resampling a past assumes the future is drawn from the same distribution, and
**that assumption is the entire content of the answer**. Deciles are reported as
properties of the simulated paths — "earlier than 9 in 10 simulated paths" —
never as a probability about the owner's portfolio.

### Why this is not the Monte Carlo ADR 0003 cut

The distinction is where the numbers come from.

| | Source of the numbers |
|---|---|
| Cut in 0003 | Assumed market parameters: a μ and σ nobody observed |
| Built here | An assumption the owner states outright, or a return this portfolio actually made |

Nothing is invented, and no figure is offered without the thing it rests on
printed next to it. That is the same rule the rest of the tool follows; it was
never "no models", it was "no unattributed numbers".

### Refusals

- Under `MIN_BOOTSTRAP_DAYS` (250) of history, the bootstrap is **not offered**
  and says why. Less history is not a worse estimate, it is a different kind of
  object — the same reasoning `goal.py` already applies to contribution rates.
- The observed annual return is shown as **context, never as a scenario row**.
  What the portfolio did is history, not a rate to plan on.
- No tax, no fees, no inflation. Stated, not assumed away.

### Determinism

A fixed default seed, so the same inputs give the same answer. A projection that
moved when you re-ran it would be untrustworthy for reasons unrelated to its
model.

## Consequences

**Good.** The question has an answer with growth in it, and the answer carries
its own assumptions visibly. `pt goal show` keeps the zero-return extrapolation
for anyone who wants only what is falsifiable.

**Good.** `goal.add_months` stopped being private, since two modules now need
calendar-correct month arithmetic.

**Bad, and worth watching.** The example corpus returned **23.7% a year** over
527 days — a window containing one unusual run. The bootstrap therefore reports
dates *earlier* than the 8% row. That is arithmetically correct and it is
exactly the failure mode the sample window exists to flag. If anyone starts
reading the bootstrap median as the answer, the feature has failed regardless of
how correct its arithmetic is.

**A limitation, accepted.** A bootstrap cannot produce dispersion its sample does
not contain. A perfectly steady history collapses every simulated path onto one,
and the deciles coincide. That is correct — and pinned by a test — but it means
the spread is a property of the sample as much as of the future.

**A limitation, accepted.** 21-day blocks preserve within-month autocorrelation
and nothing longer. A multi-year regime, which is exactly what a decade-long
projection is most exposed to, is not represented at all.

## Alternatives rejected

**Keep refusing.** The status quo was not neutral — a zero-return assumption is
still an assumption, and it is wrong in a knowable direction.

**Pick one rate and project it.** Every source that does this produces a date
people plan around. The rate would be the tool's opinion wearing the authority
of arithmetic.

**Fit μ and σ and draw from a normal.** Two invented parameters and a
distributional assumption the data does not support, in exchange for a smoother
cone. The block bootstrap uses observations directly and is honest about how few
of them there are.

**Add inflation, tax and fees to make it "realistic".** Each is another
assumption, and the compound of four assumptions looks more authoritative while
being less checkable. The target is a nominal number; the projection matches it,
and says so.
