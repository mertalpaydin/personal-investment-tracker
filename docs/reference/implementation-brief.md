> **Aspirational design brief, largely unimplemented.**
>
> Written before the real source data was examined, and several of its
> assumptions turned out to be wrong -- most importantly that the input is a
> transaction ledger (it is a positions snapshot, see `docs/adr/0001`) and that
> everything is euro-denominated (see `docs/adr/0004`). Its scope is also far
> larger than what is being built; see `docs/adr/0003`.
>
> It is kept because the parts that survived are worth keeping: the German tax
> rules in section 7 are implemented as written, and the reasoning explains why
> several schema decisions are the way they are. Read it as design history, not
> as a description of the code. Implemented scope is in the README roadmap.
>
> Financial targets in the configuration example are **placeholders**, not real.

---

# IMPLEMENTATION BRIEF — Portfolio Tracker + Peer Advisor

**Audience: a coding agent.** Everything needed to start is here. Do not ask the user to re-specify anything in §2 — those decisions are final. If something is genuinely ambiguous, pick the option that keeps the tax engine correct and leave a `# DECISION:` comment.

Companion files: `schema.sql` (authoritative DDL), `portfolio-tracker-spec.md` (rationale, read only if you need the "why").

---

## 1. What this is

A portfolio tracker for a German-resident household under joint assessment, saving toward a medium-term goal. Three jobs:

1. **Track** holdings across depots and deposit accounts, with correct FIFO tax lots and German capital-gains treatment.
2. **Plan** annual use of the €2,000 Sparerpauschbetrag (two people), including the Vorabpauschale that consumes it automatically.
3. **Compare** owned instruments against a curated watchlist of non-owned ETFs and large-cap stocks, on risk-matched historical performance and news sentiment — *never* on forecasts.

Not a trading system. Nothing places orders. Roughly one trade per week, checked daily.

---

## 2. Decisions already made — do not re-litigate

| Topic | Decision |
|---|---|
| Database | SQLite, file at `data/portfolio.db`, schema in `schema.sql` |
| Language | Python 3.11+ |
| Price data | `yfinance` primary, Stooq CSV fallback. End-of-day only. |
| Scheduling | GitHub Actions cron, 16:00 UTC weekdays (after XETRA close) |
| LLM | **Gemini 3.6 Flash**, model id `gemini-3.6-flash`, Google AI Studio **free tier** |
| LLM abstraction | Mandatory adapter interface (§8). No provider SDK calls outside `llm/providers/` |
| ETF eligibility | fund size ≥ **€100,000,000**, TER ≤ 50 bps |
| Stock eligibility | constituent of **S&P 500 or DAX 40**, market cap ≥ **€5,000,000,000** |
| Universe size | 150–300 instruments. Curated, not exhaustive. |
| ETF metadata (TER, AUM, index) | Manual entry via seed YAML. No reliable free API — do not build a scraper. |
| Broker sync | None. CSV import + manual entry only. |
| Frontend | Backend serves JSON at `/api/*`; user supplies their own UI. Build a minimal HTML debug page only. |
| Currency | Store EUR everywhere. `fx_rate` used at transaction time. |
| Timezone | Europe/Berlin for business dates; UTC for timestamps. |

---

## 3. Repo layout

```
portfolio/
  data/portfolio.db
  schema.sql
  config.yaml                  # user config, gitignored (config.example.yaml committed)
  seeds/
    instruments.yaml           # curated universe + manual ETF metadata
    persons.yaml
    accounts.yaml
  src/
    db.py                      # connection, migrations, transaction helper
    config.py                  # pydantic settings loaded from config.yaml + env
    ingest/
      prices.py                # yfinance / stooq fetch -> price, fx_rate
      fundamentals.py          # weekly AUM / market cap snapshot
      news.py                  # RSS + sentiment
      brokers/
        base.py                # CsvImporter ABC
        generic.py             # column-mapping importer driven by YAML
    core/
      lots.py                  # FIFO engine
      valuation.py             # position_daily, portfolio_daily
      returns.py               # TWR, XIRR, vol, drawdown
      tax.py                   # Teilfreistellung, Vorabpauschale, allowance, loss pots
      peers.py                 # risk buckets + peer scan (pure SQL/pandas, no LLM)
      whatif.py                # scenario replay
      policy.py                # IPS rule evaluation
    llm/
      adapter.py               # LLMProvider protocol
      providers/gemini.py
      prompts/*.md
      tasks.py                 # daily_brief, peer_scan, harvest_plan, trade_check
    api/app.py                 # FastAPI
  tests/
    fixtures/                  # hand-computed tax cases — see §9
  jobs/
    daily.py  weekly.py  yearend.py
```

---

## 4. Config (`config.example.yaml` — commit this)

```yaml
household:
  joint_assessment: true          # -> statutory_cap_eur = 2000 combined
goal:
  target_date: "2030-01-01"          # placeholder
  target_amount_eur: 100000         # placeholder
  mortgage_rate_assumption: 0.037
  buffer_bps: 150                 # hurdle = 5.20% gross
  committed_bucket_target_eur: 50000  # placeholder
  monthly_contribution_eur: 1000    # placeholder
screening:
  etf:
    min_fund_size_eur: 100000000
    max_ter_bps: 50
  stock:
    min_market_cap_eur: 5000000000
    required_indices: ["SP500", "DAX40"]
llm:
  provider: gemini
  model: gemini-3.6-flash
  max_requests_per_day: 40        # stay well inside free tier
  redact: true                    # see §8.3
data:
  price_source: yfinance
  fallback: stooq
  fetch_time_utc: "16:00"
```

