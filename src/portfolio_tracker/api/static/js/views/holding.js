/*
 * One holding, over its whole life.
 *
 * This page exists because the tracker could not answer "how is this
 * particular investment doing over time" at all -- the data was in the
 * database and in the API, and nothing drew it.
 *
 * Four charts, in the order the questions occur:
 *
 *  1. Value against cost. Is this worth more than was paid for it, and when
 *     did that change? Buy and sell markers show why the cost line steps.
 *  2. Against the portfolio. Both indexed to 100 at the start of the window,
 *     because "did this beat the rest of what I own" is the question a single
 *     price line cannot answer.
 *  3. Price against currency, for foreign holdings. The two forces that move
 *     the euro value, drawn separately, so it is obvious which one did it.
 *  4. Where the gain came from: price versus currency, as a running split.
 *
 * A sold holding gets the same page. The API reports `closed: true` and the
 * full history rather than a 404, because the performance of something you no
 * longer own is a completed fact, not a missing one.
 */

import { el, card, table, row, heading } from "../dom.js";
import { get, all, isError } from "../api.js";
import * as f from "../fmt.js";
import { timeChart, toTs, palette, indexTo100 } from "../charts.js";
import { divergingBars } from "../bars.js";

const RANGES = [
  { key: "1W", label: "1 week", days: 7 },
  { key: "1M", label: "1 month", days: 31 },
  { key: "3M", label: "3 months", days: 92 },
  { key: "6M", label: "6 months", days: 183 },
  { key: "YTD", label: "This year", days: null, ytd: true },
  { key: "1Y", label: "1 year", days: 366 },
  { key: "ALL", label: "All", days: null },
];

let range = "1M";

export async function render({ params }) {
  const key = params.key;
  const data = await all({
    holding: get(`/api/positions/${encodeURIComponent(key)}`),
    portfolio: get("/api/returns/series"),
  });

  if (isError(data.holding)) throw data.holding;
  const h = data.holding;

  const view = el("div");
  view.append(header(h));

  if (!h.history.length) {
    view.append(
      el(
        "div.empty",
        el("p", { text: "There is no valuation history for this instrument yet." }),
        el("p", { text: "It has no lots, so nothing has been valued." }),
      ),
    );
    view.append(transactionsTable(h));
    return view;
  }

  const window = sliceToRange(h.history);
  view.append(rangePicker(window));

  const chartPair1 = el(
    "div.holding-chart-layout",
    returnVsPortfolio(h, window, isError(data.portfolio) ? null : data.portfolio),
    statsVsPortfolio(h, window, isError(data.portfolio) ? null : data.portfolio),
  );
  view.append(chartPair1);

  if (h.currency !== "EUR") {
    const chartPair2 = el(
      "div.chart-pair",
      priceAndFx(h, window),
      currencySplit(h, window),
    );
    view.append(chartPair2);
  }

  view.append(dividendsSection(h));
  view.append(lotsTable(h));
  view.append(disposalsTable(h));
  view.append(transactionsTable(h));
  view.append(taxPanel(h));
  view.append(el("p.note", { text: h.performance_basis }));

  return view;
}

/* -------------------------------------------------------------- header */

