# Handover

**Status: v2 complete** (`0.2.0`, schema 10). 705 tests, `ruff` +
`mypy --strict` + PII guard clean, `pt doctor` reporting 0 errors on both the
real data and the synthetic corpus.

**Every item on the v1 v2-list is closed**, four built and one declined:

1. **ETF look-through** (item 1). Constituent lists imported from issuer files;
   exposure and concentration derived from them; every figure says which of
   four bases it rests on. [ADR 0005](adr/0005-look-through-exposure.md).
2. **Return attribution with Cariño linking** (item 3). Item 2 was declined by
   the owner. [ADR 0006](adr/0006-return-attribution-and-carino-linking.md) —
   **read its Context section before trusting §5 of this document**, because
   item 3's premise here was wrong and that is instructive.
3. **Vorabpauschale booking and the basis relief** (item 4).
   [ADR 0007](adr/0007-vorabpauschale-booking-and-basis-relief.md). Premise
   verified first this time, and it held.
4. **Tax scenarios** (item 5), scoped to today's prices and refusing the
   market forecast the original design specified.
   [ADR 0008](adr/0008-tax-scenarios-without-forecasting.md).
5. **Goal projection**, added afterwards at the owner's request. ADR 0008's
   refusal to forecast turned out to be over-generalised: *"when do I reach the
   target?"* is inherently about the future, and assuming zero return is not
   neutral, it is wrong in a known direction.
   [ADR 0009](adr/0009-goal-projection-without-a-forecast.md).

Item 2 (broker cash-flow import) was **declined by the owner**, not dropped for
technical reasons. It remains the highest-value item on §6 if a source is ever
found; the machinery is built and would simply stop guessing.

Items 4, 5 and the goal projection needed no migration at all — every column they
fill was declared in `0001_core` and never written.

Per-feature notes are in [`v2-changes.md`](v2-changes.md); this document is the
durable part — the invariants, the traps and how to work here.

This document is for whoever builds the rest. It says what exists, what it rests
on, what is deliberately absent, and which mistakes have already been made here
so they are not made again.

---

## 1. Read these first, in this order

| | |
|---|---|
| [`README.md`](../README.md) | What the project is and why. Its *Known gaps* section is honest and current. |
| [`docs/adr/`](adr/) | Nine decisions, each written from measurements against the real export. **0001 matters most; 0006 is the one to read on how a decision record can go wrong.** |
| [`docs/reference/`](reference/) | The pre-existing AI-written brief and spec, sanitised, with an honesty banner. Design *history*, not a description of the code. Several of its assumptions were proved wrong. |
| [`docs/v2-changes.md`](v2-changes.md) | Per-feature notes for what v2 built, and the traps found building it. |
| `src/portfolio_tracker/core/*.py` module docstrings | Each states what the module is for and which failure it prevents. They are not decoration; most of them record a bug that actually happened. |

The three files at the repo root before v1 (`IMPLEMENTATION.md`,
`portfolio-tracker-spec.md`, `schema.sql`) were sanitised and moved to
`docs/reference/` in commit `7631433`, before ever being staged — they contained
real financial targets and household details. They were never at the root in git
history, which is why they cannot be found there. `schema_full.sql` is the
original 45-table DDL, **kept for reference and never executed**; the live schema
is the migration chain.

---

## 2. What v1 does

```
CSV positions export
  → reconstruct a ledger (40 BUYs + 7 SELLs from 43 rows)
  → FIFO tax lots with frozen EUR cost basis
  → daily prices + ECB FX on the TARGET2 grid
  → daily valuation, three-way gain decomposition
  → unit accounting → TWR, XIRR, volatility, drawdown
  → German capital-gains engine
  → JSON API + server-rendered page
```

### Modules

