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
let range = "1D";

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
  view.append(rangePicker(o));
  view.append(headline(o, isError(data.portfolio) ? null : data.portfolio));
  view.append(explainPanel("/api/brief", {}, "Summarise this in words"));
  if (series) view.append(valueChart(o, series));
  view.append(splitColumns(o, isError(data.allocation) ? null : data.allocation));
  view.append(holdingsSection(o));
  if (o.goal) view.append(goalSection(o.goal));
  return view;
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
  const r = o.return;

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
    r.reliable
      ? hintCard(
          "Return",
          "What the investments earned, with money paid in and taken out removed. This is the performance figure.",
          f.pct(r.twr, { sign: true }),
          {
            valueCls: f.tone(r.twr),
            sub:
              r.twr_annualised === null
                ? null
                : `${f.pct(r.twr_annualised, { sign: true })} a year`,
          },
        )
      : hintCard("Return", r.unreliable_because, "—", { cls: "aside", sub: "not reliable" }),
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
  if (!r.reliable) section.append(el("p.note.bad", { text: r.unreliable_because }));
  return section;
}

/* ---------------------------------------------------------------- chart */

function valueChart(o, series) {
  const pts = series.points;
  if (pts.length < 2) return el("div");

  const pal = palette();
  const box = el("div.chart");
  const ddBox = el("div.chart");

  queueMicrotask(() => {
    timeChart(box, {
      x: pts.map((r) => toTs(r.date)),
      height: 340,
      kind: "eur",
      sync: "overview",
      // No forced zero: over a one-month window an axis starting at zero
      // squashes the whole line into the top centimetre.
      series: [
        { label: "What it is worth", data: pts.map((r) => f.num(r.total_value_eur)), fill: 0.14 },
        {
          label: "What you put in",
          data: pts.map((r) => f.num(r.net_contributions_eur)),
          colour: pal.muted,
          width: 1.4,
          dash: [4, 4],
        },
      ],
    });
    timeChart(ddBox, {
      x: pts.map((r) => toTs(r.date)),
      height: 200,
      kind: "pct",
      sync: "overview",
      series: [
        {
          label: "Below the previous peak",
          data: pts.map((r) => f.num(r.drawdown_pct)),
          colour: pal.loss,
          fill: 0.14,
        },
      ],
    });
  });

  return el(
    "section",
    heading("Value against money put in"),
    box,
    el("p.chart-note", {
      text: "The gap between the lines is everything the portfolio earned.",
    }),
    el("h3", { text: "How far below its previous peak" }),
    ddBox,
  );
}

/* ------------------------------------------------------------- columns */

/*
 * Two columns. A six-row bar chart spanning the full page width is mostly
 * whitespace with a label at either end.
 */
function splitColumns(o, allocation) {
  const cols = el("div.cols");
  const a = o.attribution;

  if (a) {
    cols.append(
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
      cols.append(
        el(
          "section",
          heading("What it is invested in"),
          bars(items, { format: (v) => f.pct(v, { digits: 1 }) }),
        ),
      );
    }
  }
  return cols;
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
  const when = el("div.cards");
  box.append(when, el("p.note", el("a", { href: "#/plan", text: "Change the plan" })));

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
      );
    } catch {
      when.replaceChildren(el("p.note", { text: "Not enough history to project yet." }));
    }
  });

  return box;
}
