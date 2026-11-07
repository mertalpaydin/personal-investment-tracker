/*
 * German tax, in English.
 *
 * The engine underneath was never wrong; it was unreadable. Every statutory
 * noun now appears as a plain-English phrase with the German after it in
 * parentheses, and every one of them carries the server's own one-line
 * explanation as a tooltip. The German stays because it is what the bank's
 * annual certificate says, and reconciling against that document is the whole
 * point of these figures.
 *
 * The other change in kind: when the tool refuses to compute something -- and
 * it refuses rather than showing zero, correctly -- the refusal now arrives
 * with the form that fixes it.
 */

import { el, card, row, heading } from "../dom.js";
import { get, put, all, isError } from "../api.js";
import * as f from "../fmt.js";
import { bars } from "../bars.js";
import * as router from "../router.js";
import { explainPanel } from "../narrative.js";

let year = new Date().getFullYear();

export async function render({ query }) {
  if (query.get("year")) year = Number(query.get("year"));

  const data = await all({
    allowance: get("/api/tax/allowance", { year }),
    vap: get("/api/tax/vorabpauschale", { year }),
    harvest: get("/api/scenario/harvest", { year }),
    constants: get("/api/tax/constants"),
  });

  const view = el(
    "div",
    el("h1", { text: "Tax" }),
    el("p.sub", { text: `German capital-gains position for ${year}.` }),
    yearPicker(),
  );

  // A missing government base rate is the one failure that blanks this whole
  // screen, and it is fixable by typing one published number.
  if (isError(data.allowance) && data.allowance.isRefusal) {
    view.append(missingConstant(data.allowance, data.constants));
    return view;
  }
  if (isError(data.allowance)) throw data.allowance;

  view.append(allowanceSection(data.allowance));
  view.append(explainPanel("/api/tax/explain", { year }, "Explain my tax position"));
  view.append(lossSection(data.allowance));
  if (!isError(data.vap)) view.append(advanceChargeSection(data.vap));
  if (!isError(data.harvest)) view.append(harvestSection(data.harvest));
  view.append(scopeSection(data.allowance));
  if (!isError(data.constants)) view.append(constantsSection(data.constants));

  view.append(
    el("p.note", { text: data.allowance.disclaimer }),
    el("p.note", el("a", { href: "#/settings", text: "Where these rates come from" })),
  );
  return view;
}

/* --------------------------------------------------------------- chrome */

function yearPicker() {
  const now = new Date().getFullYear();
  const select = el("select", {
    onchange: (e) => router.go(`#/tax?year=${e.target.value}`),
  });
  for (let y = now + 1; y >= now - 4; y--) {
    select.append(el("option", { value: String(y), selected: y === year, text: String(y) }));
  }
  return el("form.row", el("label.field", el("span", { text: "Tax year" }), select));
}

/** A term with its German in parentheses and the server's sentence on hover. */
function term(glossary, key, fallback) {
  const entry = glossary?.[key];
  if (!entry) return el("span", { text: fallback });
  return el(
    "abbr",
    { title: entry.one_line },
    el("span", { text: entry.plain }),
    " ",
    el("span.de", { lang: "de", text: `(${entry.german})` }),
  );
}

/* ------------------------------------------------------------ allowance */

/**
 * Gains actually realised by selling, as opposed to income that arrived.
 *
 * The API reports the total and two of its three parts, so the third is the
 * remainder. Clamped at zero: rounding could otherwise produce a bar of
 * -0.01, which reads as a loss rather than as nothing.
 */
function realisedShare(a) {
  const total = f.num(a.gross_gains_eur);
  if (total === null) return null;
  const rest = (f.num(a.distributions_eur) ?? 0) + (f.num(a.vorabpauschale_eur) ?? 0);
  return Math.max(0, total - rest);
}

