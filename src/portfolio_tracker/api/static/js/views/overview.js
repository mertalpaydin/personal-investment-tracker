/*
 * The front page, over one chosen window.
 *
 * Three things shape it.
 *
 * **One window, everywhere.** The range picker at the top drives every card,
 * chart and table below it. A page whose headline says "today" while the chart
 * beside it says "since inception" invites the two to be read together, and
 * they are not comparable.
 *
 * **Value change and return are different numbers, shown apart.** Over this
 * portfolio's year to date the value rose 41% and the return was 7.8%; the
 * difference is money paid in. Merging them is the commonest way a tracker
 * flatters its owner.
 *
 * **Every holding gets both of its numbers.** What it did on its own money,
 * and how many points of the portfolio's return came from it. A holding up 99%
 * that is 6% of the portfolio contributes about 6.8 points, and showing only
 * the second reads as though it had barely moved.
 */

import { el, card, row, heading } from "../dom.js";
import { get, all, isError } from "../api.js";
import * as f from "../fmt.js";
import { timeChart, toTs, palette } from "../charts.js";
import { bars, divergingBars } from "../bars.js";
import { explainPanel } from "../narrative.js";
import * as router from "../router.js";
import { columnChart } from "../columnCharts.js";

const AGG_RANGES = [
  ["6M", "6 months"],
  ["1Y", "1 year"],
  ["ALL", "All"],
];

let aggRange = "6M";

const RANGES = [
  ["1D", "1 day"],
  ["1W", "1 week"],
  ["1M", "1 month"],
  ["3M", "3 months"],
  ["6M", "6 months"],
  ["YTD", "This year"],
  ["1Y", "1 year"],
  ["ALL", "All"],
];

//: Kept across navigation, so returning to the page does not reset it.
// A day of movement is noise, and the page opened on it. A month is the
// shortest window in which anything about a portfolio is visible.
let range = "1M";

export async function render({ query }) {
  if (query.get("range")) range = query.get("range").toUpperCase();

  const data = await all({
    over: get("/api/overview", { range }),
    portfolio: get("/api/portfolio"),
    allocation: get("/api/allocation", { dim: "asset_class" }),
  });

  if (isError(data.over)) throw data.over;
  const o = data.over;

  const series = await get("/api/returns/series", { from: o.window.from }).catch(() => null);

  const view = el("div", el("h1", { text: "Overview" }));
  view.append(topReturnBoxes(o));
  view.append(rangePicker(o));
  view.append(headline(o, isError(data.portfolio) ? null : data.portfolio));
  view.append(explainPanel("/api/brief", {}, "Summarise this in words"));
  if (series) view.append(valueChart(o, series));
  view.append(aggregatedReturnsSection());
  view.append(mainHoldingsLayout(o, isError(data.allocation) ? null : data.allocation));
  if (o.goal) view.append(goalSection(o.goal));
  return view;
}

/* --------------------------------------------------------- top returns */

function topReturnBoxes(o) {
  const items = o.top_returns || [];
  if (!items.length) return el("div");

  const container = el("div.top-returns");
  for (const item of items) {
    const isCurrent = item.range_key === range;
    const cardEl = el(
      "div.card.return-box",
      {
        "aria-pressed": String(isCurrent),
        title: `Switch overview window to ${item.label}`,
        onclick: () => {
          range = item.range_key;
          router.go("#/?range=" + item.range_key);
        },
      },
      el("span.k", { text: `${item.label} Return` }),
      el("div.v", {
        text: f.pct(item.twr, { sign: true }),
        cls: f.tone(item.twr),
      }),
      el("div.sub2", {
        text: f.eur(item.value_eur, { sign: true }),
        cls: f.tone(item.value_eur),
      }),
    );
    container.append(cardEl);
  }
  return el("section.top-returns-section", container);
}

/* --------------------------------------------------------------- window */

function rangePicker(o) {
  const box = el("div.ranges");
  for (const [key, label] of RANGES) {
    box.append(
      el("button", {
        text: label,
        "aria-pressed": String(key === range),
        onclick: () => {
          range = key;
          router.go("#/?range=" + key);
        },
      }),
    );
  }
  box.append(el("span.over", { text: `${f.date(o.window.from)} to ${f.date(o.window.to)}` }));
  return box;
}

/** A label with its explanation on hover, so a card needs no paragraph. */
function withHint(label, hint) {
  return el("span.hint", { title: hint, text: label });
}

