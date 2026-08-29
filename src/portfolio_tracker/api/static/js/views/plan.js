/*
 * When you get there, and what would change that.
 *
 * The app has been computing this since the projection engine was written and
 * has never shown it -- the goal card said how much was left and stopped. This
 * is the screen that answers the question the target implies.
 *
 * Four deliberate framings:
 *
 * The headline is **75% confidence**, not the median. A 50/50 date is a coin
 * flip and the wrong thing to plan a house purchase around, so the middle card
 * is emphasised and the other two sit beside it for shape.
 *
 * There is **no single projected value**. The fan shows a range; the summary
 * shows probabilities and a shortfall. A p50 line exists inside the chart
 * because a fan needs a middle, but no number on this page is presented as
 * "what you will have".
 *
 * **What you control is separated from what you assume.** Contributions are a
 * decision. A different return is a wish about the world, and a screen that
 * lists them together invites the reader to treat them as equally available --
 * so assumed levers sit below, under their own heading, and any result using
 * one carries a sentence saying so.
 *
 * **Every field says what it would be, not how much it would change.** "Save
 * EUR 1,500 a month" can be checked against a bank statement. "Save EUR 300 a
 * month more" cannot be checked against anything unless the reader already
 * remembers, and trusts, the figure the tool measured. So each box arrives
 * filled in with what is true today, and editing it states a new truth.
 */

import { el, card, row, heading } from "../dom.js";
import { get, post, put, del, all, isError } from "../api.js";
import * as f from "../fmt.js";
import { timeChart, toTs, palette } from "../charts.js";
import * as router from "../router.js";

const CONFIDENCES = [
  ["0.50", "Half the time"],
  ["0.75", "Three times in four"],
  ["0.90", "Nine times in ten"],
];
const HEADLINE = "0.75";

/** The measured windows, in the order they should be read. */
const WINDOWS = [
  ["3m", "Last 3 months"],
  ["6m", "Last 6 months"],
  ["12m", "Last 12 months"],
  ["since_start", "Since records begin"],
];

export async function render() {
  const data = await all({
    baseline: get("/api/projection", { paths: 600, years: 15 }),
    sensitivity: get("/api/projection/sensitivity", { paths: 300 }),
    pinned: get("/api/projection/scenarios", { paths: 300 }),
    savings: get("/api/savings"),
  });

  if (isError(data.baseline)) throw data.baseline;
  const base = data.baseline.baseline;

  const view = el(
    "div",
    el("h1", { text: "Plan" }),
    el("p.sub", {
      text: `Reaching ${f.eur(base.target_eur, { compact: true })}, from where you are today.`,
    }),
  );

  view.append(standingAssumptions(base, data.baseline));
  view.append(confidenceCards(base, data.baseline));
  view.append(fanChart(base, data.baseline));
  // Before the levers, because the rate the projection assumes comes from
  // here: a plan built on an aspirational savings rate is the commonest way
  // a tool like this flatters its owner.
  if (!isError(data.savings)) view.append(savingsSection(data.savings));
  view.append(leverForm(base, data.baseline));
  if (!isError(data.sensitivity)) view.append(sensitivitySection(data.sensitivity));
  if (!isError(data.pinned)) view.append(pinnedSection(data.pinned));
  view.append(el("p.note", { text: data.baseline.disclosure }));
  view.append(caveats(data.baseline.caveats));
  return view;
}

/* --------------------------------------------------------- assumptions */

/*
 * The two numbers every date below rests on, stated before the dates.
 *
 * They were previously findable only by scrolling past the answer into the
 * form that produced it, which is the wrong way round: a reader who takes the
 * headline date and leaves has taken a conditional figure without its
 * conditions. Each says where it came from, because "measured from your
 * deposits" and "assumed, because there is not enough history" are different
 * kinds of claim and the difference is the whole point.
 */
/*
 * The assumed return, with a way to change it.
 *
 * 4% is the tool's placeholder -- the middle of the spread this codebase
 * already publishes as the range worth thinking in -- and nobody agreed to
 * it. A figure the owner cannot change is one they are entitled to distrust,
 * and the right answer to "who approved this" is "you do, here".
 */