Secrets in `.env`: `GEMINI_API_KEY`.

---

## 5. Data sources — contracts

**Prices.** `yfinance.download(tickers, start, end, auto_adjust=False)`. Write `close` from `Close` and `total_return_close` from `Adj Close`. Both are required; `close` feeds tax, `total_return_close` feeds performance. Never substitute one for the other.

**Missing days.** XETRA holidays produce gaps. Do not forward-fill in the `price` table. Forward-fill only inside `valuation.py`, max 5 trading days, then raise.

**Fundamentals.** `yfinance.Ticker(x).info` → `marketCap`, `totalAssets`. Unreliable for ETFs; when null, fall back to the value in `seeds/instruments.yaml` and set `source='manual'`. Never fail the job on a null.

**News.** Yahoo Finance RSS per ticker (`https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}`). Store headline, URL, and your own one-line summary. **Do not store full article text.** Sentiment: batch up to 30 headlines into one LLM call returning `[{url, sentiment}]`.

---

## 6. Module contracts

```python
# core/lots.py
def rebuild_lots(conn, account_id: int | None = None) -> None:
    """Idempotent. Deletes and recomputes lot + lot_disposal from transaction_.
    FIFO per (account_id, instrument_id) ordered by trade_date, then transaction_.id.
    Must be safe to run after any import."""

# core/valuation.py
def build_position_daily(conn, start: date, end: date) -> None: ...
def build_portfolio_daily(conn, start: date, end: date) -> None: ...

# core/returns.py
def twr(values: Series, external_flows: Series) -> Series: ...
def xirr(flows: list[tuple[date, float]]) -> float: ...
def annualised_vol(returns: Series, periods: int = 252) -> float: ...
def max_drawdown(values: Series) -> float: ...

# core/tax.py
def compute_disposal_tax(conn, sell_txn_id: int) -> list[DisposalTax]: ...
def forecast_vorabpauschale(conn, tax_year: int, as_of: date) -> list[VapRow]: ...
def allowance_state(conn, person_id: int, tax_year: int) -> AllowanceState: ...
def harvest_plan(conn, person_id: int, tax_year: int,
                 fee_per_trade_eur: float, spread_bps: int) -> HarvestPlan: ...

# core/peers.py
def assign_risk_buckets(conn, as_of: date) -> None: ...
def peer_scan(conn, instrument_id: int, as_of: date, top_n: int = 5) -> list[PeerCandidate]:
    """Pure SQL/pandas. Returns the JSON the LLM will narrate. No LLM inside."""
```

---

## 7. Tax rules — implement exactly

These are the parts most likely to be got wrong. Treat as a specification, not guidance.

### 7.1 FIFO
Per `(account_id, instrument_id)`, oldest `open_date` first, tie-broken by `transaction_.id`. Purchase fees increase cost basis; sale fees reduce proceeds. `TRANSFER_IN` creates a lot using `orig_acquire_date` and `orig_cost_eur`.

### 7.2 Teilfreistellung
Applies to gains **and losses** symmetrically, and to distributions and Vorabpauschale.

```
exempt        = gross_amount * teilfreistellung_pct
taxable       = gross_amount - exempt
```
`teilfreistellung_pct`: 0.30 equity funds (≥51% equity), 0.15 mixed (≥25%), 0.0 individual stocks, bonds, interest.

### 7.3 Disposal
```
gross_gain    = proceeds - cost_basis
gain_after_vap= gross_gain - vap_offset          # vap_offset = lot.vap_accumulated_eur pro-rata to qty sold
                                                  # only reduces a POSITIVE gain toward zero, never below
taxable_gain  = gain_after_vap * (1 - teilfreistellung_pct)
```
Decrement `lot.vap_accumulated_eur` proportionally to the quantity disposed.

### 7.4 Vorabpauschale (§18 InvStG)
Run for every `is_fund = 1` position held on 31 Dec.

```
basisertrag_raw = jan_first_price * qty * basiszins * 0.7
cap             = max(0, (year_end_price - jan_first_price) * qty) + distributions_eur
basisertrag     = min(basisertrag_raw, cap)
basisertrag    *= (12 - full_months_before) / 12    # full months before acquisition month
vap_gross       = max(0, basisertrag - distributions_eur)
taxable_eur     = vap_gross * (1 - teilfreistellung_pct)
```
Zero if the fund did not gain. Basiszins from `tax_constant` (2026: 0.0320). Taxable in the **following** January — book `allowance_entry.entry_date` as 2 January of `tax_year + 1`, and add `vap_gross` to `lot.vap_accumulated_eur` FIFO-weighted across open lots.

Recompute weekly with `status='FORECAST'` from October onward; flip to `ACTUAL` in January.

### 7.5 Loss pots (§20 VI EStG)
- Losses from **individual shares** → `AKTIEN` pot. Offsettable only against gains on individual shares.
- All other losses (funds, bonds) → `SONSTIGE` pot. Offsettable against any other investment income including dividends and interest.
- Order of operations: **offset losses first, then apply the allowance to what remains.**
- Unused pot balances carry forward indefinitely.