/** `card()` with a hover hint on the label and optional stacked sub-lines. */
function hintCard(label, hint, value, opts = {}) {
  const node = card("", value, opts);
  node.querySelector(".k").replaceChildren(withHint(label, hint));
  if (opts.subLines) {
    node.querySelector(".sub2")?.remove();
    node.append(el("div.sub2.stack", opts.subLines.map((line) => el("span", { text: line }))));
  }
  return node;
}

/* ------------------------------------------------------------- headline */

function headline(o, p) {
  const v = o.value;
  const cashVal = o.cash_eur ?? p?.uninvested_cash_eur ?? 0;

  const cards = el(
    "div.cards",
    card("Total value", f.eur(v.now_eur), {
      cls: "total",
      sub: `${f.eur(v.change_eur, { sign: true })} ${o.window.label}`,
    }),
    // Adjacent and deliberately distinct: one moves when you pay money in,
    // the other does not.
    hintCard(
      "Value change",
      "How much the total moved. Money you paid in moves this too, so it is not a return.",
      f.pct(v.change_pct, { sign: true }),
      { valueCls: f.tone(v.change_pct), sub: f.eur(v.change_eur, { sign: true }) },
    ),
    hintCard(
      "Cash balance",
      "Uninvested cash across depot and bank accounts.",
      f.eur(cashVal),
      {
        sub: p?.uninvested_cash?.from_capital_eur
          ? `${f.eur(p.uninvested_cash.from_capital_eur)} capital`
          : "available cash",
      },
    ),
  );

  if (p) {
    cards.append(
      hintCard(
        "Unrealised",
        "Gain on what you still hold, if you sold it all today at these prices.",
        f.eur(p.unrealized_eur, { sign: true }),
        {
          valueCls: f.tone(p.unrealized_eur),
          // Two lines: one ran past the edge of the card.
          subLines:
            p.unrealized_fx_eur && Math.abs(p.unrealized_fx_eur) >= 1
              ? [
                  `${f.eur(p.unrealized_local_eur, { sign: true })} from prices`,
                  `${f.eur(p.unrealized_fx_eur, { sign: true })} from currency`,
                ]
              : null,
        },
      ),
      hintCard(
        "Realised",
        "Gain already locked in by selling. Unlike the figure beside it, this one cannot change.",
        f.eur(p.realized_gain_eur, { sign: true }),
        { valueCls: f.tone(p.realized_gain_eur) },
      ),
      hintCard(
        "Money put in",
        "Deposits less withdrawals, since you started. Dividends are not counted — those are a return, not something you paid in.",
        f.eur(p.net_contributions_eur, { compact: true }),
      ),
    );
  }

  const section = el("section", cards);
  if (o.return && !o.return.reliable) {
    section.append(el("p.note.bad", { text: o.return.unreliable_because }));
  }
  return section;
}

/* ---------------------------------------------------------------- chart */

function valueChart(o, series) {
  const pts = series.points;
  if (pts.length < 2) return el("div");

  const pal = palette();
  const box = el("div.chart");
  const holdingsBox = el("div.chart");
  const firstUnitPrice = f.num(pts[0].unit_price);
  const holdingItems = series.holdings || [];

  queueMicrotask(() => {
    // Portfolio Return
    timeChart(box, {
      x: pts.map((r) => toTs(r.date)),
      height: 280,
      kind: "pct",
      zero: true,
      baseline: 0,
      sync: "overview",
      series: [
        {
          label: "Portfolio",
          data: pts.map((r) =>
            firstUnitPrice ? f.num(r.unit_price) / firstUnitPrice - 1 : 0,
          ),
          colour: pal.accent,
          width: 2.2,
          fill: 0.12,
        },
      ],
    });

    // Holdings Performance comparison
    if (holdingItems.length) {
      const dates = pts.map((r) => r.date);
      const hSeries = [];

      holdingItems.forEach((h, i) => {
        const priceMap = new Map(h.points.map((p) => [p.date, f.num(p.price_eur)]));
        const alignedPrices = dates.map((d) => priceMap.get(d) ?? null);
        const firstPrice = alignedPrices.find((p) => p !== null);
        if (firstPrice) {
          const returnsData = alignedPrices.map((p) =>
            p !== null ? p / firstPrice - 1 : null,
          );
          hSeries.push({
            label: h.instrument,
            data: returnsData,
            colour: pal.series[i % pal.series.length],
            width: 1.6,
            spanGaps: true,
          });
        }
      });

      timeChart(holdingsBox, {
        x: pts.map((r) => toTs(r.date)),
        height: 280,
        kind: "pct",
        zero: true,
        baseline: 0,
        sync: "overview",
        series: hSeries,
      });
    }
  });

  return el(
    "section",
    heading("Performance"),
    el(
      "div.chart-pair",
      el(
        "div",
        el("h3", { text: "Portfolio return" }),
        box,
        el("p.chart-note", {
          text:
            "Time-weighted return over this window, neutralising deposits and withdrawals. " +
            "Red dashed line marks 0% return.",
        }),
      ),
      el(
        "div",
        el("h3", { text: "Holdings performance" }),
        holdingsBox,
        el("p.chart-note", {
          text:
            "Individual holding returns over this window. Compares price movement per unit.",
        }),
      ),
    ),
  );
}

