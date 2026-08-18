/*
 * When you get there, and what would change that.
 *
 * The app has been computing this since the projection engine was written and
 * has never shown it -- the goal card said how much was left and stopped. This
 * is the screen that answers the question the target implies.
 *
 * Three deliberate framings:
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
 * **What you control is separated from what you assume.** Contributions and
 * fees are decisions. A different return is a wish about the world, and a
 * screen that lists them together invites the reader to treat them as equally
 * available -- so assumed levers sit below, under their own heading, and any
 * result using one carries a sentence saying so.
 */

import { el, card, row, heading } from "../dom.js";
import { get, post, del, all, isError } from "../api.js";
import * as f from "../fmt.js";
import { timeChart, toTs, palette } from "../charts.js";
import * as router from "../router.js";

const CONFIDENCES = [
  ["0.50", "Half the time"],
  ["0.75", "Three times in four"],
  ["0.90", "Nine times in ten"],
];
const HEADLINE = "0.75";

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

  view.append(confidenceCards(base));
  view.append(fanChart(base, data.baseline));
  // Before the levers, because the rate the projection assumes comes from
  // here: a plan built on an aspirational savings rate is the commonest way
  // a tool like this flatters its owner.
  if (!isError(data.savings)) view.append(savingsSection(data.savings));
  view.append(leverForm(base));
  if (!isError(data.sensitivity)) view.append(sensitivitySection(data.sensitivity));
  if (!isError(data.pinned)) view.append(pinnedSection(data.pinned));
  view.append(el("p.note", { text: data.baseline.disclosure }));
  view.append(caveats(data.baseline.caveats));
  return view;
}

/* ------------------------------------------------------------ headline */