function header(h) {
  const box = el("div");
  box.append(
    el("p.note", el("a", { href: "#/holdings", text: "← All holdings" })),
    el("h1", { text: h.name || h.instrument }),
  );

  const bits = [h.instrument, h.asset_class, h.currency].filter(Boolean).join(" · ");
  box.append(
    el(
      "p.sub",
      el("span", { text: bits }),
      h.closed
        ? el("span.tag", { text: `sold — last held ${f.date(h.last_held)}` })
        : null,
      h.valuation_status && h.valuation_status !== "OK"
        ? el("span.tag.warn", { text: h.valuation_status.toLowerCase() })
        : null,
    ),
  );

  const gainPct =
    f.has(h.cost_basis_eur) && f.num(h.cost_basis_eur) !== 0
      ? f.num(h.unrealized_eur ?? h.return_decomposition?.true_eur_gain) / f.num(h.cost_basis_eur)
      : null;

  const cards = h.closed
    ? el(
        "div.cards",
        card("Realised gain", f.eur(h.realised_gain_eur, { sign: true }), {
          cls: "total",
          valueCls: f.tone(h.realised_gain_eur),
        }),
        card("Income received", f.eur(h.income_eur)),
        card("Held from", f.date(h.first_held)),
        card("Held until", f.date(h.last_held)),
      )
    : el(
        "div.cards",
        card("Value", f.eur(h.market_value_eur), { cls: "total" }),
        card("Units", f.qty(h.quantity)),
        card("Cost", f.eur(h.cost_basis_eur)),
        card("Unrealised", f.eur(h.return_decomposition?.true_eur_gain, { sign: true }), {
          valueCls: f.tone(h.return_decomposition?.true_eur_gain),
          sub: gainPct === null ? null : f.pct(gainPct, { sign: true }),
        }),
        card("Realised so far", f.eur(h.realised_gain_eur, { sign: true }), {
          valueCls: f.tone(h.realised_gain_eur),
        }),
        card("Income received", f.eur(h.income_eur)),
      );

  box.append(cards);

  const note = h.return_decomposition?.vs_broker_export;
  if (note) box.append(el("div.panel", el("p", { text: note })));

  return box;
}

/* --------------------------------------------------------------- range */

function rangePicker(window) {
  const box = el("div.ranges");
  for (const option of RANGES) {
    box.append(
      el("button", {
        text: option.label,
        "aria-pressed": String(range === option.key),
        onclick: () => {
          range = option.key;
          import("../router.js").then((r) => r.refresh());
        },
      }),
    );
  }
  if (window.length) {
    box.append(
      el("span.over", {
        text: `${f.date(window[0].date)} to ${f.date(window[window.length - 1].date)}`,
      }),
    );
  }
  return box;
}

function sliceToRange(history) {
  const option = RANGES.find((r) => r.key === range);
  if (!option) return history;

  // Measured back from the last day this holding was valued, not from today.
  // A holding sold in March has no rows in the last month, and anchoring to
  // today would empty every window.
  const last = new Date(`${history[history.length - 1].date}T00:00:00`).getTime();
  let cutoff;
  if (option.ytd) cutoff = new Date(new Date(last).getFullYear(), 0, 1).getTime();
  else if (option.days === null) return history;
  else cutoff = last - option.days * 86400000;

  const window = history.filter((p) => new Date(`${p.date}T00:00:00`).getTime() >= cutoff);
  // An empty chart reads as "worth nothing"; the whole history is right.
  return window.length > 1 ? window : history;
}

/* -------------------------------------------------------------- charts */

function returnVsPortfolio(h, pts, portfolio) {
  const pal = palette();
  const box = el("div.chart");
  const marks = markers(h, pts);

  // The holding's own euro unit price: local price converted at that day's rate.
  const euroPrices = pts.map((p) => {
    const price = f.num(p.price_local);
    const rate = f.num(p.fx_rate);
    if (price === null || !rate) return null;
    return price / rate;
  });

  const firstPrice = euroPrices.find((v) => v !== null);
  const returnPct = euroPrices.map((v) =>
    v !== null && firstPrice ? v / firstPrice - 1 : null,
  );

  const series = [
    {
      label: h.instrument,
      data: returnPct,
      colour: pal.series[0],
      width: 2.2,
      fill: 0.12,
    },
  ];

  if (portfolio) {
    const byDate = new Map(portfolio.points.map((p) => [p.date, f.num(p.unit_price)]));
    const aligned = pts.map((p) => byDate.get(p.date) ?? null);
    const firstPort = aligned.find((v) => v !== null);
    if (firstPort) {
      series.push({
        label: "Whole portfolio",
        data: aligned.map((v) => (v !== null ? v / firstPort - 1 : null)),
        colour: pal.muted,
        width: 1.5,
        dash: [4, 4],
      });
    }
  }

  queueMicrotask(() =>
    timeChart(box, {
      x: pts.map((p) => toTs(p.date)),
      height: 270,
      kind: "pct",
      zero: true,
      baseline: 0,
      sync: `holding-${h.instrument}`,
      series,
    }),
  );

  return el(
    "div",
    el("h3", { text: "Return vs whole portfolio" }),
    box,
    el("p.chart-note", {
      text:
        `${h.instrument} return compared with the whole portfolio over this window. ` +
        "Red dashed line marks 0% return.",
    }),
    marks,
  );
}

