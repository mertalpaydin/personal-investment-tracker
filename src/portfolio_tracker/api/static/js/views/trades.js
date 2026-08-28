/*
 * Recording a trade, and seeing what it will do first.
 *
 * The form is the smaller half of this screen. The half that matters is the
 * panel beside it, which re-validates as you type and, for a sale, says which
 * tax lots FIFO will consume, what the gain is after advance-charge relief and
 * partial exemption, and how much tax-free allowance is left afterwards --
 * before anything is saved.
 *
 * That preview is not computed here. It comes from the same server function
 * that will process the trade when it is saved, so what the page promises and
 * what the ledger does cannot drift apart.
 */

import { el, row, heading } from "../dom.js";
import { get, post, del, all, isError } from "../api.js";
import * as f from "../fmt.js";
import * as router from "../router.js";

const NEEDS_INSTRUMENT = new Set(["BUY", "SELL", "DIVIDEND", "TRANSFER_IN", "TRANSFER_OUT"]);
const NEEDS_QUANTITY = new Set(["BUY", "SELL"]);
const TYPES = [
  ["BUY", "Bought"],
  ["SELL", "Sold"],
  ["DIVIDEND", "Dividend received"],
  ["INTEREST", "Interest received"],
  ["DEPOSIT", "Paid money in"],
  ["WITHDRAWAL", "Took money out"],
  ["FEE", "Fee charged"],
  ["TAX_WITHHELD", "Tax withheld"],
  ["TRANSFER_IN", "Transferred in from elsewhere"],
  ["TRANSFER_OUT", "Transferred out"],
];

//: Kept across refreshes, so deleting a row does not reset the filter.
let filterInstrument = "";
let filterType = "";

export async function render({ query }) {
  const data = await all({
    positions: get("/api/positions"),
    ledger: get("/api/transactions", {
      limit: 200,
      instrument: filterInstrument || null,
      type: filterType || null,
    }),
    funding: get("/api/savings/inferred"),
  });

  const view = el(
    "div",
    el("h1", { text: "Trades" }),
    el("p.sub", { text: "Record what you did. Nothing is saved until you press Save." }),
  );

  // Before the form: until this is settled every return figure in the app is
  // overstated, and there is nothing to be gained by recording another trade
  // on top of it.
  if (!isError(data.funding) && data.funding.count > 0) {
    view.append(fundingReview(data.funding));
  }

  const holdings = isError(data.positions) ? [] : data.positions.positions || [];
  view.append(entryForm(holdings, query));

  if (!isError(data.ledger)) view.append(ledgerTable(data.ledger));
  return view;
}

/* ---------------------------------------------------- inferred funding */

/*
 * Contributions an earlier version of this tool deduced rather than read.
 *
 * The rule was: a purchase larger than the cash on record means money must
 * have arrived. On this ledger it produced EUR 131,479 of savings nobody
 * recorded against EUR 23,367 that were. The money may have been a deposit --
 * or it may have come from an account this tool cannot see, which is a
 * different fact. Only the owner knows which, so only the owner decides.
 */
function fundingReview(body) {
  const rows = (body.deposits || []).map((d) =>
    row([
      f.date(d.date),
      { text: d.account, cls: "muted" },
      f.eur(d.amount_eur),
      {
        node: el("div", whyItWasGuessed(d), nearbyMoney(d)),
        cls: "prose",
        title: plainReason(d),
      },
      {
        node: el(
          "span.actions",
          el("button", {
            text: "It was new money",
            title: "Counts as a contribution. Your return figures drop accordingly.",
            onclick: () => decide("POST", `/api/savings/inferred/${d.id}/keep`, {}),
          }),
          el("button.danger", {
            text: "It came from elsewhere",
            title: "Removes it. The money is treated as having come from outside this tool.",
            onclick: () => decide("DELETE", `/api/savings/inferred/${d.id}`),
          }),
        ),
      },
    ]),
  );

  return el(
    "section",
    el("div.panel.bad",
      el("h3", { text: `${body.count} contributions were guessed, not recorded` }),
      el("p", {
        text:
          `Totalling ${f.eur(body.total_eur)}. An earlier version assumed that a purchase ` +
          "larger than your recorded cash meant a deposit had arrived.",
      }),
      el("p.note", { text: body.what_to_do }),
      el(
        "div.wrap",
        el(
          "table",
          el(
            "thead",
            el("tr", ["Date", "Account", "Amount", "Why it was guessed", ""].map((x) => el("th", { text: x }))),
          ),
          el("tbody", rows),
        ),
      ),
      el(
        "div.actions",
        el("button.danger", {
          text: "None of these were mine — clear them all",
          onclick: () => {
            if (window.confirm(`Remove all ${body.count}? Your return figures will change.`)) {
              decide("DELETE", "/api/savings/inferred");
            }
          },
        }),
      ),
    ),
  );
}