function confidenceCards(outcome) {
  const cards = CONFIDENCES.map(([key, label]) => {
    const date = outcome.dates[key];
    const months = outcome.months_to_target[key];
    return card(label, date ? f.date(date) : "not within 15 years", {
      cls: key === HEADLINE ? "total" : "",
      sub: months === null || months === undefined ? "on this evidence" : `${months} months`,
    });
  });

  return el(
    "section",
    heading("When the target is reached"),
    el("div.cards", cards),
    el("p.note", {
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

function fanChart(outcome, body) {
  const all = outcome.bands || [];
  if (all.length < 2) return el("div");

  /*
   * Trimmed to the part of the horizon the target lives in.
   *
   * The simulation runs fifteen years because a scenario may need it, but
   * drawing all of it puts a EUR 30M line at the top and squashes the target
   * onto the axis — a chart about compounding, when the question was "when do
   * I reach EUR 250,000". Cut a couple of years past the point where even the
   * unluckiest tenth has arrived.
   */
  const target = outcome.target_eur;
  const arrived = all.findIndex((b) => b.p05 >= target);
  const cutoff =
    arrived === -1 ? all.length : Math.min(all.length, arrived + Math.ceil(24 / 3) + 1);
  const bands = all.slice(0, Math.max(cutoff, 8));
  const trimmed = bands.length < all.length;

  const box = el("div.chart");
  const pal = palette();
  const x = bands.map((b) => toTs(b.date));

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
        { label: "Best 5%", data: bands.map((b) => b.p95), colour: pal.series[0], width: 1 },
        { label: "Worst 5%", data: bands.map((b) => b.p05), colour: pal.series[0], width: 1 },
        { label: "Upper quarter", data: bands.map((b) => b.p75), colour: pal.series[0], width: 1 },
        { label: "Lower quarter", data: bands.map((b) => b.p25), colour: pal.series[0], width: 1 },
        { label: "Middle", data: bands.map((b) => b.p50), colour: pal.series[0], width: 2 },
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

  return el(
    "section",
    heading("The range of outcomes"),
    box,
    el("p.chart-note", {
      text:
        "Each shaded band is a share of resampled histories: the outer covers 90 " +
        "of every 100, the inner covers half. The middle line is not a prediction " +
        "— it is the middle of a spread, and the spread is the answer." +
        (trimmed
          ? " Cut off shortly after the target is reached in every band; the " +
            "simulation itself runs further."
          : ""),
    }),
  );
}

/* -------------------------------------------------------------- levers */

function leverForm(base) {
  const monthly = el("input", { type: "number", step: "50", placeholder: "300" });
  const lumpAmount = el("input", { type: "number", step: "100", placeholder: "5000" });
  const lumpMonth = el("input", { type: "number", step: "1", min: "1", placeholder: "12" });
  const growth = el("input", { type: "number", step: "0.5", placeholder: "2" });
  const fee = el("input", { type: "number", step: "1", placeholder: "-10" });
  const target = el("input", { type: "number", step: "1000", placeholder: String(base.target_eur) });
  const ret = el("input", { type: "number", step: "0.5", placeholder: "1" });
  const name = el("input", { type: "text", placeholder: "Saving more" });
  const pin = el("input", { type: "checkbox" });

  const out = el("div");
  const button = el("button.primary", { type: "submit", text: "See what it changes" });

  const run = async () => {
    button.disabled = true;
    button.textContent = "Running both…";
    out.replaceChildren(el("p.note", { text: "Simulating the baseline and the change…" }));
    try {
      const body = {
        name: name.value.trim() || "Scenario",
        pin: pin.checked,
        monthly_contribution_delta_eur: monthly.value || "0",
        contribution_growth_pct_annual: growth.value ? String(Number(growth.value) / 100) : "0",
        return_delta_annual_pct: ret.value ? String(Number(ret.value) / 100) : "0",
        ter_delta_bps: fee.value ? Number(fee.value) : 0,
        lump_sums:
          lumpAmount.value && lumpMonth.value
            ? [[Number(lumpMonth.value), lumpAmount.value]]
            : [],
      };
      if (target.value) body.target_amount_eur = target.value;
      const result = await post("/api/projection/scenario", body, { paths: 600, years: 15 });
      out.replaceChildren(result_(result));
      if (pin.checked) router.refresh();
    } catch (err) {
      out.replaceChildren(el("p.note.bad", { text: err.message }));
    }
    button.disabled = false;
    button.textContent = "See what it changes";
  };

  const field = (label, node, hint) =>
    el("label.field", el("span", { text: label }), node, hint ? el("span.note", { text: hint }) : null);

  return el(
    "section",
    heading("What if"),
    el(
      "form",
      { onsubmit: (e) => (e.preventDefault(), run()) },
      el("h3", { text: "Things you decide" }),
      el(
        "div.entry",
        field("Extra per month (EUR)", monthly),
        field("One-off amount (EUR)", lumpAmount),
        field("…in how many months", lumpMonth),
        field("Contributions rise (% a year)", growth),
        field("Fee change (basis points)", fee, "negative is cheaper, e.g. -10"),
        field("Different target (EUR)", target),
      ),
      // Visually separated, and labelled, because the difference between a
      // decision and a wish is the most important thing on this screen.
      el("h3", { text: "Things you can only assume" }),
      el(
        "div.entry",
        field("Return, % a year more", ret, "changing this changes nothing about the portfolio"),
      ),
      el("h3", { text: "Keep it?" }),
      el(
        "div.entry",
        field("Name", name),
        el("label.check.wide", pin, el("span", { text: "Pin it, and re-run it as things move" })),
      ),
      el("div.actions", button),
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


function result_(r) {
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
          el("tr", ["Confidence", "As things stand", "With the change", "Difference"].map((t) => el("th", { text: t }))),
        ),
        el(
          "tbody",
          CONFIDENCES.map(([key, label]) =>
            row([
              { text: label, cls: "name" },
              r.baseline.dates[key] ? f.date(r.baseline.dates[key]) : "beyond horizon",
              r.scenario.dates[key] ? f.date(r.scenario.dates[key]) : "beyond horizon",
              { node: monthsDelta(r.months_earlier[key]) },
            ]),
          ),
        ),
      ),
    ),
  );

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
          el("tr", ["Extra per month", "Months earlier", "Months per EUR 100"].map((t) => el("th", { text: t }))),
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
          el("tr", ["Scenario", "Reaches target", "vs today", "", ""].map((t) => el("th", { text: t }))),
        ),
        el(
          "tbody",
          saved.map((s) =>
            row([
              { text: s.name, cls: "name" },
              s.scenario.dates[HEADLINE] ? f.date(s.scenario.dates[HEADLINE]) : "beyond horizon",
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
  const months = (s.months || []).slice(-24);

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
  section.append(
    el("p.note" + (Math.abs(gap) > 0.01 ? ".bad" : ""), {
      text:
        `${f.eur(made.money_you_put_in_eur)} put in plus ${f.eur(made.market_growth_eur)} of ` +
        `market growth is ${f.eur(made.adds_to_eur)}, and the portfolio is worth ` +
        `${f.eur(made.portfolio_value_eur)}.` +
        (Math.abs(gap) > 0.01 ? ` They differ by ${f.eur(gap)}, which should not happen.` : ""),
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
            label: "New money that month",
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
