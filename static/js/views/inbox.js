/*
 * Things the tool worked out but will not record without being told to.
 *
 * The rule this screen protects: nothing auto-generated reaches the ledger
 * without a person agreeing to it. A dividend inferred from a price gap, an
 * interest payment derived from a balance, an advance tax charge computed from
 * a published rate -- each is a good guess and none is evidence.
 *
 * So every proposal shows its full derivation, the amount is editable before
 * confirming, and rejecting asks why. The reason matters: a rejected proposal
 * must not come back next week, and "this never happened" and "the amount was
 * different" are different facts.
 */

import { el, row, heading } from "../dom.js";
import { get, post, all, isError } from "../api.js";
import * as f from "../fmt.js";
import * as router from "../router.js";

export async function render() {
  const data = await all({
    pending: get("/api/pending"),
    incoming: get("/api/incoming"),
  });

  if (isError(data.pending)) throw data.pending;

  const view = el(
    "div",
    el("h1", { text: "Inbox" }),
    el("p.sub", { text: "Nothing here has reached your records yet." }),
  );

  view.append(proposals(data.pending));
  if (!isError(data.incoming)) view.append(dropFolder(data.incoming));
  view.append(vorabpauschalePrompt());
  return view;
}

/* ------------------------------------------------------------ proposals */

function proposals(body) {
  const entries = body.entries || [];
  const section = el("section", heading("Waiting for a decision"));

  if (body.rebuild_required) {
    section.append(
      el(
        "div.panel.warn",
        el("p", {
          text:
            "Something was confirmed since the figures were last computed, so " +
            "the rest of the app is showing older numbers.",
        }),
        el("div.actions", rebuildButton()),
      ),
    );
  }

  if (!entries.length) {
    section.append(el("p.nodata", { text: "Nothing is waiting. Everything inferred has been dealt with." }));
    return section;
  }

  for (const entry of entries) section.append(proposalCard(entry));
  return section;
}

function proposalCard(entry) {
  const amount = el("input", {
    type: "number",
    step: "any",
    value: entry.amount === null ? "" : String(entry.amount),
    "aria-label": "Amount",
  });
  const status = el("p.note");
  const original = amount.value;

  const busy = (on) => {
    for (const b of card.querySelectorAll("button")) b.disabled = on;
  };

  const confirm = async () => {
    busy(true);
    status.className = "note";
    status.textContent = "Recording…";
    try {
      // Only send an amount if it was actually changed. Sending the computed
      // figure back as if it were a correction would erase the evidence that
      // it was a guess.
      const edited = amount.value !== original && amount.value !== "";
      await post(`/api/pending/${entry.id}/confirm`, edited ? { amount_local: amount.value } : {});
      router.refresh();
    } catch (err) {
      busy(false);
      status.className = "note bad";
      status.textContent = err.message;
    }
  };

  const reject = async () => {
    const reason = window.prompt(
      "Why did this not happen? The reason is recorded, and stops it being proposed again.",
    );
    if (!reason) return;
    busy(true);
    try {
      await post(`/api/pending/${entry.id}/reject`, { reason });
      router.refresh();
    } catch (err) {
      busy(false);
      status.className = "note bad";
      status.textContent = err.message;
    }
  };

  const card = el(
    "div.panel",
    el(
      "div.chart-head",
      el("span.tag", { text: String(entry.kind).toLowerCase().replace(/_/g, " ") }),
      el("strong", { text: f.date(entry.date) }),
      entry.bookable ? null : el("span.tag.warn", { text: "cannot be booked yet" }),
    ),
    el("p", { text: entry.basis }),
    entry.note ? el("p.note", { text: entry.note }) : null,
    el(
      "div.actions",
      el("label.field", el("span", { text: `Amount (${entry.currency})` }), amount),
      el("button.primary", { text: "Confirm", disabled: !entry.bookable, onclick: confirm }),
      el("button.danger", { text: "Reject", onclick: reject }),
    ),
    status,
  );
  return card;
}