/*
 * What the money was assumed to be for.
 *
 * The old explanation was "purchase of 40015.36 against a cash balance of
 * 0.00": no separators, no currency, and no clue what was bought. Worse, on a
 * lira fund that 40,015.36 was lira while the deposit beside it was 1,114.44
 * euro -- two currencies in one sentence, neither of them labelled.
 */
/** The same sentence as one string, for the hover and for screen readers. */
function plainReason(d) {
  const b = d.bought;
  if (!b) return "No purchase is linked to this one.";
  const cash =
    d.cash_before_eur > 0
      ? ` You had ${f.eur(d.cash_before_eur)} of recorded cash, so the rest was assumed to have arrived.`
      : " No cash was recorded, so the whole amount was assumed to have arrived.";
  return (
    `To buy ${f.qty(b.quantity)} ${b.instrument}` +
    (b.name ? ` (${b.name})` : "") +
    ` for ${f.money(b.amount_local, b.currency)} on ${f.date(b.date)}.${cash}`
  );
}


function whyItWasGuessed(d) {
  const b = d.bought;
  if (!b) {
    return el("span.muted", { text: "No purchase is linked to this one." });
  }

  const cash =
    d.cash_before_eur === null || d.cash_before_eur === undefined
      ? ""
      : d.cash_before_eur > 0
        ? ` You had ${f.eur(d.cash_before_eur)} of recorded cash, so the rest was assumed to have arrived.`
        : " No cash was recorded, so the whole amount was assumed to have arrived.";

  return el(
    "span",
    el("span", { text: "To buy " }),
    el("strong", { text: `${f.qty(b.quantity)} ${b.instrument}` }),
    el("span.muted", { text: b.name ? ` (${b.name})` : "" }),
    el("span", { text: ` for ${f.money(b.amount_local, b.currency)}` }),
    // Only worth showing when it differs from the amount already in the row.
    b.currency !== "EUR" && b.amount_eur
      ? el("span.muted", { text: ` — about ${f.eur(b.amount_eur)}` })
      : null,
    el("span", { text: ` on ${f.date(b.date)}.` }),
    el("span.muted", { text: cash }),
  );
}


/*
 * Sales and income around the same date, so the question is answerable.
 *
 * "Did money arrive two years ago" is not something anybody remembers. "Did
 * this sale pay for it" is. The distinction that makes the list worth reading:
 * `core/cash.py` tracks a balance per account **and currency**, so a euro sale
 * cannot offset a lira purchase in its model even when the broker converted
 * the money for you. Those sales were never applied, and they are exactly the
 * ones that might explain the shortfall.
 */
function nearbyMoney(d) {
  const near = d.money_around_it || [];
  const usable = near.filter((n) => !n.already_counted);
  const spent = near.filter((n) => n.already_counted);

  if (!near.length) {
    return el("p.note", { text: "Nothing was sold or paid out near this date." });
  }

  const box = el("div.nearby");

  if (usable.length) {
    box.append(
      el("p.note.warn", {
        text:
          usable.length === 1
            ? "This could have paid for it, and was not counted:"
            : `${usable.length} of these could have paid for it, and were not counted:`,
      }),
    );
    for (const n of usable) {
      box.append(
        el(
          "div.near",
          el("span.when", {
            text:
              n.days_before > 0
                ? `${n.days_before}d before`
                : n.days_before === 0
                  ? "same day"
                  : `${-n.days_before}d after`,
          }),
          el("span", {
            text:
              `${n.type.toLowerCase()} ${n.instrument || ""} ` +
              `${f.money(n.amount_local, n.currency)}`.trim(),
          }),
          // Why it was not counted. Three different reasons, and the reader
          // needs the right one to judge whether it really paid for this.
          el("span.tag", { text: whyNotCounted(n, d) }),
        ),
      );
    }
  }

  if (spent.length) {
    box.append(
      el("p.note", {
        text: `${spent.length} other sale${spent.length === 1 ? " was" : "s were"} already counted before this figure was worked out.`,
      }),
    );
  }
  return box;
}