| Module | Responsibility |
|---|---|
| `importers/investing_com/` | `parse → normalize → reconcile → import`. Stages 1–3 are pure. |
| `importers/deposits.py` | Two statement dialects, each checked against its own arithmetic. |
| `importers/overrides.py` | Authoritative values for fields the export destroyed. |
| `core/calendars.py` | TARGET2 / XETRA / TEFAS / ALL_DAYS. The Turkish holidays were derived from three years of observed NAVs. |
| `core/fx.py` | **The only conversion function in the codebase.** |
| `core/lots.py` | FIFO; freezes EUR cost at acquisition-date FX (German *Anschaffungskosten*). |
| `core/valuation.py` | Daily revaluation; forward-fill per leg, tracked and reported. |
| `core/cash.py` | Running balance; infers contributions; splits the balance by provenance. |
| `core/returns.py` | Unit accounting, TWR/XIRR. |
| `core/attribution.py` | The period return split by source, Cariño-linked. Two identities, checked. |
| `core/accounts.py` | Deposit day-counts, daily accrual, maturity projection. |
| `core/interest.py` | Accrued-but-unpaid interest → review inbox. |
| `core/tax.py` | German engine. Contains **zero** FX arithmetic and **zero** currency comparisons. |
| `core/vorabpauschale.py` | The January charge: proposed, booked, and relieved against the eventual gain. |
| `core/look_through.py` | Economic exposure and concentration, on four labelled bases. Touches no return figure. |
| `core/scenario.py` | Prices a sale before it is made. Read-only, today's prices, never a forecast. |
| `core/projection.py` | When the goal is reached, across stated returns and the portfolio's own history. |
| `ingest/look_through.py` | Issuer holdings files. Refuses an ambiguous weight unit rather than guessing. |
| `core/inbox.py` | What confirming an inference *means*, for every front end. |
| `core/rebuild.py` | Recompute the derived layer. Called by CLI and API alike. |
| `core/doctor.py` | Identity checks. |
| `core/goal.py` | Target and projection. |
| `api/app.py`, `api/render.py` | 20 endpoints; one page, plain Python, no template engine. |

### Migrations (schema version 10)

`0001_core` (17 tables) · `0002_fintables_price_source` · `0003_cash_flows` ·
`0004_portfolio_cash` · `0005_total_excludes_cash` · `0006_settings` ·
`0007_cash_provenance` · `0008_pending_dedup` · `0009_look_through` ·
`0010_return_income_contribution`

The runner pins a SHA-256 of every applied file and **refuses to run if an
applied migration's bytes changed**. Never edit an applied migration; add a new
one.

### CLI

`init-db` · `import` · `seed` · `fetch-fx` · `fetch-prices` · `rebuild` ·
`report` · `tax` · `doctor` · `serve` · `flows {list,propose,confirm,reject,sale}` ·
`accounts {list,add,set,import,accrue,propose-interest}` ·
`look-through {import,list,show,exposure,concentration}` ·
`vorabpauschale {forecast,propose,list,check}` · `scenario {sell,harvest}` ·
`goal {show,set,project}`

Plus [`run.py`](../run.py): one file, runnable from an IDE, that starts the
server and opens a browser once it answers.

---

## 3. The invariants. Break these and the numbers lie quietly

These are not style preferences. Each one has a test, and most have a scar.

1. **FX is stored one direction only** — quote units per 1 EUR, enforced by a
   `CHECK (base = 'EUR')`. Conversion is *always division*. There is no
   multiply-path anywhere. `tests/test_fx.py` greps the source to prove that
   only six named modules call `to_eur`.

2. **`flow_class` decides whether money is return or savings.** A dividend or
   interest payment booked as `EXTERNAL_IN` makes unit accounting issue units
   for it, cancelling exactly the return it represents — the tool would report
   the owner as having saved his own income.

3. **The headline total is holdings + deposits. Never cash.** Dividend income
   and sale proceeds sit beside it. Whether that money is still held is not
   knowable from the source, so counting it would be a guess. `doctor` enforces
   this (`CASH_LEAKED_INTO_TOTAL`).

4. **Returns run on `valuation_basis_eur`, which *does* include cash.** Excluding
   it would make a sale read as a loss on settlement day and a distribution's
   ex-date price fall read as a loss with nothing offsetting it. Two numbers for
   two questions, both labelled.

5. **Nothing auto-generated writes to `transaction_`.** Inferences become
   `pending_entry` rows and stay there until a person confirms them. This is the
   rule the whole design rests on.

6. **A missing input is a refusal, not a zero.** `tax.TaxDataMissing` → HTTP 409.
   A zero looks like an answer, and answers get filed on an *Anlage KAP*.

7. **Money is `Decimal` end to end.** Floats appear at exactly one place: the
   JSON boundary in `api/app.py::_decimal`.

8. **Derived data is never patched, only rebuilt.** Everything below
   `transaction_` is a pure function of it. `pt init-db --reset-derived` exists
   for this. `fund_holding` and `fund_holding_set` are **facts**, not
   derivations — they come from a published file and cannot be recomputed — so
   they are deliberately absent from `DERIVED_TABLES`.

