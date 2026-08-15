# v1 → v2 handover

**Status: v1 complete.** 529 tests, `ruff` + `mypy --strict` + PII guard clean,
`pt doctor` reporting 0 errors across 4,610 rows of the author's real data and
4,496 rows of the synthetic corpus.

This document is for whoever builds v2. It says what exists, what it rests on,
what is deliberately absent, and which mistakes have already been made here so
they are not made again.

---

## 1. Read these first, in this order

| | |
|---|---|
| [`README.md`](../README.md) | What the project is and why. Its *Known gaps* section is honest and current. |
| [`docs/adr/`](adr/) | Four decisions that reshaped the inherited design, each written from measurements against the real export. **0001 is the one that matters most.** |
| [`docs/reference/`](reference/) | The pre-existing AI-written brief and spec, sanitised, with an honesty banner. Design *history*, not a description of the code. Several of its assumptions were proved wrong. |
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
| `core/returns.py` | Unit accounting, TWR/XIRR, the local-vs-FX decomposition. |
| `core/accounts.py` | Deposit day-counts, daily accrual, maturity projection. |
| `core/interest.py` | Accrued-but-unpaid interest → review inbox. |
| `core/tax.py` | German engine. Contains **zero** FX arithmetic and **zero** currency comparisons. |
| `core/inbox.py` | What confirming an inference *means*, for every front end. |
| `core/rebuild.py` | Recompute the derived layer. Called by CLI and API alike. |
| `core/doctor.py` | Identity checks. |
| `core/goal.py` | Target and projection. |
| `api/app.py`, `api/render.py` | 20 endpoints; one page, plain Python, no template engine. |

### Migrations (schema version 8)

`0001_core` (17 tables) · `0002_fintables_price_source` · `0003_cash_flows` ·
`0004_portfolio_cash` · `0005_total_excludes_cash` · `0006_settings` ·
`0007_cash_provenance` · `0008_pending_dedup`

The runner pins a SHA-256 of every applied file and **refuses to run if an
applied migration's bytes changed**. Never edit an applied migration; add a new
one.

### CLI

`init-db` · `import` · `seed` · `fetch-fx` · `fetch-prices` · `rebuild` ·
`report` · `tax` · `doctor` · `serve` · `flows {list,propose,confirm,reject,sale}` ·
`accounts {list,add,set,import,accrue,propose-interest}`

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
   for this.

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
- **Currency exposure is quote-currency, not economic.** A EUR-quoted S&P
  tracker reports zero FX contribution while carrying full dollar risk.
  Mechanically correct, economically misleading. Only look-through fixes it.
- **Lira cost basis stays `ESTIMATED`.** The export prints open prices to 3–4
  decimals and there is no lossless route to the true figure.
- **No fee data exists in the source.** Cost basis is understated by every
  purchase fee, so computed taxable gains are biased high. `fees_known = 0`
  makes it visible.
- **TEFAS is an undocumented endpoint** with no SLA. The manual CSV provider is
  the contract, not the contingency.
- **`residual` in the portfolio FX split is non-zero** and exposed as a
  first-class field. Cariño linking would remove it.

### Deferred from the original 45-table design (ADR 0003)

ETF look-through · Cariño attribution · Monte Carlo projection · LLM narration ·
what-if scenarios · peer scanning · news sentiment. Roughly 29 tables.

---

## 6. Suggested v2 order

Ordered by value per unit of risk, not by appeal.

1. **ETF look-through.** Unlocks the largest honest gap: real currency and
   sector exposure. Needs a holdings source per fund and a `look_through`
   table. Everything downstream (`/api/allocation`) already labels its basis,
   so the API contract barely changes.
2. **Broker cash-flow import**, if a source can be found. Would turn inferred
   contributions into recorded ones and make XIRR trustworthy. Highest value
   per line of code — the machinery (inbox, `flow_class`, reconciliation) is
   already built and would simply stop guessing.
3. **Cariño linking**, to remove `residual` from the FX decomposition. Purely
   additive; the residual is already stored, so the test is that it goes to
   zero.
4. **Vorabpauschale automation for the January charge** — `pending_entry`
   already has the `VORABPAUSCHALE` kind, and `core/inbox.py` will refuse to
   book it until a rule exists. That refusal is deliberate: `NotBookable` is
   better than reporting success and recording nothing.
5. **Scenario / what-if**, last. It is the most fun and the least load-bearing.

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