/** Why the tool could not apply this sale to that purchase. */
function whyNotCounted(n, d) {
  const bought = d.bought || {};
  if (n.currency !== bought.currency) {
    // The one that matters most here: cash is tracked per currency, so a euro
    // sale never offsets a lira purchase even when the broker converted it.
    return `${n.currency} cash, the purchase was ${bought.currency}`;
  }
  if (n.days_before < 0) return "settled after the purchase";
  if (n.account !== d.account) return `different account (${n.account})`;
  return "not applied";
}


async function decide(method, path, body) {
  try {
    if (method === "DELETE") await del(path);
    else await post(path, body || {});
    router.refresh();
  } catch (err) {
    window.alert(err.message);
  }
}

/* ----------------------------------------------------------------- form */

function entryForm(holdings, query) {
  const preview = el("div.panel", el("p.note", { text: "Fill in the trade to see what it would do." }));
  const status = el("p.note");

  const fields = {
    type: el(
      "select",
      TYPES.map(([value, label]) =>
        el("option", { value, text: label, selected: value === (query.get("type") || "BUY") }),
      ),
    ),
    instrument: el(
      "select",
      el("option", { value: "", text: "Choose…" }),
      holdings.map((h) =>
        el("option", {
          value: h.instrument,
          text: `${h.instrument} — ${h.name}`,
          selected: h.instrument === query.get("instrument"),
          // Read back by applyRelevance: a euro holding has no exchange
          // rate to enter, and a field that cannot apply is a field that
          // invites a wrong answer.
          "data-currency": h.currency,
        }),
      ),
    ),
    trade_date: el("input", { type: "date", value: f.iso(new Date()), required: true }),
    quantity: el("input", { type: "number", step: "any", min: "0", value: query.get("qty") || "" }),
    price: el("input", { type: "number", step: "any", min: "0" }),
    amount: el("input", { type: "number", step: "any", min: "0" }),
    fees: el("input", { type: "number", step: "any", min: "0", value: "0" }),
    feesKnown: el("input", { type: "checkbox", checked: true }),
    fx: el("input", { type: "number", step: "any", min: "0", placeholder: "from stored rates" }),
    // Asked, never deduced. This tool used to infer a contribution whenever a
    // purchase exceeded the cash it knew about, and that invented 85% of the
    // savings figure -- the money might equally have come from an account it
    // cannot see. Only you know, so only you say.
    newMoney: el("input", { type: "number", step: "any", min: "0", placeholder: "0" }),
    newMoneyLabel: el("input", { type: "text", placeholder: "salary, bonus, gift" }),
    note: el("input", { type: "text", placeholder: "optional" }),
  };

  const saveButton = el("button.primary", { type: "submit", text: "Save this trade" });

  /** Build the request body from whatever is currently in the form. */
  const body = () => {
    const type = fields.type.value;
    const out = {
      type,
      trade_date: fields.trade_date.value,
      fees_local: fields.fees.value || "0",
      fees_known: fields.feesKnown.checked,
    };
    if (NEEDS_INSTRUMENT.has(type) && fields.instrument.value) out.instrument = fields.instrument.value;
    if (fields.quantity.value) out.quantity = fields.quantity.value;
    if (fields.price.value) out.price_per_unit_local = fields.price.value;
    if (fields.amount.value) out.gross_amount_local = fields.amount.value;
    if (fields.fx.value) out.fx_rate = fields.fx.value;
    if (fields.newMoney.value) out.new_money_local = fields.newMoney.value;
    if (fields.newMoneyLabel.value) out.new_money_label = fields.newMoneyLabel.value;
    if (fields.note.value) out.note = fields.note.value;
    return out;
  };

  const complete = () => {
    const type = fields.type.value;
    if (!fields.trade_date.value) return false;
    if (NEEDS_INSTRUMENT.has(type) && !fields.instrument.value) return false;
    if (NEEDS_QUANTITY.has(type) && !fields.quantity.value) return false;
    if (NEEDS_QUANTITY.has(type) && !fields.price.value && !fields.amount.value) return false;
    if (!NEEDS_QUANTITY.has(type) && !fields.amount.value) return false;
    return true;
  };

  // Debounced, because this fires on every keystroke and each call runs the
  // real FIFO walk and tax settlement on the server.
  let timer = null;
  let generation = 0;
  const revalidate = () => {
    applyRelevance();
    window.clearTimeout(timer);
    if (!complete()) {
      preview.replaceChildren(el("p.note", { text: "Fill in the trade to see what it would do." }));
      saveButton.disabled = true;
      return;
    }
    timer = window.setTimeout(async () => {
      const mine = ++generation;
      try {
        const verdict = await post("/api/transactions/validate", body());
        if (mine !== generation) return;
        preview.replaceChildren(...previewNodes(verdict));
        saveButton.disabled = !verdict.ok;
      } catch (err) {
        if (mine !== generation) return;
        preview.replaceChildren(el("p.note.bad", { text: err.message }));
        saveButton.disabled = true;
      }
    }, 350);
  };

  /* Show only the fields the chosen type actually uses. A dividend has no
     unit price; a deposit has no instrument. Leaving them visible invites
     entering a number that will be ignored. */
  const wraps = {};
  const applyRelevance = () => {
    const type = fields.type.value;
    const trades = NEEDS_QUANTITY.has(type);
    wraps.instrument.hidden = !NEEDS_INSTRUMENT.has(type);
    wraps.quantity.hidden = !trades;
    wraps.price.hidden = !trades;
    wraps.fees.hidden = !trades;
    wraps.feesKnown.hidden = !trades;
    // Only a purchase can be funded from outside. A sale raises its own money
    // and a deposit already is the money.
    wraps.newMoney.hidden = type !== "BUY";
    wraps.newMoneyLabel.hidden = type !== "BUY";

    // An exchange rate only means something for a holding not priced in
    // euros. Showing the field otherwise invites a number that would be
    // ignored, or worse, one that would not be.
    const chosen = fields.instrument.selectedOptions[0];
    const currency = chosen ? chosen.getAttribute("data-currency") : null;
    wraps.fx.hidden = !currency || currency === "EUR";

    wraps.amount.querySelector("span").textContent = trades ? "Total instead" : "Amount";
  };

  const field = (key, label, node, hint = null) => {
    const wrap = el(
      "label.field",
      el("span", { text: label }),
      node,
      hint ? el("span.note", { text: hint }) : null,
    );
    wraps[key] = wrap;
    return wrap;
  };

  /** A checkbox reads as one line, not as a label stacked over a control. */
  const check = (key, label, node, hint = null) => {
    const wrap = el(
      "label.check.wide",
      node,
      el("span", { text: label }),
      hint ? el("span.note", { text: `— ${hint}` }) : null,
    );
    wraps[key] = wrap;
    return wrap;
  };

  const form = el(
    "form",
    {
      onsubmit: async (e) => {
        e.preventDefault();
        saveButton.disabled = true;
        status.className = "note";
        status.textContent = "Saving and recomputing…";
        try {
          const saved = await post("/api/transactions", body());
          status.className = "note";
          status.textContent =
            `Saved as entry ${saved.id}. ` +
            (saved.rebuild?.ok
              ? "Everything has been recomputed."
              : "Recomputed, but with warnings — see below.");
          router.refresh();
        } catch (err) {
          saveButton.disabled = false;
          status.className = "note bad";
          status.textContent = err.message;
          if (err.body?.detail?.errors) {
            preview.replaceChildren(...previewNodes(err.body.detail));
          }
        }
      },
    },
    el(
      "div.entry",
      field("type", "What happened", fields.type),
      field("trade_date", "Date", fields.trade_date),
      field("instrument", "Holding", fields.instrument),
      field("quantity", "Units", fields.quantity),
      field("price", "Price per unit", fields.price),
      field("amount", "Amount", fields.amount),
      field("fees", "Dealing fee", fields.fees),
      field("fx", "Exchange rate", fields.fx, "Only if no rate is stored"),
      field(
        "newMoney",
        "Funded by new money",
        fields.newMoney,
        "how much of this came from outside — leave blank if you paid with money already here",
      ),
      field("newMoneyLabel", "Where it came from", fields.newMoneyLabel, "optional"),
      field("note", "Note", fields.note),
      check(
        "feesKnown",
        "I know what the dealing fee was",
        fields.feesKnown,
        "untick if the broker charged something you cannot see; every gain then reads slightly high",
      ),
    ),
    el("div.actions", saveButton),
    status,
  );

  for (const node of Object.values(fields)) {
    node.addEventListener("input", revalidate);
    node.addEventListener("change", revalidate);
  }
  queueMicrotask(revalidate);

  return el(
    "section",
    heading("Record a trade"),
    el(
      "div.entry-layout",
      form,
      // The panel that makes this screen worth having: it re-runs the real
      // FIFO walk and tax settlement on every keystroke, so a sale is priced
      // before it is committed rather than explained afterwards.
      el("div", el("h3", { text: "What this would do" }), preview),
    ),
  );
}