### 7.6 Allowance
`statutory_cap_eur` = 2000 if `joint_assessment` else 1000 per person. Consume chronologically by `entry_date` across all four sources. Also warn when the sum of `account.freistellungsauftrag_eur` for a person ≠ the statutory cap, or when one bank's FSA is exhausted while another has unused headroom.

### 7.7 Harvest plan
Given remaining headroom, produce sell-and-immediately-rebuy candidates:
```
benefit = taxable_gain_realised * 0.26375 * (1 + church_tax_rate)
cost    = 2 * fee_per_trade_eur + notional * spread_bps / 10000
recommend if benefit > cost * 1.5
```
Output must show the arithmetic. Label every output: *planning worksheet; the bank's Steuerbescheinigung is authoritative.*

---

## 8. LLM layer

### 8.1 Adapter
```python
class LLMProvider(Protocol):
    def complete(self, *, system: str, user: str,
                 response_schema: dict | None = None,
                 max_output_tokens: int = 2048) -> LLMResponse: ...
```
`GeminiProvider` calls `generateContent` on `gemini-3.6-flash` with `responseMimeType: "application/json"` and `responseSchema` when a schema is supplied. Log every call to `llm_run`, including failures.

### 8.2 Rate limits and failure
Free-tier limits are applied per project and change; do not hardcode a published RPM figure. Instead: a token-bucket limiter defaulting to 5 RPM / `max_requests_per_day`, exponential backoff on HTTP 429 (4 attempts), and **graceful degradation** — if the LLM is unavailable, the API still returns the deterministic JSON with `"narrative": null`. The tool must be fully usable with the LLM switched off.

### 8.3 Redaction (on by default)
Send tickers, percentages, risk buckets, relative weights, allowance headroom in EUR. Do **not** send account numbers, broker names, person names, or absolute portfolio value. The free tier's data handling permits use of submitted content for product improvement, so treat everything sent as public.

### 8.4 Guardrails — put these in every system prompt
```
- You may only use numbers present in the supplied JSON. Never compute new figures.
- Never state or imply what any price will do in future. If asked, decline.
- Never recommend buying or selling. Describe differences; the user decides.
- If a field is null, say the data is missing. Do not estimate it.
- Output must validate against the supplied JSON schema.
```

### 8.5 Tasks
| Task | Input | Output |
|---|---|---|
| `daily_brief` | portfolio_daily delta, positions >2% moves, allowance headroom, breached policy rules | ≤120 words |
| `peer_scan` | `peers.peer_scan()` output + 90-day mean sentiment | one paragraph per candidate |
| `harvest_plan` | `tax.harvest_plan()` output | ordered list with arithmetic restated |
| `trade_check` | proposed trade + active `policy_rule` rows + tax impact | pass/fail per rule + one caveat |

Peer scan target output shape:
> "IWDA sits in the same risk bucket as your VWCE (EQ_DM_BROAD, MID vol). Over 3 years it returned 1.9%/yr more at comparable volatility; TER is 12 bps lower. News sentiment over 90 days is mildly positive (+0.21, 14 items)."

---

## 9. Test fixtures — write these before the tax code

`tests/fixtures/` must contain hand-computed cases; assert to the cent:

1. **FIFO partial sale** — 3 buys, 1 sale spanning 2 lots.
2. **Equity ETF gain** — €1,000 gross gain, 30% Teilfreistellung → €700 taxable.
3. **Equity ETF loss** — €1,000 loss → €700 deductible, into `SONSTIGE`.
4. **Vorabpauschale, fund up** — €50,000 on 1 Jan, Basiszins 3.20%, accumulating, no distributions → Basisertrag €1,120 → taxable €784.
5. **Vorabpauschale, fund down** → 0.
6. **Vorabpauschale, cap binding** — fund up only €300 → Basisertrag capped at €300.
7. **Part-year holding** — bought 10 July → 6 full months before → ×6/12.
8. **VAP offset on sale** — lot with €784 accumulated VAP, later sold at €2,000 gain → gain reduced to €1,216 before Teilfreistellung.
9. **Loss pot segregation** — share loss must NOT offset ETF gain.
10. **Allowance ordering** — dividend in March, VAP in January, gain in December; cap €2,000.
11. **Idempotent import** — same CSV twice → identical row count.
12. **Two persons** — allowances never cross.

---

## 10. Build order and acceptance criteria

| Phase | Deliverable | Done when |
|---|---|---|
| 1 | schema, config, seeds, CSV import, price fetch, FIFO | Computed portfolio value matches the broker statement to the cent on 3 sample dates. Re-import changes nothing. |
| 2 | valuation, returns, `/api/portfolio`, `/api/positions` | TWR and XIRR both present and divergent; contribution/growth split reconciles to total. |
| 3 | tax engine | All 12 fixtures pass. Prior completed tax year reconciles against the real Steuerbescheinigung. |
| 4 | universe, fundamentals, risk buckets, peer scan, news | `peer_scan()` returns sensible JSON with no LLM involved. |
| 5 | Gemini adapter, 4 tasks, guardrails, rate limiting | Every task degrades cleanly with the API key removed. |
| 6 | what-if replay, per-trade scorecard | Replaying the actual transaction set reproduces `portfolio_daily` exactly. |