function assumedReturnCard(returns) {
  const input = el("input", {
    type: "number",
    step: "any",
    value: (returns.implied_annual * 100).toFixed(2),
    style: "width:5.5rem",
  });
  const save = async () => {
    if (!input.value) return;
    try {
      await put("/api/projection/assumed-return", { annual: Number(input.value) / 100 });
      router.refresh();
    } catch (err) {
      window.alert(err.message);
    }
  };
  return el(
    "div.card",
    el("span.k", { text: "Return a year" }),
    el(
      "div.v.warn.inline",
      input,
      el("span", { text: "%" }),
      el("button.small", { type: "button", text: "Set", onclick: save }),
    ),
    el("div.sub2", {
      text: returns.assumed_is_default
        ? "assumed by the tool, not measured — change it"
        : "your figure, not measured",
    }),
  );
}

function standingAssumptions(base, body) {
  const returns = body.returns;
  const stated = returns && returns.from_history === false;

  return el(
    "section.standing",
    el(
      "div.cards",
      card("Saving per month", f.eur(base.monthly_contribution_eur), {
        sub: "measured from your last 12 months",
      }),
      stated ? assumedReturnCard(returns) : card("Return a year", f.pct(returns.implied_annual), {
        sub: "resampled from your holdings",
      }),
      card("Target", f.eur(base.target_eur, { compact: true }), {
        sub: "what the dates below are measured against",
      }),
    ),
    el("p.note", {
      text:
        "Every date on this page follows from these three. Change any of them " +
        "in “What if” below.",
    }),
  );
}

/* ------------------------------------------------------------ headline */

/*
 * A projected date is only ever accurate to a month, so it is shown as one.
 * "18 Mar 2028" reads as a diary entry somebody could hold the tool to; "Mar
 * 2028" reads as what it is -- the month a resampled simulation crossed a
 * line. The precision that was there was not information, it was decoration.
 */
function confidenceCards(outcome, body) {
  const cards = CONFIDENCES.map(([key, label]) => {
    const date = outcome.dates[key];
    const months = outcome.months_to_target[key];
    return card(label, date ? f.month(date) : "not within 15 years", {
      cls: key === HEADLINE ? "total" : "",
      sub: months === null || months === undefined ? "on this evidence" : `${months} months`,
    });
  });

  /*
   * Three identical dates look like a broken feature, and the explanation was
   * sitting at the foot of the page where nobody would connect it.
   *
   * They coincide when the price history is barely longer than the 252-day
   * blocks the simulation draws from: every path is then built from nearly the
   * same overlapping windows, so the paths barely differ and the percentiles
   * land on one month. The spread is real, but far too narrow to believe --
   * which is a statement about how little history there is, not about how
   * certain the date is. Saying that here is the difference between an
   * apparent bug and an honest limit.
   */
  const dates = CONFIDENCES.map(([key]) => outcome.dates[key]);
  const identical = dates.every((d) => d === dates[0]) && dates[0];
  const days = body?.returns?.history_days;

  return el(
    "section",
    heading("When the target is reached"),
    el("div.cards", cards),
    identical
      ? el("p.note.warn", {
          text:
            "All three are the same date, and that is a warning rather than a " +
            "result. " +
            (days
              ? `There are only ${days} days of price history to resample, barely ` +
                "more than the one-year blocks the simulation draws from, "
              : "There is too little price history to resample, ") +
            "so every simulated path ends up looking like every other one. Read " +
            "this as a single rough estimate, not as three levels of confidence. " +
            "It will separate as the price history grows.",
        })
      : el("p.note", {
          text:
            "Read the middle one. “Three times in four” means that in 75 out of 100 " +
            "resampled histories the target had been reached by then — it is not a " +
            "promise, and the other two show how wide the spread is.",
        }),
    outcome.shortfall_p10_eur > 0
      ? el("p.note.warn", {
          text:
            `In the worst tenth of outcomes you are ${f.eur(outcome.shortfall_p10_eur)} short ` +
            "at the end of the horizon. That figure is more useful than any single " +
            "projected value, because it is the one you would have to plan around.",
        })
      : null,
  );
}

/* ---------------------------------------------------------------- fan */

/**
 * How far of the horizon is worth drawing.
 *
 * The simulation runs fifteen years because a scenario may need it. Drawing
 * all fifteen puts a EUR 30M line at the top, squashes the target onto the
 * axis, and answers a question about compounding when the question asked was
 * "when do I reach EUR 250,000". Stop a year after even the unluckiest
 * twentieth has arrived; failing that, a year after the middle arrives.
 */