/* -------------------------------------------------------------- preview */

function previewNodes(v) {
  const nodes = [];

  for (const e of v.errors || []) {
    nodes.push(
      el("p.note.bad", el("strong", { text: `${e.message} ` }), e.remedy ? remedyLink(e) : null),
    );
  }

  /*
   * Nothing below the errors, when there are errors.
   *
   * The server still computes the amount and the resulting position for a
   * trade it has refused, because those are useful for debugging. Showing
   * them to a reader is not: "cannot sell 99,999 units" sitting above
   * "€1,849,981.50" and "→ -99,558 units" describes a trade that cannot
   * happen, and the figures look like an answer.
   */
  if ((v.errors || []).length) return nodes;

  for (const w of v.warnings || []) nodes.push(el("p.note.warn", { text: w.message }));

  const n = v.normalised || {};
  if (n.gross_amount_eur !== null && n.gross_amount_eur !== undefined) {
    nodes.push(
      el(
        "p",
        el("strong", { text: f.eur(n.gross_amount_eur) }),
        n.currency && n.currency !== "EUR"
          ? el("span.note", {
              text: ` — ${f.qty(n.gross_amount_local)} ${n.currency} at ${f.qty(n.fx_rate)} (${n.fx_rate_source})`,
            })
          : null,
      ),
    );
  }

  const effect = v.effect?.position;
  if (effect) {
    nodes.push(
      el("p.note", {
        text:
          `${effect.instrument}: ${f.qty(effect.quantity_before)} units → ` +
          `${f.qty(effect.quantity_after)} units.`,
      }),
    );
  }

  if (v.sale) nodes.push(...saleNodes(v.sale));
  return nodes.length ? nodes : [el("p.note", { text: "Nothing to preview yet." })];
}