Do not start a phase before its predecessor's criterion passes. Phase 3 depends entirely on Phase 1 being exact.

---

## 11. Explicit non-goals

Order placement. Real-time quotes. Bank/PSD2 sync. Scraping justETF or index-provider data. Price prediction of any kind. Multi-user auth. Anything that presents output as tax or investment advice.

---
---

# ADDENDUM A — Manual entry, proposed entries, ETF look-through

Schema for all of this is in `schema.sql` under "ADDENDUM A". Read §7 (tax rules) first; nothing here may bypass it.

## A1. Manual trade entry

After the initial CSV import, trades are entered through the UI.

**Endpoints**
```
POST   /api/transactions          -> create, source='MANUAL', confidence='ACTUAL'
PATCH  /api/transactions/{id}     -> edit; forbidden if the tax year is closed (see below)
DELETE /api/transactions/{id}     -> soft delete
POST   /api/transactions/validate -> dry run: returns FIFO + tax impact WITHOUT writing
```

`validate` is the important one. Before the user saves a sale, show which lots FIFO will consume, the resulting taxable gain after Teilfreistellung and VAP offset, remaining allowance headroom, and any `policy_rule` breach. This is the moment where the tool earns its keep.

**After any write:** call `rebuild_lots()` for the affected `(account_id, instrument_id)`, then rebuild `position_daily` / `portfolio_daily` from the earliest affected date forward. Never patch lots incrementally.

**Closed tax years.** Once a year is reconciled against the Steuerbescheinigung, set a `closed_through` date in config. Edits to transactions before it require an explicit `force=true` and log a warning — otherwise a stray edit silently rewrites a year you already filed.

**Duplicate detection.** A manually entered trade will often reappear in a later broker CSV with an `external_id`. On import, before inserting, fuzzy-match against `source='MANUAL'` rows:

```
same account_id AND same instrument_id
AND abs(trade_date_delta) <= 3 days
AND abs(qty_delta) / qty < 0.001
AND abs(price_delta) / price < 0.005
```
Match → write a `duplicate_candidate` row, insert the imported row, and surface a merge prompt. Merging keeps the imported row (it has the authoritative `external_id` and fees) and soft-deletes the manual one. Never auto-delete.

## A2. Proposed entries — dividends, interest, Vorabpauschale

**Hard rule: nothing auto-generated writes to `transaction_`.** Generators write `pending_entry` rows; only a user CONFIRM or EDIT creates a transaction, with `source='AUTO_CONFIRMED'`.

### A2.1 Dividends

Daily job:
1. Refresh `dividend_event` from `yfinance.Ticker(sym).dividends` for every instrument with an open position.
2. For each event where `ex_date` ≥ position open date and no transaction exists, create a `pending_entry`:

```
qty            = quantity held on ex_date (from position_daily)
gross_local    = amount_per_share * qty
gross_eur      = gross_local * fx_rate(pay_date or ex_date)
withholding    = gross_eur * treaty_rate(domicile)     # 0.15 US, 0.0 IE-domiciled ETF, etc.
proposed_net   = gross_eur - withholding               # BEFORE German KESt
basis          = "0.42 USD/share x 120 shares @ 1.08 EURUSD, less 15% US WHT"
```

3. Surface it on the pay date, or ex-date + 14 days if the pay date is unknown.

`confidence='ESTIMATE'` on the created transaction unless the user edits the amount, in which case `'ACTUAL'`. Withholding treatment varies by fund domicile and broker; the estimate exists to prompt the user, not to be trusted. Show the derivation in the UI so a wrong number is obvious.

Only `distribution_policy='DIST'` instruments and individual stocks generate dividend proposals. Accumulating ETFs never do — they generate Vorabpauschale instead.

### A2.2 Interest

For `kind IN ('TAGESGELD','FESTGELD')`:

```
rate      = account.interest_rate
            OR inferred: last_interest_txn.gross / avg_balance_over_last_period * periods_per_year
days      = day count since interest_last_paid, per account.interest_day_count
gross_eur = average_balance * rate * days / 365
```

Use average balance across the period, not closing balance — deposits mid-period otherwise overstate it. Festgeld with `AT_MATURITY` frequency: propose once, on `maturity_date`.

Propose on the expected payment date derived from `interest_frequency` and `interest_last_paid`.

**Rate-drift detection:** when a confirmed interest amount differs from the proposal by more than 10%, back out the implied rate, update `account.interest_rate`, and tell the user the rate appears to have changed. Tagesgeld rates move; this keeps the model self-correcting without nagging.

### A2.3 Vorabpauschale

From October, the weekly forecast writes `pending_entry` rows with `kind='VORABPAUSCHALE'` and `proposed_date` = 2 January next year, so the December harvest planner sees the allowance it will consume. These are informational until the bank actually books them in January.

### A2.4 Inbox API
```
GET   /api/pending?status=PROPOSED
POST  /api/pending/{id}/confirm          -> creates transaction as proposed
POST  /api/pending/{id}/confirm-edited   -> body carries corrected amounts
POST  /api/pending/{id}/reject           -> body carries a reason
```
Include an open-proposal count in `daily_brief` input so the LLM can mention it. Never let the LLM confirm anything.

## A3. ETF look-through exposure

**Goal:** total economic exposure to a company = directly held shares + the share of it inside every ETF held.