/* -------------------------------------------------- aggregated returns */

function aggregatedReturnsSection() {
  const section = el("section");
  const monthlyBox = el("div.agg-chart-container");
  const weeklyBox = el("div.agg-chart-container");

  const rangeButtons = el("div.agg-returns-ranges.ranges");

  async function loadData() {
    monthlyBox.replaceChildren(el("p.nodata", { text: "Loading monthly returns…" }));
    weeklyBox.replaceChildren(el("p.nodata", { text: "Loading weekly returns…" }));

    try {
      const data = await get("/api/returns/aggregated", { range: aggRange });
      if (isError(data)) {
        monthlyBox.replaceChildren(el("p.nodata", { text: "Could not load returns." }));
        weeklyBox.replaceChildren(el("p.nodata", { text: "Could not load returns." }));
        return;
      }
      columnChart(monthlyBox, data.monthly, { height: 260, emptyText: "No monthly data in this window" });
      columnChart(weeklyBox, data.weekly, { height: 260, emptyText: "No weekly data in this window" });
    } catch {
      monthlyBox.replaceChildren(el("p.nodata", { text: "Could not load returns." }));
      weeklyBox.replaceChildren(el("p.nodata", { text: "Could not load returns." }));
    }
  }

  function renderButtons() {
    rangeButtons.replaceChildren();
    for (const [key, label] of AGG_RANGES) {
      rangeButtons.append(
        el("button", {
          text: label,
          "aria-pressed": String(key === aggRange),
          onclick: () => {
            if (aggRange === key) return;
            aggRange = key;
            renderButtons();
            loadData();
          },
        }),
      );
    }
  }

  renderButtons();

  section.append(
    el(
      "div.agg-returns-header",
      heading("Aggregated returns"),
      rangeButtons,
    ),
    el(
      "div.chart-pair",
      el(
        "div",
        el("h3", { text: "Monthly aggregated return" }),
        monthlyBox,
        el("p.chart-note", {
          text: "Gain and percentage return for each specific month. Solid line marks 0.0% baseline.",
        }),
      ),
      el(
        "div",
        el("h3", { text: "Weekly aggregated return" }),
        weeklyBox,
        el("p.chart-note", {
          text: "Gain and percentage return for each specific week. Solid line marks 0.0% baseline.",
        }),
      ),
    ),
  );

  queueMicrotask(() => {
    loadData();
  });

  return section;
}

/* ------------------------------------------------------------- columns */

/*
 * Where the return came from and what it is invested in placed to the left
 * of By holding.
 */
function mainHoldingsLayout(o, allocation) {
  const leftCol = el("div.breakdown-col");
  const a = o.attribution;

  if (a) {
    leftCol.append(
      el(
        "section",
        heading("Where the return came from"),
        divergingBars([
          { label: "Asset prices", value: f.num(a.asset_prices) },
          { label: "Currency", value: f.num(a.currency) },
          { label: "Income", value: f.num(a.income) },
          { label: "Trading", value: f.num(a.trading) },
        ]),
        el("p.chart-note", {
          text: `Adds up to ${f.pct(a.total, { sign: true })} ${o.window.label}.`,
        }),
      ),
    );
  }

  if (allocation) {
    const items = (allocation.buckets || allocation.allocation || []).map((b) => ({
      label: b.bucket ?? b.name ?? b.key,
      value: f.num(b.weight ?? b.pct),
      right: f.eur(b.value_eur, { compact: true }),
    }));
    if (items.length) {
      leftCol.append(
        el(
          "section",
          heading("What it is invested in"),
          bars(items, { format: (v) => f.pct(v, { digits: 1 }) }),
        ),
      );
    }
  }

  const rightCol = el("div.holdings-col", holdingsSection(o));
  return el("div.overview-split", leftCol, rightCol);
}