function remedyLink(problem) {
  // Turn the endpoint the server named into something clickable.
  const target = problem.remedy.includes("fetch-fx")
    ? "#/settings"
    : problem.remedy.includes("watchlist")
      ? "#/watchlist"
      : "#/settings";
  return el("a", { href: target, text: "Fix this" });
}

function saleNodes(sale) {
  const nodes = [
    el(
      "div.cards",
      statCard("Proceeds", f.eur(sale.proceeds_eur)),
      statCard("Taxable gain", f.eur(sale.taxable_gain_eur), f.tone(sale.taxable_gain_eur)),
      statCard("Tax this adds", f.eur(sale.tax_this_sale_adds_eur)),
      statCard("Allowance left after", f.eur(sale.allowance_remaining_after_eur)),
    ),
  ];

  if (sale.consumes?.length) {
    nodes.push(
      el("h3", { text: "Which lots this sells" }),
      el(
        "div.wrap",
        el(
          "table",
          el("thead", el("tr", ["Bought", "Units", "Cost", "Proceeds", "Gain"].map((t) => el("th", { text: t })))),
          el(
            "tbody",
            sale.consumes.map((c) =>
              row([
                f.date(c.opened),
                f.qty(c.quantity),
                f.eur(c.cost_basis_eur),
                f.eur(c.proceeds_eur),
                { text: f.eur(c.gross_gain_eur, { sign: true }), cls: f.tone(c.gross_gain_eur) },
              ]),
            ),
          ),
        ),
      ),
      el("p.note", {
        text:
          "Oldest first — that is how German tax decides which units you sold, " +
          "and it is why selling half a holding may realise a much larger or " +
          "smaller gain than the average cost suggests.",
      }),
    );
  }

  if (sale.vap_offset_eur > 0) {
    nodes.push(
      el("p.note", {
        text:
          `${f.eur(sale.vap_offset_eur)} of advance charges already taxed on these ` +
          "lots is deducted from the gain, so the same euros are not taxed twice.",
      }),
    );
  }
  if (sale.teilfreistellung_eur > 0) {
    nodes.push(
      el("p.note", {
        text: `${f.eur(sale.teilfreistellung_eur)} of the gain is exempt because it came from a fund.`,
      }),
    );
  }
  if (sale.caveats?.length) {
    nodes.push(el("ul.caveats", sale.caveats.map((c) => el("li", { text: c }))));
  }
  return nodes;
}