function allowanceSection(a) {
  const g = a.glossary || {};
  const cap = f.num(a.statutory_cap_eur) ?? 0;
  const used = f.num(a.allowance_used_eur) ?? 0;
  const share = cap ? Math.max(0, Math.min(1, used / cap)) : 0;

  return el(
    "section",
    el("h2", term(g, "statutory_cap_eur", "Tax-free allowance")),
    el("div.progress", el("span", { style: `width:${(share * 100).toFixed(1)}%` })),
    el(
      "div.cards",
      card("Allowance", f.eur(a.statutory_cap_eur), { cls: "total" }),
      card("Used", f.eur(a.allowance_used_eur)),
      card("Still free", f.eur(a.allowance_remaining_eur), {
        valueCls: (f.num(a.allowance_remaining_eur) ?? 0) > 0 ? "gain" : "",
      }),
      card("Taxable after it", f.eur(a.taxable_eur)),
      card("Estimated tax", f.eur(a.estimated_tax_eur), {
        sub: g.estimated_tax_eur?.one_line || null,
      }),
    ),
    el("h3", { text: "What used it" }),
    // `gross_gains_eur` is the TOTAL of everything taxable this year --
    // realised gains plus distributions plus the advance charge -- not a
    // component beside them. Drawing it as one bar next to the others would
    // show the whole and a part at equal length and read as double counting,
    // so the realised-gain share is backed out here.
    bars(
      [
        { label: "Realised gains", value: realisedShare(a) },
        { label: "Dividends and interest", value: f.num(a.distributions_eur) },
        {
          label: g.vorabpauschale_eur
            ? `${g.vorabpauschale_eur.plain} (${g.vorabpauschale_eur.german})`
            : "Advance charge",
          value: f.num(a.vorabpauschale_eur),
          title: g.vorabpauschale_eur?.detail,
        },
      ].filter((i) => (i.value ?? 0) !== 0),
      { format: (v) => f.eur(v) },
    ),
    el("p.chart-note", {
      text: `These add up to ${f.eur(a.gross_gains_eur)} of taxable investment income this year.`,
    }),
    el("p.note", {
      text:
        "The allowance resets on 1 January and does not carry over. Anything " +
        "unused on 31 December is lost, which is why realising a gain in " +
        "December can be worth doing.",
    }),
  );
}

/* ----------------------------------------------------------------- losses */

function lossSection(a) {
  const g = a.glossary || {};
  const carried = a.losses_carried_forward || {};
  const aktien = f.num(carried.aktien) ?? 0;
  const sonstige = f.num(carried.sonstige) ?? 0;
  if (!aktien && !sonstige && !(f.num(a.losses_offset_eur) ?? 0)) return el("div");

  return el(
    "section",
    heading("Losses"),
    el(
      "div.cards",
      card("Offset this year", f.eur(a.losses_offset_eur)),
      card("Carried forward: shares", f.eur(aktien), {
        sub: g.AKTIEN?.one_line || null,
      }),
      card("Carried forward: everything else", f.eur(sonstige), {
        sub: g.SONSTIGE?.one_line || null,
      }),
    ),
    el(
      "p.note",
      "German law keeps these in two separate pots. ",
      term(g, "AKTIEN", "Losses on individual shares"),
      " can only cancel gains on other individual shares. ",
      term(g, "SONSTIGE", "Losses on everything else"),
      " can cancel any investment income except individual-share gains. " +
        "Both carry forward indefinitely. Losses are applied before the " +
        "allowance, not after.",
    ),
  );
}

/* -------------------------------------------------------- advance charge */

function advanceChargeSection(v) {
  const g = v.glossary || {};
  const rows = v.rows || [];

  const explain = g.vorabpauschale;
  const section = el("section", el("h2", term(g, "vorabpauschale", "Advance charge")));

  if (explain) section.append(el("div.panel", el("p", { text: explain.detail })));

  if (!rows.length) {
    section.append(
      el("p.nodata", {
        text: `No advance charge for ${v.tax_year}: no accumulating fund gained enough, or none is held.`,
      }),
    );
    return section;
  }

  section.append(
    el(
      "div.wrap",
      el(
        "table",
        el(
          "thead",
          el(
            "tr",
            el("th", { text: "Fund" }),
            el("th", { text: "Units" }),
            el("th", { text: "Value 1 Jan" }),
            el("th", term(g, "basisertrag_eur", "Deemed income")),
            el("th", { text: "Capped at" }),
            el("th", { text: "Taxable" }),
          ),
        ),
        el(
          "tbody",
          rows.map((r) =>
            row([
              { text: r.instrument, cls: "name" },
              f.qty(r.quantity),
              f.eur((f.num(r.jan_first_price) ?? 0) * (f.num(r.quantity) ?? 0)),
              {
                text: f.eur(r.basisertrag_eur),
                title:
                  `Value on 1 January × ${f.pct(r.basiszins)} × 70%` +
                  (r.full_months_before
                    ? `, then × ${12 - r.full_months_before}/12 for a part year`
                    : ""),
              },
              { text: f.eur(r.cap_eur), title: "The fund's actual gain over the year." },
              f.eur(r.taxable_eur),
            ]),
          ),
        ),
      ),
    ),
    el("p.note", {
      text:
        `Your bank will charge this on ${f.date(v.taxable_on)} — in the January ` +
        "after the year it relates to. It uses up allowance in that later year, " +
        "not this one.",
    }),
  );
  return section;
}

/* --------------------------------------------------------------- harvest */