### A3.1 Acquiring holdings

One downloader per issuer under `src/ingest/etf_holdings/`, each implementing:

```python
class HoldingsProvider(Protocol):
    provider: str
    def supports(self, instrument: Instrument) -> bool: ...
    def fetch(self, instrument: Instrument) -> HoldingsFile: ...   # raw rows + stated as_of
```

Implement `ishares` and `xtrackers` first (they cover most XETRA volume), plus a `manual` provider that reads a user-dropped CSV from `data/holdings_manual/`. The manual provider is the permanent fallback and must always work — issuer URLs and CSV layouts change without notice, and a broken downloader must degrade to "upload the file yourself", never to a crash.

Refresh **monthly**, not daily. Index constituents drift slowly and daily downloads add fragility for no signal. Mark a snapshot `STALE` past 45 days.

Set `is_synthetic=1` for swap-based ETFs and **exclude them from look-through entirely** — their published basket is collateral, not exposure. Report them as an unattributed residual rather than pretending.

### A3.2 Entity resolution — the actual work

For each raw holding row, in order:
1. `raw_isin` exact match on `issuer_entity.isin`.
2. `security_alias` exact match on `(raw_name, provider)`.
3. Normalised-name match: uppercase, strip punctuation, strip corporate suffixes (`CORP`, `CORPORATION`, `INC`, `PLC`, `AG`, `NV`, `SA`, `LTD`, `CLASS A/B/C`), collapse whitespace. Require exact match on the normalised form.
4. Unresolved → leave `issuer_entity_id` NULL and queue for user review, **but only if `weight >= 0.001`** (0.10%).

The 0.10% floor is deliberate. An MSCI World file has ~1,400 rows; the top ~150 carry the concentration signal. Resolving the tail is weeks of work for a rounding error. Track `coverage_pct` per snapshot and warn below 90%.

Every user-confirmed resolution writes a `security_alias` row, so the manual work is done once per name per provider and then never again.

Link `issuer_entity.instrument_id` when the company is also in your tradeable universe — that's what merges the direct and indirect legs.

### A3.3 Aggregation

Nightly, after `position_daily`:

```
for each ETF position P (excluding synthetic):
    S = latest OK snapshot for P.instrument_id
    for each holding H in S where issuer_entity_id is not null:
        indirect[H.issuer_entity_id] += P.market_value_eur * H.weight

for each stock position P:
    direct[entity_of(P.instrument_id)] += P.market_value_eur

total = direct + indirect
pct   = total / portfolio_value
```

Write `lookthrough_exposure` per person and combined. Roll the same figures up by sector, country, and currency into `lookthrough_sector_exposure`.

Carry `oldest_snapshot_date` through to the API. The aggregate blends snapshots of different dates and the UI must say so.

### A3.4 Endpoints
```
GET /api/exposure?person_id=&top=25       -> ranked entities, direct vs indirect split
GET /api/exposure/{entity_id}             -> which ETFs contribute, at what weight
GET /api/exposure/dimensions?dim=SECTOR   -> sector / country / currency rollup
GET /api/exposure/unresolved              -> holdings >=0.10% awaiting mapping
```

### A3.5 New policy rule

```
rule_type: MAX_LOOKTHROUGH_SINGLE_NAME_PCT
params:    {"limit_pct": 8.0}
```
Evaluate in `policy.py` against `lookthrough_exposure`, not against `position_daily`. Feed breaches into `daily_brief` and `trade_check`. This rule is the main reason the feature is worth building: direct positions understate real concentration, often severely.

Also extend `peer_scan` output with an `overlap_pct` field — the weight-overlap between a candidate ETF and current holdings — so "similar risk, better 3-year return" doesn't quietly mean "the same 40 companies again".

## A4. Additions to the build order

| Phase | Deliverable | Done when |
|---|---|---|
| 2b | Manual trade entry + `/validate` dry run + duplicate detection | Manual entry then CSV re-import of the same trade produces one transaction and one resolved `duplicate_candidate`. |
| 3b | Dividend + interest proposal generators, inbox API | A known historical dividend is proposed within 2% of the amount the broker actually credited. Rejected proposals never regenerate. |
| 7 | ETF holdings ingestion, entity resolution, alias UI | Two issuer providers plus manual upload working; `coverage_pct` ≥ 90% on every held ETF. |
| 8 | Exposure aggregation, endpoints, concentration policy rule | Look-through exposure to a company held both directly and inside an ETF equals the hand-computed figure. |

Phase 7–8 come last. They are the most fragile and the least load-bearing; the tracker and tax engine must be correct without them.

## A5. Additional non-goals

Real-time holdings. Look-through into fund-of-funds or nested ETFs (flag and skip). Reconstructing swap-ETF exposure. Auto-confirming any proposal without user action.

---
---

# ADDENDUM B — Unit accounting, return attribution, look-ahead

Schema in `schema.sql` under "ADDENDUM B". This supersedes the naive TWR mention in §6 `core/returns.py`.

## B1. Unit accounting — implement this before anything else in returns

Model the portfolio as a fund. This makes contributions and withdrawals structurally invisible to the return calculation, which is the whole point.

**Daily, in order:**