9. **Exposure is an estimate; the return decomposition is an identity.** They
   are not reconciled and must not be. `returns.py` splits a gain using the rate
   the position was actually valued at, and `doctor` checks the parts sum to the
   whole. Look-through says what the portfolio is exposed to *next*. Folding the
   second into the first would make a modelling error indistinguishable from a
   bug (ADR 0005).

---

## 4. Traps this codebase has already fallen into

Written down because each cost real debugging time and none announced itself.

| Trap | What happened |
|---|---|
| **SQLite type affinity** | A column typed `DECIMAL` gets NUMERIC affinity and silently becomes a float. The custom type is `DECIMAL_TEXT` for that reason. Also: `sum()` in SQL over a `DECIMAL_TEXT` column returns a float — aggregate in Python. |
| **`executescript` commits** | It issues an implicit COMMIT, so a transaction wrapper around it is decorative. Migration 0002 destroyed the `instrument` table this way. The runner now splits statements and executes them individually. Related: `PRAGMA foreign_keys` is a no-op inside a transaction. |
| **NULLs are distinct in a UNIQUE constraint** | Migration 0003's uniqueness never fired for proposals with no source transaction, so three modules that documented themselves as idempotent were not. Fixed in 0008 with a COALESCE expression index. |
| **anyio's threadpool does not pin threads** | A sync FastAPI dependency and its endpoint run on different worker threads, so sqlite3's same-thread guard rejected every request. Hence `connect(same_thread=False)`, used only by the server. |
| **Off-grid flows vanish** | Five contributions (€20,400) fell on Sundays and were dropped when mapping to the TARGET2 grid. Reported TWR: **1326%**. Flows are now rolled forward *and* reconciled against the ledger. |
| **A position with no price** | An instrument bought before its first available price made its funding contribution precede the asset existing. Reported TWR: **877%**. Positions are now held at cost when no market price exists. |
| **Partially-sold lots** | Stored the cost of the *remaining* units while quantity was the whole purchase — 36% understated basis on the largest holding. Cost is now per-unit, scaled to purchased quantity. |
| **Quantising too early** | Rounding each day's accrual to the cent before summing loses ~€1.79/year on a €20,000 balance. Accrue at full precision, round when paid. |
| **A rate converted twice** | `41.5` → `0.415` in the parser → `0.00415` in the writer. A 41.5% term deposit accruing like a current account still produces a plausible number. |
| **Ex-date price falls with no offsetting income** | €2,548.76 of distributions were being booked as pure loss, and 2025 tax was reported as €0 where it is €383.43. |

---

## 5. What is deliberately absent

### Known limitations of v1 (in the README, not hidden)

- **XIRR is approximate.** Contributions are inferred from purchases, so their
  dates are when money was *invested*, not when it arrived. Labelled
  `"reliability": "low"`. TWR via unit accounting is unaffected.
- **Currency *return* is quote-currency, and stays that way.** A EUR-quoted S&P
  tracker shows zero FX contribution in `/api/returns` because it was genuinely
  valued in euro. Its dollar risk now appears under exposure instead. The two
  are deliberately not reconciled — invariant 9.
- **Look-through coverage is a maintenance obligation.** Exposure is only as
  complete as the compositions imported, and a composition goes stale with
  nothing to refresh it. `LOOK_THROUGH_MISSING` and `LOOK_THROUGH_STALE` exist
  so the gap is reported rather than discovered.
- **Lira cost basis stays `ESTIMATED`.** The export prints open prices to 3–4
  decimals and there is no lossless route to the true figure.
- **No fee data exists in the source.** Cost basis is understated by every
  purchase fee, so computed taxable gains are biased high. `fees_known = 0`
  makes it visible.
- **TEFAS is an undocumented endpoint** with no SLA. The manual CSV provider is
  the contract, not the contingency.
- ~~**`residual` in the portfolio FX split is non-zero** and exposed as a
  first-class field.~~ **This was never true.** There was no portfolio FX split
  at all: the three `ret_*` columns were never written and the API never
  emitted `linking_method`. ADR 0004 recorded an intention as an outcome and
  this document repeated it. Built in ADR 0006; the residual is now zero
  because Cariño linking is exact, not because a non-zero one was removed.

### Deferred from the original 45-table design (ADR 0003)

**Built since:** ETF look-through (ADR 0005) · Cariño attribution (ADR 0006) ·
what-if scenarios, scoped to tax (ADR 0008) · goal projection, scoped to stated
assumptions and the portfolio's own history (ADR 0009).

