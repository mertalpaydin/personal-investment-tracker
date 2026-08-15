# ADR 0003: Cutting 45 tables and 11 phases down to a shippable core

**Status:** accepted
**Date:** 2026-08-15

## Context

The inherited design consists of a brief, a rationale document and a 711-line
SQL schema defining roughly 45 tables across three addenda. It specifies eleven
build phases covering FIFO tax lots, German capital-gains treatment, ETF
look-through exposure, Cariño-linked performance attribution, Monte Carlo goal
projection via block bootstrap, LLM-generated narrative, peer scanning, news
sentiment, savings-perimeter classification and scenario modelling.

None of it was implemented. There was no repository, no source file and no
database when this decision was taken.

The schema also cannot be applied twice: it has no `IF NOT EXISTS`, no version
table, and its addenda use `ALTER TABLE`, so it only works start-to-finish on a
fresh file and cannot express a second change.

## Decision

**Ship a lean core, then grow.** Version 1 covers CSV import → FIFO lots → daily
prices and FX → daily valuation → TWR/XIRR/volatility/drawdown → the German tax
engine → a JSON API with a minimal server-rendered debug UI. That is **16 tables**.

Deferred to a roadmap, explicitly and in the README rather than silently: ETF
look-through, Cariño attribution, Monte Carlo projection, LLM narration, what-if
scenarios, peer scanning, news sentiment. Around 29 tables.

`allowance_ledger` is dropped outright rather than deferred: its only substantive
column is a pure function of statute and marital status, so it becomes
`tax.statutory_cap(person, year)`.

**Two things are pulled *forward* rather than deferred**, against the general
principle of cutting:

- **Unit accounting** (five columns on `portfolio_daily`). The inherited design
  places this in a late addendum, but also notes that retrofitting it means
  recomputing all history. It costs five columns now.
- **`flow_class`** on `transaction_`, `NOT NULL`. Without it there is no way to
  distinguish a deposit from a dividend, and every time-weighted return is wrong.
  Unit accounting depends on it, so it cannot be added later either.

**Migrations replace the monolithic schema.** Numbered SQL files applied by a
small runner with a `schema_version` table storing a SHA-256 of each applied
file. Editing an already-applied migration makes the runner refuse, rather than
silently diverging from every other copy of the database. Not Alembic: SQLite,
one developer and no ORM make that pure ceremony.

`db.reset_derived()` truncates every recomputable table — lots, disposals, daily
positions and portfolios, accruals, allowance entries, loss pots, Vorabpauschale.
Any bug in the derived layer is fixed by reset-and-rebuild, never by patching
rows. This is the rationale document's "store facts, derive everything else"
made operational.

## Consequences

**Good.** The first useful output arrives in weeks rather than months. Each
deferred feature is one the tracker is *correct without*, so the cut costs
accuracy nowhere.

**Good, less obviously.** Several deferred features depend on data the source
does not contain — look-through needs holdings files, peer scanning needs a
curated universe with manually maintained fund metadata. Building them early
would have meant building the data-acquisition machinery before the ledger they
are supposed to describe.

**Bad.** The schema will grow by migration rather than arriving complete, so
early migrations will be superseded. The checksum guard makes that safe but not
free.

**A real risk, accepted.** Deferring look-through means currency and
concentration exposure are reported on a *quote-currency* basis in v1. A
euro-quoted S&P 500 ETF shows zero currency contribution while carrying full
dollar risk. That is mechanically correct and economically misleading, so the API
labels the basis explicitly rather than letting a dashboard imply the portfolio
is mostly euro-risk. Only look-through resolves it properly.

## Notes

The inherited documents are kept, sanitised, under `docs/reference/` with a
banner marking them aspirational. They are worth keeping: the tax rules in §7 of
the brief survive verbatim and are implemented as written, and the reasoning in
the rationale document explains *why* several schema choices are the way they
are. Keeping the design-then-descope arc visible is also more honest than a
repository that pretends the scope was always small.
