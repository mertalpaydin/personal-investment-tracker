/*
 * The overview.
 *
 * The layout answers three questions in order, because that is the order they
 * get asked in: how much is there, is it growing, and where did the growth
 * come from.
 *
 * The hero chart is value against money-put-in. A line of portfolio value on
 * its own is nearly meaningless for someone contributing monthly -- it rises
 * whether or not anything is being earned. Drawing the contribution line
 * underneath makes the gap between them the return, visibly, with no
 * percentage to interpret.
 */

import { el, card, table, row, heading } from "../dom.js";
import { get, all, isError } from "../api.js";
import * as f from "../fmt.js";
import { timeChart, toTs, palette } from "../charts.js";
import { bars, divergingBars } from "../bars.js";

export async function render() {
  const data = await all({
    portfolio: get("/api/portfolio"),
    returns: get("/api/returns"),
    series: get("/api/returns/series"),
    allocation: get("/api/allocation", { dim: "asset_class" }),
    goal: get("/api/goal"),
  });

  // The portfolio endpoint is the only hard dependency: with no valuation
  // there is no overview to draw, and saying so beats five empty panels.
  if (isError(data.portfolio)) throw data.portfolio;

  const p = data.portfolio;
  const view = el("div");

  view.append(
    el("h1", { text: "Overview" }),
    el("p.sub", {
      text:
        `Valued to ${f.date(p.as_of)}` +
        (data.returns && !isError(data.returns) ? ` · ${data.returns.days} days tracked` : ""),
    }),
  );

  view.append(headline(p));
  view.append(outsideTheTotal(p));

  if (!isError(data.series) && data.series.points.length > 1) {
    view.append(valueVsContributions(data.series));
    view.append(indexAndDrawdown(data.series));
  }

  if (!isError(data.returns)) {
    view.append(returnCards(data.returns));
    view.append(attributionSection(data.returns));
  }

  if (!isError(data.allocation)) view.append(allocationSection(data.allocation));
  if (!isError(data.goal)) view.append(goalSection(data.goal));

  return view;
}

/* ------------------------------------------------------------- headline */

function headline(p) {
  return el(
    "section",
    el(
      "div.cards",
      card("Total", f.eur(p.total_value_eur), { cls: "total" }),
      card("Holdings", f.eur(p.holdings_eur)),
      card("Deposits", f.eur(p.deposits_eur)),
      card("Money put in", f.eur(p.net_contributions_eur), {
        sub: "deposits and withdrawals only",
      }),
      card("Unrealised", f.eur(p.unrealized_eur, { sign: true }), {
        valueCls: f.tone(p.unrealized_eur),
        sub:
          p.unrealized_fx_eur && Math.abs(p.unrealized_fx_eur) >= 1
            ? `${f.eur(p.unrealized_local_eur, { sign: true })} price, ` +
              `${f.eur(p.unrealized_fx_eur, { sign: true })} currency`
            : null,
      }),
      card("Realised", f.eur(p.realized_gain_eur, { sign: true }), {
        valueCls: f.tone(p.realized_gain_eur),
      }),
    ),
  );
}

/*
 * Cash sits beside the total, never inside it. Whether that money is still
 * held cannot be known from a positions export, and quietly adding it would
 * inflate the headline with a guess.
 */
function outsideTheTotal(p) {
  const cash = p.uninvested_cash || {};
  if (!f.has(p.uninvested_cash_eur) && !f.has(p.income_received_eur)) return el("div");
  return el(
    "section",
    heading("Outside the total"),
    el(
      "div.cards",
      card("Cash from sales", f.eur(cash.from_capital_eur), { cls: "aside" }),
      card("Cash from income", f.eur(cash.from_income_eur), { cls: "aside" }),
      card("Income received", f.eur(p.income_received_eur), {
        cls: "aside",
        sub: "whether or not still held as cash",
      }),
    ),
    el("p.note", {
      text:
        "None of these is counted in the total above. " +
        (cash.attribution_rule || ""),
    }),
  );
}

/* ---------------------------------------------------------- hero chart */

function valueVsContributions(series) {
  const pts = series.points;
  const x = pts.map((r) => toTs(r.date));
  const pal = palette();

  const box = el("div.chart");
  const section = el(
    "section",
    heading("Value against money put in"),
    el(
      "div.legend",
      legendItem(pal.series[0], "What it is worth"),
      legendItem(pal.muted, "What you put in"),
    ),
    box,
    el("p.chart-note", {
      text:
        "The gap between the lines is everything the portfolio earned: price " +
        "moves, currency, dividends and interest. Contributions are deposits " +
        "and withdrawals only — a reinvested dividend is a return, not a " +
        "contribution, and is deliberately not counted as money put in.",
    }),
  );

  // Deferred so the container has a measured width. uPlot needs pixels, and
  // an element not yet in the document reports zero.
  queueMicrotask(() =>
    timeChart(box, {
      x,
      height: 300,
      kind: "eur",
      zero: true,
      sync: "portfolio",
      series: [
        { label: "Value", data: pts.map((r) => f.num(r.total_value_eur)), fill: 0.14 },
        {
          label: "Put in",
          data: pts.map((r) => f.num(r.net_contributions_eur)),
          colour: pal.muted,
          width: 1.4,
          dash: [4, 4],
        },
      ],
    }),
  );

  return section;
}