**Still absent, and deliberately:** LLM narration · peer scanning · news
sentiment · Monte Carlo over *assumed market parameters* — as distinct from the
resampling of observed returns that ADR 0009 does build. The difference is
where the numbers come from, and it is the whole argument.

---

## 6. Suggested v2 order

Ordered by value per unit of risk, not by appeal.

1. ~~**ETF look-through.**~~ **Done.** See §8 and ADR 0005. The estimate in the
   original list was right about the shape and wrong about the size: the API
   contract did barely change, but the honest part turned out to be the coverage
   reporting rather than the arithmetic.
2. **Broker cash-flow import**, if a source can be found. Would turn inferred
   contributions into recorded ones and make XIRR trustworthy. Highest value
   per line of code — the machinery (inbox, `flow_class`, reconciliation) is
   already built and would simply stop guessing.
3. ~~**Cariño linking**, to remove `residual` from the FX decomposition.~~
   **Done, and the premise was wrong** — there was nothing stored to remove.
   See §9 and ADR 0006. Not purely additive either: the whole period-level
   decomposition had to be built first.
4. ~~**Vorabpauschale automation for the January charge.**~~ **Done**, and
   this entry's premise was accurate. See §10 and ADR 0007. The `NotBookable`
   refusal it describes was correct and is retained for the next kind that
   lacks a rule; `test_api.py` removes a rule to exercise it.
5. ~~**Scenario / what-if**, last.~~ **Done**, and the scoping was the whole
   decision: the *tax* question has an exact answer and the *market* question
   does not. See §11 and ADR 0008.

### If you add a front end

The current page is intentionally plain Python with no build step. Replacing it
is reasonable. Do **not** replace the API's honesty contract while you do:
`as_of`, `data_quality`, `attribution_rule`, `caveats` and the 409-not-zero rule
exist because a number without its provenance is how this tool would start
lying. A prettier dashboard that drops them is a worse tool.

---

## 7. Working on this repo

```bash
uv sync --all-extras --dev
uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest -q
uv run python scripts/check_no_pii.py --all
uv run python scripts/make_example_data.py --check
```

**Versioning.** `pyproject.toml` and `__init__.py` carry the package version;
git tags mark the milestones (`v1`, `v2`). The schema version is separate and
lives in `schema_version` — the two move independently and the schema one is
what `pt doctor` and `/api/health/data` report. Bump the package version when
you close a milestone, and tag it, or `pt --version` stops meaning anything.

**Privacy is the hard constraint.** No trade data and no personal information
beyond the author's name and email may reach GitHub. Never `git add .` or
`git add -A` without checking what it caught. `scripts/check_no_pii.py` runs as
a pre-commit hook *and* as a CI job (the hook is bypassable with `--no-verify`;
CI catches what is already pushed, including files added and later deleted).

**Do not hand-edit `data/example/`.** It is generated, and its BOM, CRLF and
U+00A0 characters are load-bearing test fixtures. `--check` fails on a
single-byte drift, and pre-commit is configured to leave those files alone.

**When you find a bug in a number**, add the identity to `core/doctor.py` as
well as fixing it. The checks there are the ones whose violations do not look
like violations, and every one is tested against a planted failure as well as
against clean data.

---

## 8. If you are picking this up next

v2 closed every item v1 named. There is no queue. What follows is what the work
itself surfaced, in no particular order:

- **Broker cash-flow import** (v1's item 2, declined rather than dropped). Still
  the highest value per line of code if a statement source exists. See §6.
- **Attribution per instrument and per currency.** The daily terms are already
  computed per position in `attribution.daily_terms` and summed before being
  stored; the change is where the aggregation happens, not the arithmetic.
- **A Vorabpauschale reminder.** Nothing schedules `pt vorabpauschale propose`.
  "The 2026 charge has not been proposed and it is February" needs no new data.
- **Issuer mapping for concentration.** Currently groups on identifier where
  files supply one and on the upper-cased name otherwise, so it understates.
  Wait until a real holdings file shows the problem -- nothing in the example
  corpus does.

**And the habit worth keeping.** ADR 0006's Context section exists because a
decision record claimed a feature shipped when it never had, this document
repeated the claim, and it cost a planning cycle. One `SELECT count(...)`
disproved it. Before planning around anything §2 to §11 asserts, query for it.
The two items built after that lesson both started with a verification step,
and one of the two premises was wrong again.