/* ------------------------------------------------------------ holdings */

function holdingsSection(o) {
  const holdings = o.holdings || [];
  if (!holdings.length) return el("div");

  const ranked = [...holdings].sort(
    (a, b) => Math.abs(b.contribution ?? 0) - Math.abs(a.contribution ?? 0),
  );

  const rows = ranked.map((h) =>
    row([
      {
        node: el("a", {
          href: `#/holdings/${encodeURIComponent(h.instrument)}`,
          text: h.instrument,
        }),
        cls: "name",
      },
      { text: h.name, cls: "name muted" },
      f.eur(h.value_eur),
      { text: f.eur(h.value_change_eur, { sign: true }), cls: f.tone(h.value_change_eur) },
      {
        text: f.pct(h.own_return, { sign: true }),
        cls: f.tone(h.own_return),
        title: h.held_from ? `measured from ${f.date(h.held_from)}` : undefined,
      },
      { text: f.pp(h.contribution), cls: f.tone(h.contribution) },
    ]),
  );

  const head = [
    ["Ticker", null],
    ["Name", null],
    ["Value", null],
    ["Change", "How much its value moved over this window, in euros."],
    [
      "Its own return",
      "What this holding earned on its own money, per unit — so buying more of it does not read as a gain.",
    ],
    [
      "Share of the total",
      "How many percentage points of the whole portfolio's return came from this holding. A big gain on a small position is a small share.",
    ],
  ];

  return el(
    "section",
    heading("By holding"),
    el(
      "div.wrap",
      el(
        "table",
        el(
          "thead",
          el(
            "tr",
            head.map(([label, hint]) =>
              el("th", hint ? withHint(label, hint) : el("span", { text: label })),
            ),
          ),
        ),
        el("tbody", rows),
      ),
    ),
    el("p.note", {
      text:
        "The last two columns answer different questions. A holding up 99% that is " +
        "6% of your portfolio shows +99% and about +6 points.",
    }),
  );
}

/* ---------------------------------------------------------------- goal */

function goalSection(g) {
  const pct = Math.max(0, Math.min(1, f.num(g.progress_pct) ?? 0));
  const box = el(
    "section",
    heading("Goal"),
    el("div.progress", el("span", { style: `width:${(pct * 100).toFixed(1)}%` })),
    el(
      "div.cards",
      card("Target", f.eur(g.target_eur, { compact: true })),
      card("Reached", f.pct(pct, { digits: 1 })),
      card("Still to go", f.eur(g.remaining_eur, { compact: true })),
    ),
  );

  // The date, without being asked for. "How much is left" is the one thing a
  // target does not need help with; when you get there is the question.
  const when = el("div.cards", { style: "margin-top: 1rem;" });
  box.append(when, el("p.note", { style: "margin-top: 0.75rem;" }, el("a", { href: "#/plan", text: "Change the plan" })));

  queueMicrotask(async () => {
    try {
      const p = await get("/api/projection", { paths: 300, years: 15 });
      const b = p.baseline;
      when.replaceChildren(
        hintCard(
          "On track for",
          "The month by which the target is reached in three of every four resampled histories. Not a promise.",
          b.dates["0.75"] ? f.month(b.dates["0.75"]) : "not within 15 years",
          { cls: "total", sub: "three times in four" },
        ),
        card("Half the time", b.dates["0.50"] ? f.month(b.dates["0.50"]) : "—"),
        card("Nine times in ten", b.dates["0.90"] ? f.month(b.dates["0.90"]) : "—"),
        card("Adding", f.eur(b.monthly_contribution_eur), { sub: "a month, measured" }),
        // A date with no assumptions beside it is a date somebody will quote
        // back six months later having forgotten it was conditional.
        card(
          "Return assumed",
          p.returns ? f.pct(p.returns.implied_annual) : f.DASH,
          {
            sub: p.returns?.from_history ? "from your own price history" : "assumed, not measured",
            valueCls: p.returns?.from_history ? "" : "warn",
          },
        ),
      );
    } catch {
      when.replaceChildren(el("p.note", { text: "Not enough history to project yet." }));
    }
  });

  return box;
}