function statCard(label, value, cls = "") {
  return el("div.card", el("span.k", { text: label }), el("div.v", { class: cls, text: value }));
}

/* --------------------------------------------------------------- ledger */

function ledgerTable(body) {
  const txns = body.transactions || [];
  if (!txns.length) {
    return el("section", heading("Everything recorded"), el("p.nodata", { text: "Nothing yet." }));
  }

  const rows = txns.map((t) => {
    const cells = [
      f.date(t.trade_date),
      t.type.toLowerCase().replace(/_/g, " "),
      {
        node: !t.instrument
          ? document.createTextNode("—")
          : t.is_cash
            ? // A cash proxy is a bookkeeping device. `CASH_TRY_3` where a
              // fund name belongs makes a deposit look like a mystery holding.
              el("span.muted", { text: t.shown_as || t.account })
            : el("a", {
                href: `#/holdings/${encodeURIComponent(t.instrument)}`,
                text: t.instrument,
                title: t.shown_as || undefined,
              }),
      },
      f.qty(t.quantity),
      f.eur(t.gross_amount_eur),
      { node: badges(t) },
      { node: t.editable ? deleteButton(t) : el("span.muted", { text: "imported" }) },
    ];
    return row(cells);
  });

  return el(
    "section",
    heading("Everything recorded"),
    filterBar(body),
    el(
      "div.wrap",
      el(
        "table",
        el("thead", el("tr", ["Date", "What", "Holding", "Units", "Amount", "", ""].map((t) => el("th", { text: t })))),
        el("tbody", rows),
      ),
    ),
    body.total > txns.length
      ? el("p.note", { text: `Showing the most recent ${txns.length} of ${body.total}.` })
      : null,
    el("p.note", {
      text:
        "Only entries you typed here can be edited. Anything from an imported " +
        "file would be restored by the next import, so correcting it there is " +
        "the only change that lasts.",
    }),
  );
}

function filterBar(body) {
  const options = body.filters?.instruments || [];
  const types = body.filters?.types || [];

  const instrument = el(
    "select",
    { onchange: (e) => ((filterInstrument = e.target.value), router.refresh()) },
    el("option", { value: "", text: "Every holding", selected: !filterInstrument }),
    options.map((o) =>
      el("option", {
        value: o.instrument,
        // A deposit account rather than its bookkeeping ticker.
        text: o.is_cash ? o.label : `${o.instrument} — ${o.label}`,
        selected: o.instrument === filterInstrument,
      }),
    ),
  );

  const kind = el(
    "select",
    { onchange: (e) => ((filterType = e.target.value), router.refresh()) },
    el("option", { value: "", text: "Everything", selected: !filterType }),
    types.map((x) =>
      el("option", {
        value: x,
        text: x.toLowerCase().replace(/_/g, " "),
        selected: x === filterType,
      }),
    ),
  );

  return el(
    "form.row",
    { onsubmit: (e) => e.preventDefault() },
    el("label.field", el("span", { text: "Holding" }), instrument),
    el("label.field", el("span", { text: "Kind" }), kind),
    filterInstrument || filterType
      ? el("button", {
          type: "button",
          text: "Clear",
          onclick: () => ((filterInstrument = ""), (filterType = ""), router.refresh()),
        })
      : null,
    el("span.over", { text: `${body.total} entr${body.total === 1 ? "y" : "ies"}` }),
  );
}


function badges(t) {
  return el(
    "span",
    t.source !== "IMPORT" ? el("span.tag", { text: t.source.toLowerCase() }) : null,
    t.confidence !== "ACTUAL" ? el("span.tag.warn", { text: t.confidence.toLowerCase() }) : null,
    !t.fees_known && (t.type === "BUY" || t.type === "SELL")
      ? el("span.tag.warn", { text: "no fees" })
      : null,
  );
}

function deleteButton(t) {
  const button = el("button.danger", {
    text: "Delete",
    onclick: async () => {
      if (!window.confirm(`Delete this ${t.type.toLowerCase()} of ${f.date(t.trade_date)}?`)) return;
      button.disabled = true;
      try {
        await del(`/api/transactions/${t.id}`);
        router.refresh();
      } catch (err) {
        button.disabled = false;
        window.alert(err.message);
      }
    },
  });
  return button;
}