function horizonCut(bands, target) {
  const arrives = (key) => bands.findIndex((b) => b[key] >= target);
  const YEAR = 4; // bands are quarterly

  for (const [key, pad] of [
    ["p05", YEAR],
    ["p25", YEAR],
    ["p50", YEAR * 2],
  ]) {
    const at = arrives(key);
    if (at !== -1) return Math.min(bands.length, at + pad + 1);
  }
  return bands.length;
}

function fanChart(outcome, body) {
  const all = outcome.bands || [];
  if (all.length < 2) return el("div");

  const target = outcome.target_eur;
  const bands = all.slice(0, Math.max(horizonCut(all, target), 8));
  const trimmed = bands.length < all.length;

  const box = el("div.chart");
  const pal = palette();
  const x = bands.map((b) => toTs(b.date));

  /*
   * Five lines in one colour is five lines nobody can tell apart, and the
   * legend then names things the reader cannot find. Each band gets its own
   * weight instead: the outer edges recede into grey, the middle half is
   * coloured, and the median is the only heavy line on the chart.
   */
  queueMicrotask(() =>
    timeChart(box, {
      x,
      height: 320,
      kind: "eur",
      // 0 and 1 shade the outer band, 2 and 3 the inner one, so the darker
      // middle is where half the outcomes fall.
      fillBetween: [
        [0, 1],
        [2, 3],
      ],
      series: [
        { label: "Best 5%", data: bands.map((b) => b.p95), colour: pal.muted, width: 1 },
        { label: "Worst 5%", data: bands.map((b) => b.p05), colour: pal.muted, width: 1 },
        { label: "Upper quarter", data: bands.map((b) => b.p75), colour: pal.series[1], width: 1 },
        { label: "Lower quarter", data: bands.map((b) => b.p25), colour: pal.series[1], width: 1 },
        { label: "Middle", data: bands.map((b) => b.p50), colour: pal.accent, width: 2.5 },
        {
          label: "Target",
          data: bands.map(() => target),
          colour: pal.gain,
          width: 1.5,
          dash: [5, 4],
        },
      ],
    }),
  );

  // A fan drawn from too little history is a fan with nothing in it. Better
  // to name that than to let the reader admire how precise it looks.
  const last = bands[bands.length - 1];
  const width = last.p95 > 0 ? (last.p95 - last.p05) / last.p95 : 0;

  return el(
    "section",
    heading("The range of outcomes"),
    box,
    el("p.chart-note", {
      text:
        "The grey band covers 90 of every 100 resampled histories; the coloured " +
        "one covers the middle half. The blue line is not a prediction — it is " +
        "the middle of a spread, and the spread is the answer. The green dashes " +
        `are your ${f.eur(target, { compact: true })} target.` +
        (trimmed
          ? ` Drawn to ${f.month(last.date)}, about a year after ` +
            "the target is reached in every band; the simulation itself runs to " +
            `${f.month(all[all.length - 1].date)}.`
          : ""),
    }),
    width < 0.1
      ? el("p.note.warn", {
          text:
            `The bands are only ${f.pct(width, { digits: 0 })} apart, which is far ` +
            "narrower than markets are. There is not enough price history yet for " +
            "the simulation to draw genuinely different futures, so this reads as " +
            "more certain than anything about a portfolio can be.",
        })
      : null,
  );
}

/* -------------------------------------------------------------- levers */

/** Whole months between an ISO date and a "YYYY-MM" from a month input. */
function monthsBetween(fromIso, yearMonth) {
  const [fy, fm] = fromIso.slice(0, 7).split("-").map(Number);
  const [ty, tm] = String(yearMonth).slice(0, 7).split("-").map(Number);
  return (ty - fy) * 12 + (tm - fm);
}