/** Buys and sells, listed under the chart rather than drawn on it. */
function markers(h, pts) {
  const from = pts[0]?.date;
  const to = pts[pts.length - 1]?.date;
  const trades = (h.transactions || []).filter(
    (t) => (t.type === "BUY" || t.type === "SELL") && t.trade_date >= from && t.trade_date <= to,
  );
  if (!trades.length) return el("div");

  return el(
    "p.chart-note",
    { text: "In this window: " },
    trades
      .map((t) => `${t.type === "BUY" ? "bought" : "sold"} ${f.qty(t.quantity)} on ${f.date(t.trade_date)}`)
      .join(" · "),
  );
}

function statsVsPortfolio(h, pts, portfolio) {
  // 1. Holding return & daily returns
  const euroPrices = pts
    .map((p) => {
      const price = f.num(p.price_local);
      const rate = f.num(p.fx_rate);
      return price !== null && rate ? price / rate : null;
    })
    .filter((p) => p !== null);

  const hFirst = euroPrices[0];
  const hLast = euroPrices[euroPrices.length - 1];
  const hReturn = hFirst && hLast ? hLast / hFirst - 1 : null;

  const hDailies = [];
  for (let i = 1; i < euroPrices.length; i++) {
    if (euroPrices[i - 1] > 0) {
      hDailies.push(euroPrices[i] / euroPrices[i - 1] - 1);
    }
  }

  // 2. Portfolio return & daily returns over same window
  let pReturn = null;
  const pDailies = [];
  let pPoints = [];
  if (portfolio && portfolio.points?.length) {
    const fromDate = pts[0]?.date;
    const toDate = pts[pts.length - 1]?.date;
    pPoints = portfolio.points
      .filter((p) => p.date >= fromDate && p.date <= toDate)
      .map((p) => f.num(p.unit_price))
      .filter((p) => p !== null);

    if (pPoints.length >= 2) {
      pReturn = pPoints[pPoints.length - 1] / pPoints[0] - 1;
      for (let i = 1; i < pPoints.length; i++) {
        if (pPoints[i - 1] > 0) {
          pDailies.push(pPoints[i] / pPoints[i - 1] - 1);
        }
      }
    }
  }

  // Volatility calculation helper (annualized std dev * sqrt(252))
  const calcVol = (returns) => {
    if (returns.length < 2) return null;
    const mean = returns.reduce((a, b) => a + b, 0) / returns.length;
    const variance =
      returns.reduce((acc, r) => acc + (r - mean) ** 2, 0) / (returns.length - 1);
    return Math.sqrt(variance * 252);
  };

  // Drawdown helper
  const calcMaxDd = (levels) => {
    if (levels.length < 2) return null;
    let peak = levels[0];
    let worst = 0;
    for (const v of levels) {
      if (v > peak) peak = v;
      if (peak > 0) worst = Math.min(worst, v / peak - 1);
    }
    return worst;
  };

  const hVol = calcVol(hDailies);
  const pVol = calcVol(pDailies);
  const hMaxDd = calcMaxDd(euroPrices);
  const pMaxDd = calcMaxDd(pPoints);
  const spread = hReturn !== null && pReturn !== null ? hReturn - pReturn : null;
  const volRatio = hVol && pVol ? hVol / pVol : null;

  const lastPt =
    portfolio?.points?.length ? portfolio.points[portfolio.points.length - 1] : null;
  const lastPortVal =
    lastPt ? f.num(lastPt.total_value_eur ?? lastPt.value) : null;
  const weightPct =
    lastPortVal && f.num(h.market_value_eur)
      ? f.num(h.market_value_eur) / lastPortVal
      : null;

  const cardNode = el(
    "div.holding-stats-card",
    el(
      "table.holding-stats-table",
      el(
        "tbody",
        el(
          "tr",
          el("td.metric-label", { text: "Return in window" }),
          el("td.metric-val", {
            text: `${f.pct(hReturn, { sign: true })} vs ${f.pct(pReturn, { sign: true })} port`,
            cls: f.tone(hReturn),
          }),
        ),
        el(
          "tr",
          el("td.metric-label", { text: "Performance spread" }),
          el("td.metric-val", {
            text:
              spread !== null
                ? `${f.pct(spread, { sign: true })} ${spread >= 0 ? "ahead" : "behind"}`
                : "—",
            cls: f.tone(spread),
          }),
        ),
        el(
          "tr",
          el("td.metric-label", { text: "Variability (ann. vol)" }),
          el("td.metric-val", {
            text:
              hVol !== null && pVol !== null
                ? `${f.pct(hVol, { digits: 1 })} vs ${f.pct(pVol, { digits: 1 })} port (${volRatio ? volRatio.toFixed(1) + "x" : ""})`
                : hVol !== null
                  ? f.pct(hVol, { digits: 1 })
                  : "—",
          }),
        ),
        el(
          "tr",
          el("td.metric-label", { text: "Worst drop from peak" }),
          el("td.metric-val", {
            text:
              hMaxDd !== null && pMaxDd !== null
                ? `${f.pct(hMaxDd, { sign: true })} vs ${f.pct(pMaxDd, { sign: true })} port`
                : hMaxDd !== null
                  ? f.pct(hMaxDd, { sign: true })
                  : "—",
            cls: "loss",
          }),
        ),
        el(
          "tr",
          el("td.metric-label", { text: "Position value (% of port)" }),
          el("td.metric-val", {
            text:
              weightPct !== null
                ? `${f.eur(h.market_value_eur)} (${f.pct(weightPct, { digits: 1 })})`
                : f.eur(h.market_value_eur),
          }),
        ),
      ),
    ),
  );

  return el(
    "div",
    el("h3", { text: "Variability & return vs portfolio" }),
    cardNode,
    el("p.chart-note", {
      text: "Variability is annualised standard deviation of daily price returns over this window.",
    }),
  );
}