```python
V_prev   = portfolio_unit[t-1].market_value_eur
U_prev   = portfolio_unit[t-1].units_outstanding
F        = external_net_flow(t)        # DEPOSIT/WITHDRAWAL/TRANSFER only
V_t      = market_value(t)             # priced at close, BEFORE issuing units

# 1. return earned on yesterday's capital, flow-neutral
price_pre = (V_t - F) / U_prev
# 2. flow transacts at that price
units_issued = F / price_pre
U_t          = U_prev + units_issued
price_t      = V_t / U_t               # == price_pre by construction
```

**Rules that must hold:**
- `F` counts **external** flows only. A buy, sale, dividend, interest payment, fee, or tax is internal — it must never issue or redeem units, or you will erase the very returns you are trying to measure.
- Inception: `unit_price = 100`, `units_outstanding = initial_value / 100`.
- Flows are applied at the close price of the flow date. Same-day deposit-and-buy is therefore neutral, which is correct.
- Assert `abs(V_t - U_t * price_t) < 0.01` every day. A break means a flow was misclassified.

**Derived measures:**
```
TWR(a,b)          = price_b / price_a - 1
TWR annualised    = (price_b / price_a) ** (365 / days) - 1
XIRR              = irr of external flows + terminal value
timing_gap        = XIRR - TWR        # >0 means contribution timing helped
```

Report calendar-year TWR per year, and since-inception annualised. `pnl_bridge` gives the same story in EUR: opening → contributions → income → realized → unrealized → fees → taxes → closing. The bridge must reconcile to the cent; make that a test.

## B2. Return attribution — "what my holdings generated, by weight"

Daily contribution of holding *i*: `c_i,t = w_i,t-1 * r_i,t`, where `w` is start-of-day weight and `r_i,t` is the holding's total return (use `price.total_return_close`).

Naively summing daily contributions across a period does not equal the compounded portfolio return. Use **Cariño linking** so the parts sum exactly to the whole:

```python
k_t = log(1 + r_p_t) / r_p_t   if r_p_t != 0 else 1.0
K   = log(1 + R_p)  / R_p      if R_p  != 0 else 1.0     # R_p = period portfolio return
contribution_i = sum((k_t / K) * c_i_t for t in period)
# invariant:
assert abs(sum(contribution_i for i in holdings) - R_p) < 1e-6
```

Assert that invariant in tests. If it fails, weights are being computed end-of-day instead of start-of-day.

Write one `return_attribution` row per instrument per period, for calendar years, YTD, and rolling 12m. Include closed positions — a holding sold in March still contributed to the year.

**Cash drag** is a legitimate line: treat uninvested cash as an instrument with ~0 return. Users are consistently surprised by its cost, and hiding it makes the attribution not sum.

**API**
```
GET /api/returns?person_id=&period=2026        -> twr, xirr, timing_gap, pnl_bridge
GET /api/returns/attribution?period=2026       -> ranked contributions, sums to twr
GET /api/returns/annual                        -> per-calendar-year table
```

The headline UI number should be TWR, with XIRR and `timing_gap` beside it. Total balance growth is a separate figure and must never be labelled a return.

## B3. Look-ahead: income (real forecasting)

Job `jobs/forecast_income.py`, weekly, horizon 24 months. Writes `cashflow_forecast`.

### B3.1 Dividends
| Confidence | When | Method |
|---|---|---|
| `DECLARED` | Issuer has announced ex-date and amount | Use it directly |
| `HIGH` | ≥3 years of regular payments, stable schedule | Trailing-12m DPS × `dividend_growth_pct`, placed on the historical month pattern |
| `MEDIUM` | Irregular or <3 years history | TTM DPS, no growth |
| `LOW` | Cut or suspended in last 24m | Include at 50%, flag |

```
gross_eur = projected_dps * qty_projected * fx_forward(=spot)
qty_projected = current holding; do NOT project future purchases into dividend estimates
```
Then apply Teilfreistellung and write `allowance_use_eur`. Distributing ETFs and stocks only.

Never project dividend *growth* above the assumption in `forecast_assumption`; default 0% and let the user raise it. An optimistic growth rate compounds quietly into a wrong allowance plan.

### B3.2 Interest
- **Festgeld**: deterministic. `balance × rate × term`, dated at `maturity_date` or the contractual schedule. Confidence `DECLARED`.
- **Tagesgeld**: `projected_balance × forecast_assumption['tagesgeld_rate'] × days/365`, on the known payment schedule. Confidence `MEDIUM` — the rate is variable. Projected balance must include scheduled contributions.
- Emit a `MATURITY` row for every Festgeld so reinvestment decisions surface before the money silently rolls.

### B3.3 Vorabpauschale
Current year: as §7.4, from actual January price. Next year: unknown Basiszins — use `forecast_assumption['basiszins_next']` (default: carry current), confidence `LOW`, and show a ±0.5pp sensitivity band. Note it is booked the following January.

### B3.4 The payoff — forward allowance view
```
GET /api/allowance/forecast?person_id=&months=24
```
Returns, per tax year: allowance cap, committed (actual + declared), projected (forecast income), and **remaining headroom for deliberate gain realisation**. This is the number that should be visible on the dashboard year-round, not just in December. Feed it to `harvest_plan` so the planner sizes sales against *projected* headroom rather than today's.

## B4. Look-ahead: prices (distribution only — no point forecasts)

`core/projection.py`. **There must be no code path that outputs a single predicted price or value for a future date.**