function harvestSection(h) {
  const candidates = h.candidates || [];
  const headroom = f.num(h.allowance_remaining_eur ?? h.remaining_eur);

  const section = el(
    "section",
    heading("Gains you could still take tax-free"),
    el("p.note", {
      text:
        headroom === null
          ? ""
          : `About ${f.eur(headroom)} of allowance is still unused this year. ` +
            "Selling and immediately rebuying to realise a gain inside it raises " +
            "your cost basis at no tax cost — but it does cost dealing fees and " +
            "spread, which is what the figures below weigh.",
    }),
  );

  if (!candidates.length) {
    section.append(el("p.nodata", { text: "Nothing worth realising: no allowance left, or no gains to take." }));
    return section;
  }

  section.append(
    el(
      "div.wrap",
      el(
        "table",
        el(
          "thead",
          el("tr", ["Holding", "Units", "Gain realised", "Taxable", "Tax saved"].map((t) => el("th", { text: t }))),
        ),
        el(
          "tbody",
          candidates.map((c) =>
            row([
              {
                node: el("a", {
                  href: `#/holdings/${encodeURIComponent(c.instrument)}`,
                  text: c.instrument,
                }),
                cls: "name",
              },
              f.qty(c.quantity),
              f.eur(c.gross_gain_eur),
              f.eur(c.taxable_gain_eur),
              { text: f.eur(c.tax_saved_eur ?? c.benefit_eur), cls: "gain" },
            ]),
          ),
        ),
      ),
    ),
  );
  if (h.caveats?.length) {
    section.append(el("ul.caveats", h.caveats.map((c) => el("li", { text: c }))));
  }
  return section;
}

/* ----------------------------------------------------------------- scope */

function scopeSection(a) {
  const scope = a.out_of_scope || {};
  if (!scope.instruments?.length) return el("div");

  return el(
    "section",
    heading("Not included in the figures above"),
    el(
      "div.panel.warn",
      el("p", { text: scope.warning || "" }),
      el("p", {
        text: `${scope.instruments.join(", ")} — worth ${f.eur(scope.value_eur)} in total.`,
      }),
      scope.reasons?.length
        ? el("ul.caveats", scope.reasons.map((r) => el("li", { text: r })))
        : null,
    ),
  );
}

/* ------------------------------------------------------------- constants */

function missingConstant(error, constants) {
  const suggested = !isError(constants) && constants.missing_years?.length
    ? constants.missing_years[constants.missing_years.length - 1]
    : year;

  return el(
    "div.panel.warn",
    el("h3", { text: "One published rate is missing" }),
    el("p", { text: error.message }),
    el("p", {
      text:
        "This tool refuses to guess it, because a guessed rate produces a tax " +
        "figure that looks authoritative and is not. Enter the published " +
        "figure and everything on this page computes.",
    }),
    constantForm(suggested),
    !isError(constants) ? el("p.note", { text: constants.source }) : null,
  );
}

function constantForm(defaultYear) {
  const yearInput = el("input", { type: "number", value: String(defaultYear), step: "1" });
  const rateInput = el("input", { type: "number", step: "any", placeholder: "3.20" });
  const status = el("p.note");

  const save = async () => {
    const percent = Number(rateInput.value);
    if (!Number.isFinite(percent) || percent < 0) {
      status.textContent = "Enter the rate as a percentage, for example 3.20.";
      status.className = "note bad";
      return;
    }
    try {
      await put("/api/tax/constants", {
        tax_year: Number(yearInput.value),
        // Entered as a percentage because that is how it is published;
        // stored as a fraction because that is how it is used.
        basiszins: String(percent / 100),
      });
      router.refresh();
    } catch (err) {
      status.textContent = err.message;
      status.className = "note bad";
    }
  };

  return el(
    "div",
    el(
      "form.row",
      { onsubmit: (e) => (e.preventDefault(), save()) },
      el("label.field", el("span", { text: "Tax year" }), yearInput),
      el("label.field", el("span", { text: "Base rate %" }), rateInput),
      el("button.primary", { type: "submit", text: "Save" }),
    ),
    status,
  );
}

function constantsSection(constants) {
  const rows = (constants.years || []).map((y) =>
    row([y.tax_year, f.pct(y.basiszins), f.pct(y.kest_rate), f.pct(y.soli_rate)]),
  );
  return el(
    "section",
    heading("Published rates on file"),
    el(
      "div.wrap",
      el(
        "table",
        el(
          "thead",
          el(
            "tr",
            el("th", { text: "Year" }),
            el("th", { text: "Base rate" }),
            el("th", { text: "Capital-gains tax" }),
            el("th", { text: "Solidarity surcharge" }),
          ),
        ),
        el("tbody", rows),
      ),
    ),
    constants.missing_years?.length
      ? el(
          "div",
          el("p.note.warn", {
            text: `No base rate recorded for ${constants.missing_years.join(", ")}.`,
          }),
          constantForm(constants.missing_years[constants.missing_years.length - 1]),
        )
      : null,
    el("p.note", { text: constants.source }),
  );
}