function priceAndFx(h, pts) {
  const pal = palette();
  const box = el("div.chart");

  queueMicrotask(() =>
    timeChart(box, {
      x: pts.map((p) => toTs(p.date)),
      height: 270,
      kind: "plain",
      sync: `holding-${h.instrument}`,
      axes: [{ scale: "fx", kind: "plain" }],
      series: [
        {
          label: `Price (${h.currency})`,
          data: pts.map((p) => f.num(p.price_local)),
          colour: pal.series[0],
        },
        {
          label: `${h.currency} per EUR`,
          data: pts.map((p) => f.num(p.fx_rate)),
          colour: pal.series[2],
          scale: "fx",
          width: 1.4,
        },
      ],
    }),
  );

  return el(
    "section",
    heading("Price and exchange rate"),
    box,
    el("p.chart-note", {
      text:
        `The euro value moves when either line moves. A rising price with a ` +
        `weakening ${h.currency} can leave the euro value flat, which is why ` +
        "these are drawn apart rather than combined.",
    }),
  );
}

function currencySplit(h, pts) {
  const pal = palette();
  const box = el("div.chart");

  queueMicrotask(() =>
    timeChart(box, {
      x: pts.map((p) => toTs(p.date)),
      height: 270,
      kind: "eur",
      sync: `holding-${h.instrument}`,
      series: [
        {
          label: "From the price",
          data: pts.map((p) => f.num(p.unrealized_local_eur)),
          colour: pal.series[1],
        },
        {
          label: `From ${h.currency}`,
          data: pts.map((p) => f.num(p.unrealized_fx_eur)),
          colour: pal.series[2],
        },
        {
          label: "Total gain",
          data: pts.map((p) => f.num(p.unrealized_eur)),
          colour: pal.fg,
          width: 2,
        },
      ],
    }),
  );

  const d = h.return_decomposition || {};
  return el(
    "section",
    heading("What the gain is made of"),
    box,
    divergingBars([
      {
        label: "From the price",
        value: fraction(d.local_gain_at_todays_rate, h.cost_basis_eur),
      },
      { label: `From ${h.currency}`, value: fraction(d.currency_effect, h.cost_basis_eur) },
    ]),
    el("p.chart-note", { text: d.identity || "" }),
  );
}