### B4.1 Method
Block bootstrap, default 5,000 paths:
1. Sample window: ≥10 years of daily total returns per instrument where available, else the longest common window.
2. Draw contiguous **blocks of 20 trading days** — preserves volatility clustering and cross-asset correlation, which IID sampling destroys.
3. Sample blocks jointly across instruments by date index, so correlations are retained without estimating a covariance matrix.
4. Apply portfolio weights, scheduled contributions, fee drag, and the glidepath from `goal`.
5. Apply tax drag on projected income above the allowance.
6. Write `projection_percentile` per month and `projection_outcome`.

Deterministic mode (`method='DETERMINISTIC'`) also allowed: fixed x% p.a., clearly labelled a scenario.

### B4.2 Outcome metrics — these are the deliverable, not the paths
```
prob_target_met        P(value >= goal.target_amount_eur at target_date)
prob_below_committed   P(value < goal.committed_bucket_target_eur at target_date)
shortfall_p10_eur      target - p10 value
months_delay_p10       months of extra saving needed in the p10 case
```

### B4.3 Presentation rules (enforce in the API, not just the UI)
- Return percentile bands. **Do not emit p50 as a headline field** — it gets read as a prediction. It is available inside `projection_percentile` for charting the fan, and the API's summary object exposes only the outcome metrics above.
- Always return `assumptions` and `history_start` alongside results.
- Attach this disclosure string to every projection response, verbatim:
  > Based on resampled historical returns. Historical samples understate tail risk and are drawn from a period that may not repeat. This is a range of outcomes, not a forecast.
- The LLM guardrails (§8.4) still apply: the model may narrate `prob_target_met` but may not extrapolate, revise, or comment on likelihood beyond the supplied numbers.

### B4.4 Honest limits, state them in the UI
A 4-year horizon is short enough that the outcome is dominated by sequence risk, which no amount of sampling resolves. The useful output is not "you will have €X" but "in the worst tenth of outcomes you are €Y short, which is Z months of extra saving." Design the screen around that sentence.

## B5. Build order additions

| Phase | Deliverable | Done when |
|---|---|---|
| 2c | Unit accounting, TWR/XIRR, pnl_bridge | Two portfolios with identical holdings but different contribution timing show identical TWR and different XIRR. Bridge reconciles to the cent. |
| 2d | Return attribution | Sum of contributions equals portfolio TWR to 1e-6, including closed positions and cash drag. |
| 9 | Income look-ahead + forward allowance API | Backtest: forecast run as of Jan reproduces the year's actual dividends within 10%. |
| 10 | Monte Carlo projection | No API response contains a single-value future estimate. Disclosure string present on every projection response. |

Phase 2c/2d belong with Phase 2 — unit accounting is foundational and retrofitting it means recomputing all history. Phases 9–10 come after the tax engine.

---
---

# ADDENDUM C — Savings tracking and projection scenarios

Schema under "ADDENDUM C". Depends on Addendum B unit accounting.

## C1. What counts as savings

Define the **perimeter** as all accounts with `in_perimeter = 1`. New savings is money crossing into that perimeter from outside. Everything else is internal reshuffling and must not be counted.

| Situation | `flow_class` | New savings? |
|---|---|---|
| Salary account → depot | `EXTERNAL_IN` | **Yes** |
| Bonus, gift, tax refund → any perimeter account | `EXTERNAL_IN` | **Yes** |
| Tagesgeld → depot (both in perimeter) | `INTERNAL_TRANSFER` | No |
| Sell ETF A, buy ETF B | `INVESTMENT` | No |
| Sell to raise cash, cash sits in depot | `INVESTMENT` | No |
| Dividend or interest received | `INCOME` | No — this is a return |
| Dividend reinvested | `INVESTMENT` | No |
| Withdrawal to spend | `EXTERNAL_OUT` | Negative |
| Broker fee, tax payment | `COST` | No |

**Reinvested income is not savings.** It is return being compounded. Counting it as contribution simultaneously understates your return and overstates your discipline. This is the single most important rule in this addendum.

### C1.1 Classifier

Run on import and on manual entry:

```python
if type in ('BUY','SELL','SPLIT'):                    -> INVESTMENT
elif type in ('DIVIDEND','INTEREST'):                 -> INCOME
elif type in ('FEE','TAX_WITHHELD'):                  -> COST
elif type in ('DEPOSIT','TRANSFER_IN'):               -> EXTERNAL_IN   (provisional)
elif type in ('WITHDRAWAL','TRANSFER_OUT'):           -> EXTERNAL_OUT  (provisional)
```
Then reclassify provisional rows by transfer pairing (§C1.2). Set `is_new_savings = (flow_class == 'EXTERNAL_IN')`.

### C1.2 Internal transfer pairing

An `EXTERNAL_OUT` in one perimeter account and an `EXTERNAL_IN` in another are the same money moving. Match on:
```
same household (person or spouse)
AND abs(date_delta) <= 3 days
AND abs(amount_delta) < 0.01
AND opposite direction
```
On match: write `internal_transfer_link`, set both `flow_class = 'INTERNAL_TRANSFER'`, both `is_new_savings = 0`. Unit accounting must then treat both as internal — net household external flow is zero, which is the correct answer.

Ambiguous cases (two identical amounts on the same day) → present both to the user rather than guessing.

### C1.3 User override — "mark as new savings"