/** "YYYY-MM" a given number of months after an ISO date. */
function monthAfter(fromIso, months) {
  const d = new Date(`${fromIso.slice(0, 10)}T00:00:00`);
  d.setMonth(d.getMonth() + months);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

/*
 * What the projection is already assuming, next to what actually happened.
 *
 * The old form asked for "Return, % a year more", which is unanswerable: more
 * than what? The bootstrap never states a return -- it resamples this
 * portfolio's own price history -- so the figure it works out to had to be
 * computed and put on the screen before a reader could sensibly move it.
 *
 * The measured windows sit beside it as evidence, each with the number of days
 * behind it, because "+24% a year" from twelve weeks of data and "+24% a year"
 * from four years are not the same claim.
 */
function returnEvidence(returns, onPick) {
  if (!returns) return el("div");

  const rows = WINDOWS.map(([key, label]) => {
    const w = returns.measured?.[key];
    if (!w || w.annualised === null) {
      return row([{ text: label, cls: "name" }, "—", "not enough history", ""]);
    }
    return row([
      { text: label, cls: "name" },
      { text: f.pct(w.period, { sign: true }), cls: f.tone(w.period) },
      { text: f.pct(w.annualised, { sign: true }), cls: f.tone(w.annualised) },
      {
        // type="button": these live inside the form, and a bare <button>
        // there defaults to submit -- picking a figure would run the
        // simulation instead of filling the box.
        node: el("button.small", {
          type: "button",
          text: "Use this",
          onclick: () => onPick(w.annualised),
        }),
      },
    ]);
  });

  const box = el(
    "div.panel",
    el(
      "p",
      el("span", { text: "The projection is using " }),
      el("strong", { text: `${f.pct(returns.implied_annual)} a year` }),
      el("span", {
        text: returns.from_history
          ? `, which is what ${returns.history_days} days of your own holdings' prices ` +
            `(from ${f.month(returns.history_from)}) average out to. It is not typed in ` +
            "anywhere — it falls out of resampling that history."
          : `, a stated figure, not a measured one. Resampling your own history ` +
            `works out at ${f.pct(returns.resampled_annual)} a year, but there are only ` +
            `${returns.history_days} days of it (from ${f.month(returns.history_from)}) — ` +
            "one stretch of good weather, not a range of outcomes. Planning years " +
            "on that number would be indefensible, so a modest one stands in until " +
            "there is enough history. Change it below if you disagree.",
      }),
    ),
    el(
      "div.wrap",
      el(
        "table",
        el(
          "thead",
          el(
            "tr",
            ["Window", "Return over it", "Per year", ""].map((t) => el("th", { text: t })),
          ),
        ),
        el("tbody", rows),
      ),
    ),
    el("p.note", {
      text:
        "“Per year” scales a short window up to twelve months. Over three " +
        "months that magnifies whatever happened to happen, so read it beside " +
        "the window it came from rather than on its own.",
    }),
  );

  if (returns.measured_reliable === false) {
    box.append(
      el("p.note.bad", {
        text:
          "These returns are overstated. Purchases exist that no recorded money " +
          "paid for, and unit accounting reads that money as investment return. " +
          "Fix it on the Trades page before using any of these figures as an " +
          "assumption.",
      }),
    );
  }
  return box;
}

/*
 * What was actually paid in, over each window, average beside median.
 *
 * The gap between the two is the reason this panel exists. One EUR 22,686
 * transfer made two thirds of a six-month average on this ledger, and a plan
 * built on that average quietly assumes the transfer repeats every month for
 * fifteen years. The median month says what a normal month looked like, and
 * seeing them side by side is what makes the choice between them a choice.
 */
function savingsEvidence(contributions, onPick) {
  if (!contributions) return el("div");

  const labels = [
    ["6m", "Last 6 months"],
    ["12m", "Last 12 months"],
    ["24m", "Last 2 years"],
  ];

  const rows = labels.flatMap(([key, label]) => {
    const w = contributions[key];
    if (!w) return [];
    const lumpy = w.median_monthly_eur !== null && w.average_monthly_eur > w.median_monthly_eur * 2;
    return [
      row([
        { text: label, cls: "name" },
        { text: f.eur(w.average_monthly_eur) },
        { text: f.eur(w.median_monthly_eur), cls: lumpy ? "warn" : "" },
        {
          node: el(
            "span.actions",
            el("button.small", {
              type: "button",
              text: "Average",
              onclick: () => onPick(w.average_monthly_eur),
            }),
            // A typical month of nothing is a true and useful fact -- you do
            // not pay in every month -- but it is not a savings rate anybody
            // means to plan on, so it is shown and not offered.
            el("button.small", {
              type: "button",
              text: "Typical",
              disabled: !w.median_monthly_eur,
              onclick: () => onPick(w.median_monthly_eur),
            }),
          ),
        },
      ]),
    ];
  });

  if (!rows.length) return el("div");

  return el(
    "div.panel",
    el(
      "div.wrap",
      el(
        "table",
        el(
          "thead",
          el(
            "tr",
            ["Window", "Average month", "Typical month", ""].map((t) => el("th", { text: t })),
          ),
        ),
        el("tbody", rows),
      ),
    ),
    el("p.note", {
      text:
        "“Typical” is the middle month of the window. Where it sits well below " +
        "the average, one large transfer is carrying the average — and a plan " +
        "built on that number assumes the transfer happens again every month.",
    }),
  );
}

function leverForm(base, body) {
  const returns = body.returns;
  const contributions = body.contributions;
  const asOf = body.as_of;

  /*
   * Every box starts holding what is true today rather than empty. An empty
   * box beside the words "per month" is a question the reader has to answer
   * before they can experiment; a filled one is a statement they can edit.
   */
  const target = el("input", {
    type: "number",
    step: "any",
    min: "0",
    value: String(Math.round(base.target_eur)),
  });
  const monthly = el("input", {
    // step="any", not 50. A stepped number input silently refuses anything
    // off the grid, so "1281" was rejected while "1281" is exactly the sort
    // of figure a real savings rate is.
    type: "number",
    step: "any",
    min: "0",
    value: String(Math.round(base.monthly_contribution_eur)),
  });
  const lumpAmount = el("input", { type: "number", step: "any", min: "0", placeholder: "5000" });
  const lumpWhen = el("input", { type: "month", min: monthAfter(asOf, 1) });
  const growth = el("input", { type: "number", step: "any", placeholder: "2" });
  const ret = el("input", {
    type: "number",
    step: "any",
    value: returns ? (returns.implied_annual * 100).toFixed(1) : "",
  });
  const name = el("input", { type: "text", placeholder: "Saving more" });
  const pin = el("input", { type: "checkbox" });

  const out = el("div");
  const button = el("button.primary", { type: "submit", text: "See what it changes" });

  const reset = () => {
    target.value = String(Math.round(base.target_eur));
    monthly.value = String(Math.round(base.monthly_contribution_eur));
    lumpAmount.value = "";
    lumpWhen.value = "";
    growth.value = "";
    if (returns) ret.value = (returns.implied_annual * 100).toFixed(1);
    out.replaceChildren();
  };

  const run = async () => {
    button.disabled = true;
    button.textContent = "Running both…";
    out.replaceChildren(el("p.note", { text: "Simulating the baseline and the change…" }));
    try {
      const months = lumpWhen.value ? monthsBetween(asOf, lumpWhen.value) : null;
      if (lumpAmount.value && (months === null || months < 1)) {
        throw new Error("Pick a month for the one-off amount, later than this one.");
      }
      const request = {
        name: name.value.trim() || "Scenario",
        pin: pin.checked,
        contribution_growth_pct_annual: growth.value ? String(Number(growth.value) / 100) : "0",
        lump_sums: lumpAmount.value ? [[months, lumpAmount.value]] : [],
      };
      // Sent only when it differs from the measured or implied figure, so a
      // scenario that changes nothing is recorded as changing nothing.
      if (monthly.value && Number(monthly.value) !== Math.round(base.monthly_contribution_eur)) {
        request.monthly_contribution_eur = monthly.value;
      }
      if (ret.value && returns && Number(ret.value) !== Number((returns.implied_annual * 100).toFixed(1))) {
        request.return_annual_pct = String(Number(ret.value) / 100);
      }
      if (target.value && Number(target.value) !== Math.round(base.target_eur)) {
        request.target_amount_eur = target.value;
      }
      const result = await post("/api/projection/scenario", request, { paths: 600, years: 15 });
      out.replaceChildren(result_(result, base));
      if (pin.checked) router.refresh();
    } catch (err) {
      out.replaceChildren(el("p.note.bad", { text: err.message }));
    }
    button.disabled = false;
    button.textContent = "See what it changes";
  };

  const field = (label, node, hint) =>
    el(
      "label.field",
      el("span", { text: label }),
      node,
      hint ? el("span.note", { text: hint }) : null,
    );

  return el(
    "section",
    heading("What if"),
    el(
      "form",
      { onsubmit: (e) => (e.preventDefault(), run()) },
      // The target comes first because everything under it is measured
      // against it. Asking for it last made the whole form read as a set of
      // adjustments to something the reader had not yet been shown.
      el("h3", { text: "What you are aiming at" }),
      el("div.entry", field("Target (EUR)", target, "what the dates below are measured against")),
      el("h3", { text: "Things you decide" }),
      savingsEvidence(contributions, (amount) => {
        monthly.value = String(Math.round(amount));
      }),
      el(
        "div.entry",
        field("Saving per month (EUR)", monthly, "starts at your last 12 months' average"),
        field("One-off amount (EUR)", lumpAmount),
        field("…paid in", lumpWhen, "which month it arrives"),
        field("Contributions rise (% a year)", growth),
      ),
      // Visually separated, and labelled, because the difference between a
      // decision and a wish is the most important thing on this screen.
      el("h3", { text: "Things you can only assume" }),
      returnEvidence(returns, (annual) => {
        ret.value = (annual * 100).toFixed(1);
      }),
      el(
        "div.entry",
        field("Return (% a year)", ret, "changing this changes nothing about the portfolio"),
      ),
      el("h3", { text: "Keep it?" }),
      el(
        "div.entry",
        field("Name", name),
        el("label.check.wide", pin, el("span", { text: "Pin it, and re-run it as things move" })),
      ),
      el("div.actions", button, el("button", { type: "button", text: "Reset", onclick: reset })),
    ),
    out,
  );
}

/** "4 months earlier" / "2 months later" / "no change" / "—". */
function monthsDelta(months) {
  if (months === null || months === undefined) return el("span.muted", { text: "—" });
  if (months === 0) return el("span.muted", { text: "no change" });
  return el("span", {
    class: months > 0 ? "gain" : "loss",
    text: `${Math.abs(months)} months ${months > 0 ? "earlier" : "later"}`,
  });
}

/*
 * Everything the answer rests on, printed with the answer.
 *
 * A date with no assumptions beside it is a number somebody will quote back
 * six months later having forgotten it was conditional. Both columns are
 * shown -- what it was, what it became -- so a reader can see at a glance
 * which single thing they changed, and confirm they did not change two.
 */
function assumptions(r, base) {
  const l = r.levers || {};
  const asked = (value, fallback) => (value === null || value === undefined ? fallback : value);

  const lines = [
    ["Target", f.eur(base.target_eur), f.eur(r.scenario.target_eur)],
    [
      "Saving per month",
      f.eur(r.baseline.monthly_contribution_eur),
      f.eur(r.scenario.monthly_contribution_eur),
    ],
    [
      "Return assumed",
      f.pct(r.returns?.implied_annual),
      l.return_annual_pct === null || l.return_annual_pct === undefined
        ? f.pct(r.returns?.implied_annual)
        : f.pct(Number(l.return_annual_pct)),
    ],
    [
      "One-off payments",
      "none",
      (l.lump_sums || []).length
        ? l.lump_sums
            .map(([m, a]) => `${f.eur(Number(a))} in ${f.month(monthAfter(r.as_of, Number(m)))}`)
            .join(", ")
        : "none",
    ],
    [
      "Contributions rise",
      "0.00% a year",
      `${f.pct(Number(asked(l.contribution_growth_pct_annual, 0)))} a year`,
    ],
    ["Horizon", `${r.baseline.horizon_months} months`, `${r.scenario.horizon_months} months`],
  ];

  return el(
    "details.assumptions",
    { open: true },
    el("summary", { text: "Everything this answer assumes" }),
    el(
      "div.wrap",
      el(
        "table",
        el(
          "thead",
          el(
            "tr",
            ["", "As things stand", "In this scenario"].map((t) => el("th", { text: t })),
          ),
        ),
        el(
          "tbody",
          lines.map(([label, was, now]) =>
            row([
              { text: label, cls: "name" },
              { text: was },
              { text: now, cls: was === now ? "muted" : "" },
            ]),
          ),
        ),
      ),
    ),
    el("p.note", {
      text:
        `Both runs start from the portfolio as it stood on ${f.date(r.as_of)} and draw ` +
        `the identical sequence of return blocks (seed ${r.seed}), so the difference ` +
        "between them is the change and not the sampling." +
        (r.returns
          ? ` Returns are resampled from ${r.returns.history_days} days of price ` +
            `history starting ${f.month(r.returns.history_from)}.`
          : ""),
    }),
  );
}

function result_(r, base) {
  const earlier = r.months_earlier[HEADLINE];
  const box = el("div.panel");

  box.append(
    el("h3", { text: r.name }),
    earlier === null || earlier === undefined
      ? el("p.note.warn", {
          text:
            "The target is not reached within the horizon in three of four outcomes, " +
            "either before or after this change — so there is no difference in months " +
            "to report. Extrapolating past the horizon would be inventing the answer.",
        })
      : el(
          "p",
          el("strong", {
            text:
              earlier === 0
                ? "No change to the date."
                : `${Math.abs(earlier)} months ${earlier > 0 ? "earlier" : "later"}.`,
          }),
          r.efficiency_months_per_100
            ? el("span", {
                text: ` That is ${r.efficiency_months_per_100} months for every EUR 100 a month.`,
              })
            : null,
        ),
  );

  box.append(
    el(
      "div.wrap",
      el(
        "table",
        el(
          "thead",
          el(
            "tr",
            ["Confidence", "As things stand", "With the change", "Difference"].map((t) =>
              el("th", { text: t }),
            ),
          ),
        ),
        el(
          "tbody",
          CONFIDENCES.map(([key, label]) =>
            row([
              { text: label, cls: "name" },
              r.baseline.dates[key] ? f.month(r.baseline.dates[key]) : "beyond horizon",
              r.scenario.dates[key] ? f.month(r.scenario.dates[key]) : "beyond horizon",
              { node: monthsDelta(r.months_earlier[key]) },
            ]),
          ),
        ),
      ),
    ),
  );

  box.append(assumptions(r, base));

  if (r.assumption_warning) {
    box.append(el("p.note.warn", { text: r.assumption_warning }));
  }
  if (r.caveats?.length) {
    box.append(el("ul.caveats", r.caveats.map((c) => el("li", { text: c }))));
  }
  return box;
}

/* --------------------------------------------------------- sensitivity */

function sensitivitySection(body) {
  const steps = (body.steps || []).filter((s) => s.months_earlier_75 !== null);
  if (!steps.length) return el("div");

  // A table of zeros is worse than no table: it looks like a broken feature
  // rather than the real answer, which is that the target is close enough
  // that saving more cannot bring it much closer.
  if (steps.every((s) => s.months_earlier_75 === 0)) {
    return el(
      "section",
      heading("What each extra hundred buys"),
      el("p.note", {
        text:
          "Nothing measurable. You are close enough to the target, and already " +
          "contributing enough, that saving more does not bring the date forward " +
          "by a whole month. Raising the target on the form above will show where " +
          "extra contributions start to matter again.",
      }),
    );
  }

  return el(
    "section",
    heading("What each extra hundred buys"),
    el(
      "div.wrap",
      el(
        "table",
        el(
          "thead",
          el(
            "tr",
            ["Extra per month", "Months earlier", "Months per EUR 100"].map((t) =>
              el("th", { text: t }),
            ),
          ),
        ),
        el(
          "tbody",
          steps.map((s) =>
            row([
              { text: f.eur(s.monthly_delta_eur, { compact: true }), cls: "name" },
              s.months_earlier_75 === null ? "—" : `${s.months_earlier_75}`,
              { text: s.efficiency_months_per_100 ?? "—", cls: "gain" },
            ]),
          ),
        ),
      ),
    ),
    el("p.note", { text: body.note }),
  );
}

/* -------------------------------------------------------------- pinned */

function pinnedSection(body) {
  const saved = body.scenarios || [];
  if (!saved.length) return el("div");

  return el(
    "section",
    heading("Kept scenarios"),
    el(
      "div.wrap",
      el(
        "table",
        el(
          "thead",
          el(
            "tr",
            ["Scenario", "Reaches target", "vs today", "", ""].map((t) => el("th", { text: t })),
          ),
        ),
        el(
          "tbody",
          saved.map((s) =>
            row([
              { text: s.name, cls: "name" },
              s.scenario.dates[HEADLINE] ? f.month(s.scenario.dates[HEADLINE]) : "beyond horizon",
              {
                text:
                  s.months_earlier[HEADLINE] === null
                    ? "—"
                    : `${s.months_earlier[HEADLINE]} months earlier`,
                cls: s.months_earlier[HEADLINE] > 0 ? "gain" : "",
              },
              {
                node: s.lever_kinds.includes("assumption")
                  ? el("span.tag.warn", { text: "assumed", title: s.assumption_warning })
                  : el("span.tag.ok", { text: "your decision" }),
              },
              { node: forgetButton(s) },
            ]),
          ),
        ),
      ),
    ),
    el("p.note", {
      text:
        "Re-run every time you open this page, against the portfolio as it is " +
        "now. Only the question is stored — a saved answer would be a figure " +
        "that was true in March.",
    }),
  );
}

function forgetButton(scenario) {
  return el("button", {
    text: "Forget",
    onclick: async () => {
      try {
        await del(`/api/projection/scenarios/${scenario.id}`);
        router.refresh();
      } catch (err) {
        window.alert(err.message);
      }
    },
  });
}

function caveats(list) {
  if (!list?.length) return el("div");
  return el("ul.caveats", list.map((c) => el("li", { text: c })));
}

/* ------------------------------------------------------------- savings */

/*
 * What you actually put aside, as opposed to what moved.
 *
 * The three-way split is the point. "You are worth EUR 176,733" says nothing
 * about whether that came from saving or from markets, and the two have
 * completely different implications for what to do next. Income sits outside
 * the sum deliberately -- dividends land in cash, which the headline value
 * excludes, and folding them in would double-count whatever was reinvested.
 */
function savingsSection(s) {
  const made = s.made_of || {};
  // From the first month that has anything in it. A fixed 24-month window
  // padded the chart with empty months from before the records begin.
  const all = s.months || [];
  const firstReal = all.findIndex((m) => f.num(m.net_new_money_eur) || f.num(m.value_eur));
  const months = firstReal === -1 ? all : all.slice(firstReal);

  const section = el(
    "section",
    heading("What you actually saved"),
    el(
      "div.cards",
      card("Last 12 months", f.eur(s.trailing_12m_eur), {
        cls: "total",
        sub: `${f.eur(s.average_monthly_12m_eur)} a month`,
      }),
      card("This year", f.eur(s.year_to_date_eur)),
      card("Since the start", f.eur(made.money_you_put_in_eur)),
      // Its own card, not folded into either neighbour. Counted as saving it
      // flatters the habit; counted as growth it invents a return.
      card("Money already yours", f.eur(made.money_you_already_had_eur), {
        cls: "aside",
        sub: "brought in when an account joined",
      }),
      card("Market growth", f.eur(made.market_growth_eur), {
        valueCls: f.tone(made.market_growth_eur),
      }),
      card("Income received", f.eur(s.income_received_eur), {
        cls: "aside",
        sub: "held as cash, outside the total",
      }),
    ),
    el("p.note", { text: s.definition }),
  );

  // The identity, shown rather than asserted. A gap means a movement was
  // classified as something this does not account for.
  const gap = f.num(made.gap_eur) ?? 0;
  const already = f.num(made.money_you_already_had_eur) ?? 0;
  const cash = f.num(made.uninvested_cash_eur) ?? 0;
  section.append(
    el("p.note" + (Math.abs(gap) > 0.01 ? ".bad" : ""), {
      text:
        `${f.eur(made.money_you_put_in_eur)} put in` +
        // Named in the sum, not just in the total. Left out, the sentence
        // states an addition that does not add up, which reads as a bug in
        // the very line whose job is to prove there is not one.
        (already ? `, plus ${f.eur(made.money_you_already_had_eur)} you already had` : "") +
        `, plus ${f.eur(made.market_growth_eur)} of market growth, is ` +
        `${f.eur(made.adds_to_eur)}.` +
        // The sum closes on holdings *and* uninvested cash. Naming the cash
        // is what makes the two figures reconcile on the page instead of
        // looking like a rounding error nobody explained.
        (Math.abs(cash) > 0.01
          ? ` That is ${f.eur(made.portfolio_value_eur)} of holdings and deposits, ` +
            `plus ${f.eur(made.uninvested_cash_eur)} of cash not yet invested.`
          : ` That is what the accounts hold.`) +
        (Math.abs(gap) > 0.01 ? ` A gap of ${f.eur(gap)} remains, which should not happen.` : ""),
    }),
  );

  if (months.length > 1) {
    const box = el("div.chart");
    const pal = palette();
    queueMicrotask(() =>
      timeChart(box, {
        x: months.map((m) => toTs(`${m.period}-01`)),
        height: 200,
        kind: "eur",
        zero: true,
        series: [
          {
            label: "Money you paid in",
            data: months.map((m) => f.num(m.net_new_money_eur)),
            colour: pal.series[1],
            fill: 0.15,
          },
        ],
      }),
    );
    section.append(el("h3", { text: "Month by month" }), box);
  }

  section.append(caveats(s.caveats));
  return section;
}