function fraction(value, base) {
  const v = f.num(value);
  const b = f.num(base);
  return v === null || b === null || b === 0 ? null : v / b;
}

/* -------------------------------------------------------------- tables */

function lotsTable(h) {
  const open = (h.lots || []).filter((l) => (f.num(l.qty_remaining) ?? 0) > 0);
  if (!open.length) return el("div");

  // A euro holding has no exchange rate worth showing: it is 1, on every row,
  // for ever. A column of ones is a column of noise.
  const foreign = h.currency !== "EUR";

  const rows = open.map((l) =>
    row([
      f.date(l.open_date),
      f.qty(l.qty_remaining),
      foreign ? `${f.qty(l.cost_per_unit_local)} ${l.currency}` : f.eur(l.cost_per_unit_local),
      f.eur(l.cost_total_eur),
      ...(foreign ? [f.qty(l.fx_rate_at_open)] : []),
      {
        node:
          l.basis_quality === "EXACT"
            ? document.createTextNode("exact")
            : el("span.tag.warn", {
                text: l.basis_quality.toLowerCase(),
                title:
                  "The euro cost of this lot was reconstructed rather than read " +
                  "from a contract note, so the gain computed from it is an estimate.",
              }),
      },
    ]),
  );

  const head = [
    ["Bought", null],
    ["Units", null],
    ["Cost / unit", null],
    ["Cost (EUR)", "Frozen at the exchange rate on the day you bought."],
    ...(foreign ? [["Rate then", `${h.currency} per euro on the day you bought.`]] : []),
    ["Basis", "Whether the euro cost was read from a record or reconstructed."],
  ];

  return el(
    "section",
    heading("Open lots"),
    el(
      "div.wrap",
      el(
        "table",
        el("thead", el("tr", head.map(([label, hint]) => el("th", withHint(label, hint))))),
        el("tbody", rows),
      ),
    ),
    el("p.note", {
      text: "German tax sells these oldest first. A sale takes from the top down.",
    }),
  );
}


function disposalsTable(h) {
  const disposals = h.disposals || [];
  if (!disposals.length) return el("div");

  const rows = disposals.map((d) =>
    row([
      f.date(d.sell_date),
      f.qty(d.quantity),
      f.eur(d.proceeds_eur),
      f.eur(d.cost_basis_eur),
      { text: f.eur(d.gross_gain_eur, { sign: true }), cls: f.tone(d.gross_gain_eur) },
      f.eur(d.vap_offset_eur),
      f.eur(d.teilfreistellung_eur),
      { text: f.eur(d.taxable_gain_eur, { sign: true }), cls: f.tone(d.taxable_gain_eur) },
    ]),
  );

  // "Less advance charge" and "Less exemption" meant nothing without the rule
  // behind them, and the rule does not fit in a column header -- so it lives
  // on the hover, and the header says what the number does.
  const head = [
    ["Sold", null],
    ["Units", null],
    ["Proceeds", "What the sale raised, after dealing costs."],
    ["Cost", "What you paid, at the exchange rate on the day you bought."],
    ["Gain", "Proceeds less cost. Before any tax relief."],
    [
      "Already taxed",
      "Tax you already paid on this holding in earlier years, through the annual advance charge (Vorabpauschale). It is deducted here so the same euros are not taxed twice.",
    ],
    [
      "Tax-free part",
      "The share of a fund's gain that is simply not taxed — 30% for an equity fund, nothing for a bond or money-market one (Teilfreistellung).",
    ],
    ["Taxable", "What actually counts against your allowance, after both deductions."],
  ];

  return el(
    "section",
    heading("Sales"),
    el(
      "div.wrap",
      el(
        "table",
        el("thead", el("tr", head.map(([label, hint]) => el("th", withHint(label, hint))))),
        el("tbody", rows),
      ),
    ),
    el("p.note", {
      text: "Planning figures. Your bank's annual tax certificate is authoritative.",
    }),
  );
}