function indexAndDrawdown(series) {
  const pts = series.points;
  const x = pts.map((r) => toTs(r.date));
  const pal = palette();
  const indexBox = el("div.chart");
  const ddBox = el("div.chart");

  queueMicrotask(() => {
    timeChart(indexBox, {
      x,
      height: 210,
      kind: "index",
      sync: "portfolio",
      series: [
        {
          label: "Return index",
          data: pts.map((r) => f.num(r.unit_price)),
          colour: pal.series[1],
          fill: 0.1,
        },
      ],
    });
    timeChart(ddBox, {
      x,
      height: 130,
      kind: "pct",
      sync: "portfolio",
      series: [
        {
          label: "Below peak",
          data: pts.map((r) => f.num(r.drawdown_pct)),
          colour: pal.loss,
          fill: 0.14,
        },
      ],
    });
  });

  return el(
    "section",
    heading("Return, with contributions taken out"),
    indexBox,
    el("p.chart-note", {
      text:
        "Base 100 at inception. Money going in or out moves the total value " +
        "but not this line, so it shows what the investments did rather than " +
        "how much was saved.",
    }),
    el("h3", { text: "How far below the previous peak" }),
    ddBox,
  );
}

/* ------------------------------------------------------------- returns */

function returnCards(r) {
  const cards = el(
    "div.cards",
    card("Total return", f.pct(r.twr, { sign: true }), {
      valueCls: f.tone(r.twr),
      sub: "time-weighted",
    }),
    card("Per year", f.pct(r.twr_annualised, { sign: true }), {
      valueCls: f.tone(r.twr_annualised),
    }),
    card("Your return", f.pct(r.xirr, { sign: true }), {
      valueCls: f.tone(r.xirr),
      sub: `money-weighted · ${r.xirr_reliability} confidence`,
    }),
    card("Timing effect", f.pp(r.timing_gap), {
      sub: "your return minus the portfolio's",
    }),
    card("Volatility", f.pct(r.volatility)),
    card("Worst fall", f.pct(r.max_drawdown), { valueCls: "loss" }),
  );

  return el(
    "section",
    heading("Return"),
    cards,
    el("p.note", {
      text:
        "Total return is what the investments did. Your return also counts " +
        "when the money went in. They differ when contributions land before " +
        "a good or a bad stretch.",
    }),
    caveats(r.caveats),
  );
}

function attributionSection(r) {
  const a = r.attribution;
  if (!a) return el("div");

  const meanings = a.meanings || {};
  const parts = [
    ["Asset prices", a.local, meanings.local],
    ["Currency", a.currency, meanings.currency],
    ["Income", a.income, meanings.income],
    ["Trading", a.unexplained, meanings.unexplained],
  ];

  const section = el(
    "section",
    heading("Where the return came from"),
    divergingBars(parts.map(([label, value, title]) => ({ label, value: f.num(value), title }))),
    el("p.chart-note", {
      text:
        `The four add up to the total return of ${f.pct(a.total, { sign: true })}, ` +
        `linked by the ${a.linking_method} method so the parts sum to the whole ` +
        "rather than leaving a gap.",
    }),
  );

  const byHolding = r.attribution_by_holding;
  if (byHolding && byHolding.holdings?.length) {
    const ranked = [...byHolding.holdings].sort((x, y) => (y.total ?? 0) - (x.total ?? 0));
    section.append(
      el("h3", { text: "By holding" }),
      divergingBars(
        ranked.map((h) => ({
          label: h.instrument,
          value: f.num(h.total),
          href: `#/holdings/${encodeURIComponent(h.instrument)}`,
        })),
      ),
      el("p.chart-note", { text: byHolding.unit }),
      caveats(byHolding.caveats),
    );
  }

  return section;
}

/* --------------------------------------------------------- allocation */

function allocationSection(a) {
  const items = (a.buckets || a.allocation || []).map((b) => ({
    label: b.bucket ?? b.name ?? b.key,
    value: f.num(b.weight ?? b.pct),
    right: f.eur(b.value_eur, { compact: true }),
  }));
  if (!items.length) return el("div");

  return el(
    "section",
    heading("What it is invested in"),
    bars(items, { format: (v) => f.pct(v, { digits: 1 }) }),
    a.basis
      ? el("p.chart-note", {
          text:
            a.basis === "quote"
              ? "Grouped by each instrument's own attribute."
              : "Grouped by what the funds actually hold.",
        })
      : null,
    caveats(a.caveats),
  );
}

/* --------------------------------------------------------------- goal */

function goalSection(g) {
  if (!f.has(g.target_eur)) return el("div");
  const pct = Math.max(0, Math.min(1, (f.num(g.current_eur) ?? 0) / f.num(g.target_eur)));

  return el(
    "section",
    heading("Goal"),
    el("div.progress", el("span", { style: `width:${(pct * 100).toFixed(1)}%` })),
    el(
      "div.cards",
      card("Target", f.eur(g.target_eur, { compact: true })),
      card("Reached", f.pct(pct, { digits: 1 })),
      card("Still to go", f.eur((f.num(g.target_eur) ?? 0) - (f.num(g.current_eur) ?? 0), { compact: true })),
    ),
    el("p.actions", el("a", { href: "#/settings", text: "Change the target" })),
    caveats(g.caveats),
  );
}

/* ------------------------------------------------------------- shared */

function legendItem(colour, label) {
  return el(
    "span.item",
    el("span.swatch", { style: `background:${colour}` }),
    el("span", { text: label }),
  );
}

function caveats(list) {
  if (!list || !list.length) return el("div");
  return el("ul.caveats", list.map((c) => el("li", { text: c })));
}

export { table, row };
