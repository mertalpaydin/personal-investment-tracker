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

export async function render({ query }) {
  const data = await all({
    positions: get("/api/positions"),
    ledger: get("/api/transactions", { limit: 100 }),
  });

  const view = el(
    "div",
    el("h1", { text: "Trades" }),
    el("p.sub", { text: "Record what you did. Nothing is saved until you press Save." }),
  );

  const holdings = isError(data.positions) ? [] : data.positions.positions || [];
  view.append(entryForm(holdings, query));

  if (!isError(data.ledger)) view.append(ledgerTable(data.ledger));
  return view;
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
        node: t.instrument
          ? el("a", { href: `#/holdings/${encodeURIComponent(t.instrument)}`, text: t.instrument })
          : document.createTextNode("—"),
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