function withHint(label, hint) {
  return hint ? el("span.hint", { title: hint, text: label }) : el("span", { text: label });
}


/*
 * What the holding paid out, kept apart from what you did with it.
 *
 * A trade is a decision you made; a distribution is something the holding did.
 * In one list the income stream is invisible -- five dividends scattered
 * between forty buys read as noise rather than as a yield.
 */
function dividendsSection(h) {
  const d = h.dividends || {};
  if (!d.count) return el("div");

  const rows = (d.payments || []).map((p) =>
    row([
      f.date(p.date),
      p.type === "INTEREST" ? "interest" : "dividend",
      f.eur(p.amount_eur),
    ]),
  );

  return el(
    "section",
    heading("What it has paid you"),
    el(
      "div.cards",
      card("Received in total", f.eur(d.total_eur), {
        cls: "total",
        sub: `${d.count} payment${d.count === 1 ? "" : "s"} since ${f.date(d.first)}`,
      }),
      card("Last 12 months", f.eur(d.last_12m_eur)),
      hintCard(
        "Yield on what you paid",
        "Last twelve months of payments against what this holding cost you. This is what you earn, and it rises as the price does.",
        f.pct(d.yield_on_cost),
      ),
      hintCard(
        "Yield at today's price",
        "The same payments against what the holding is worth now — what somebody buying today would get. Lower than the figure beside it whenever the price has risen.",
        f.pct(d.current_yield),
      ),
    ),
    el(
      "div.wrap",
      el(
        "table",
        el("thead", el("tr", ["Paid", "What", "Amount"].map((x) => el("th", { text: x })))),
        el("tbody", rows),
      ),
    ),
    el("p.note", { text: d.basis }),
  );
}


function hintCard(label, hint, value, opts = {}) {
  const node = card("", value, opts);
  node.querySelector(".k").replaceChildren(withHint(label, hint));
  return node;
}


function transactionsTable(h) {
  // Distributions have their own section; this is what you did.
  const txns = (h.transactions || []).filter(
    (x) => x.type !== "DIVIDEND" && x.type !== "INTEREST",
  );
  if (!txns.length) return el("div");

  const rows = txns.map((t) =>
    row([
      f.date(t.trade_date),
      t.type.toLowerCase().replace("_", " "),
      f.qty(t.quantity),
      t.price_per_unit_local === null ? "—" : `${f.qty(t.price_per_unit_local)} ${t.currency}`,
      f.eur(t.gross_amount_eur),
      {
        node: el(
          "span",
          t.source !== "IMPORT" ? el("span.tag", { text: t.source.toLowerCase() }) : null,
          t.confidence !== "ACTUAL"
            ? el("span.tag.warn", { text: t.confidence.toLowerCase() })
            : null,
          !t.fees_known && (t.type === "BUY" || t.type === "SELL")
            ? el("span.tag.warn", {
                text: "no fees",
                title:
                  "The source recorded no dealing fee, so every gain computed " +
                  "from this row is slightly too high.",
              })
            : null,
        ),
      },
    ]),
  );

  return el(
    "section",
    heading("Your trades"),
    el(
      "div.wrap",
      el(
        "table",
        el("thead", el("tr", ["Date", "What", "Units", "Price", "Amount", ""].map((t) => el("th", { text: t })))),
        el("tbody", rows),
      ),
    ),
  );
}

function taxPanel(h) {
  if (h.tax_regime && h.tax_regime !== "DE_INVESTMENT") {
    return el(
      "div.panel.warn",
      el("h3", { text: "Outside German investment tax" }),
      el("p", { text: h.tax_scope_note || "This holding is not covered by the tax figures." }),
    );
  }
  if (h.is_fund && !f.has(h.teilfreistellung_pct)) {
    return el(
      "div.panel.warn",
      el("h3", { text: "Partial exemption rate not set" }),
      el("p", {
        text:
          "This is a fund, so part of any gain is tax-free — but the rate has " +
          "not been recorded, so its tax figures are incomplete.",
      }),
      el("p", el("a", { href: "#/settings", text: "Set it in Settings" })),
    );
  }
  return el("div");
}

export { table };