```
PATCH /api/transactions/{id}/flow-class   {"flow_class": "EXTERNAL_IN", "savings_label": "bonus"}
```
User classification always wins and is sticky through re-imports (keyed on `external_id`). Surface an inbox of unclassified or provisional flows so nothing sits miscounted. After any change, recompute `portfolio_unit` from that date forward — flow classification directly changes TWR.

## C2. Savings metrics

`savings_period` rebuilt nightly. Expose:

```
GET /api/savings?person_id=&from=&to=&granularity=month|year
GET /api/savings/summary
```

Summary should carry: net new money this month / YTD / trailing 12m, cumulative contributions since inception, average monthly contribution over trailing 12m, and the split of current value into contributions vs. income vs. market growth (from `pnl_bridge`). `net_income_eur` is optional user input; when absent, omit `savings_rate` rather than inventing a denominator.

**Feed the projection from this.** The default `monthly_contribution` for any projection is trailing-12m actual net new money, not the number in `config.yaml`. Show the derived figure and let the user override. A projection built on an aspirational savings rate is the most common way these tools flatter their owner.

## C3. Scenarios — the what-if layer

A scenario is the baseline projection re-run with levers applied. Same block-bootstrap engine, same seed (so differences are attributable to the lever, not sampling noise — **reuse the seed, this is mandatory**).

### C3.1 Lever schema

```json
{
  "monthly_contribution_delta_eur": 300,
  "contribution_growth_pct_annual": 2.0,
  "lump_sums": [{"date": "2027-01-15", "amount_eur": 5000}],
  "return_delta_annual_pct": 1.0,
  "return_delta_monthly_pct": null,
  "couple_vol_to_return": true,
  "ter_delta_bps": -10,
  "equity_allocation_pct": null,
  "target_amount_eur": null,
  "target_date": null
}
```

Implementation notes:
- `return_delta_*` is applied as additive drift on sampled daily returns (`delta_annual / 252`), preserving the bootstrap's volatility and correlation structure.
- Accept monthly or annual, store both, and **always display the annualised equivalent**: `(1 + m)^12 - 1`. A 1%/month input is +12.68%/yr — the UI must show that before the user runs it.
- `couple_vol_to_return = true` (default) scales sampled volatility by `(1 + return_delta / baseline_return)`. Higher expected return without higher volatility is not a thing that exists; make the honest version the default and require an explicit opt-out.
- `equity_allocation_pct` re-weights the bootstrap across asset buckets and interacts with the `goal` glidepath.

### C3.2 The "5 months earlier" metric

```python
def months_to_target(paths, target, conf):
    """Earliest month m where P(value_m >= target) >= conf. None if never within horizon."""

months_earlier_75 = months_to_target(baseline, T, 0.75) - months_to_target(scenario, T, 0.75)
```

Compute at conf = 0.50, 0.75, 0.90. **Default the UI to 0.75, not 0.50.** A 50% confidence date is a coin flip, and it is the wrong headline for a house purchase. If a scenario never reaches the target within the horizon, return `null` and say so — never extrapolate past the horizon.

Also compute:
```
efficiency_months_per_100 = months_earlier_75 / (monthly_contribution_delta_eur / 100)
```
Ranking levers by months bought per €100/month is the most decision-useful output in the whole tool.

### C3.3 Separate what you control from what you hope for

Group levers in the API response and the UI:

- **Controllable**: contribution delta, lump sums, contribution growth, TER reduction, target date, target amount.
- **Assumed**: return delta, equity allocation, volatility.

Return-lever scenarios must carry a flag `"lever_type": "assumption"` and this string:
> Changing the assumed return does not change the portfolio. This shows what would follow if returns were different, not a way to make them so.

Sort controllable levers above assumed ones. The framing matters: a user who sees "+1%/month gets you there 14 months early" next to "+€300/month gets you there 5 months early" will draw exactly the wrong conclusion about which to act on.

### C3.4 API

```
POST /api/projection/scenario        {levers, name?, pin?}  -> outcomes + deltas vs baseline
GET  /api/projection/scenarios       -> pinned scenarios, re-run against current baseline
GET  /api/projection/levers/sensitivity  -> each lever swept over a range, months_earlier per step
DELETE /api/projection/scenarios/{id}
```

Pinned scenarios re-run nightly so their answers stay current as the portfolio moves.

### C3.5 LLM task `scenario_narrative`
Input: baseline outcomes, scenario outcomes, lever set, `lever_type`. Output: ≤80 words. §8.4 guardrails apply in full — the model may restate `months_earlier_75` and `prob_target_met`, may not speculate about whether a return assumption is achievable, and may not recommend a course of action.

## C4. Build order additions

| Phase | Deliverable | Done when |
|---|---|---|
| 2e | Flow classification, transfer pairing, override API | Tagesgeld→depot transfer nets to zero new savings; sell-and-rebuy produces zero; reinvested dividend produces zero. |
| 2f | `savings_period`, savings API | Cumulative net new money + income + market growth reconciles to current value to the cent. |
| 11 | Scenario engine, sensitivity sweep, pinning | Baseline re-run with an empty lever set reproduces the baseline exactly (seed reuse verified). Monthly return input displays its annualised equivalent. |

Phase 2e must land before any historical TWR is trusted — a misclassified transfer corrupts the return series silently.