function rebuildButton() {
  const button = el("button.primary", {
    text: "Recompute now",
    onclick: async () => {
      button.disabled = true;
      button.textContent = "Recomputing…";
      try {
        await post("/api/admin/rebuild", {});
        router.refresh();
      } catch (err) {
        button.disabled = false;
        button.textContent = "Recompute now";
        window.alert(err.message);
      }
    },
  });
  return button;
}

/* ---------------------------------------------------------- drop folder */

function dropFolder(body) {
  const files = body.files || [];
  const section = el(
    "section",
    heading("Files you dropped in"),
    el("p.note", { text: body.folder }),
  );

  if (!files.length) {
    section.append(
      el("div.empty", el("p", { text: body.note || "Nothing in the folder." })),
    );
    return section;
  }

  for (const file of files) section.append(fileCard(file, body.funds || []));
  section.append(el("p.note", { text: body.note }));
  return section;
}

function fileCard(file, funds) {
  const head = el(
    "div.chart-head",
    el("strong", { text: file.name }),
    el("span.tag", { text: String(file.kind || "unknown").replace(/_/g, " ") }),
  );

  if (!file.importable) {
    return el(
      "div.panel",
      head,
      el("p.note" + (file.problem ? ".bad" : ""), {
        text: file.problem || file.summary || "This kind of file is not imported from here.",
      }),
    );
  }

  // The fund is always chosen by a person. A holdings file does not reliably
  // say which fund it belongs to, and attaching one issuer's constituents to
  // the wrong fund corrupts every exposure figure without erroring anywhere.
  const fundSelect = el(
    "select",
    el("option", { value: "", text: "Choose the fund…" }),
    funds.map((fund) => el("option", { value: fund.instrument, text: `${fund.instrument} — ${fund.name}` })),
  );
  const asOf = el("input", { type: "date", value: file.suggested_as_of || "" });
  const status = el("p.note");

  const doImport = async () => {
    if (!fundSelect.value) {
      status.className = "note bad";
      status.textContent = "Choose which fund this file describes.";
      return;
    }
    if (!asOf.value) {
      status.className = "note bad";
      status.textContent = "Enter the date the fund published this composition.";
      return;
    }
    status.className = "note";
    status.textContent = "Importing…";
    try {
      await post("/api/incoming/import", {
        name: file.name,
        instrument: fundSelect.value,
        as_of: asOf.value,
      });
      router.refresh();
    } catch (err) {
      status.className = "note bad";
      status.textContent = err.message;
    }
  };

  return el(
    "div.panel",
    head,
    el("p.note", {
      text:
        (file.summary || "") +
        (file.holdings ? ` · ${file.holdings} holdings` : "") +
        (f.has(file.weight_sum) ? ` · weights sum to ${f.pct(file.weight_sum)}` : ""),
    }),
    el(
      "form.row",
      { onsubmit: (e) => (e.preventDefault(), doImport()) },
      el("label.field", el("span", { text: "Fund" }), fundSelect),
      el("label.field", el("span", { text: "Published" }), asOf),
      el("button.primary", { type: "submit", text: "Import" }),
    ),
    status,
  );
}

/* ------------------------------------------------------- advance charge */

function vorabpauschalePrompt() {
  const year = new Date().getFullYear() - 1;
  const input = el("input", { type: "number", step: "1", value: String(year) });
  const status = el("p.note");

  const propose = async () => {
    status.className = "note";
    status.textContent = "Computing…";
    try {
      const body = await post("/api/vorabpauschale/propose", {}, { year: input.value });
      status.textContent =
        body.created === 0
          ? "Nothing to charge for that year."
          : `${body.created} charge(s) added above for you to review.`;
      if (body.created) router.refresh();
    } catch (err) {
      status.className = "note bad";
      status.textContent = err.message;
    }
  };

  return el(
    "section",
    heading("Advance tax charges", "Vorabpauschale"),
    el("p.note", {
      text:
        "Each January your bank charges tax on a notional return from " +
        "accumulating funds. Work out what it should be for a year, and it " +
        "appears above as a proposal rather than being recorded straight away.",
    }),
    el(
      "form.row",
      { onsubmit: (e) => (e.preventDefault(), propose()) },
      el("label.field", el("span", { text: "For year" }), input),
      el("button", { type: "submit", text: "Work it out" }),
    ),
    status,
  );
}

export { row };
