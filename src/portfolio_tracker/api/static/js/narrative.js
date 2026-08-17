/*
 * The "explain this" control.
 *
 * Deliberately a button rather than something that happens on load. Each
 * narration costs a call against a free tier, and a page that spent four of
 * them every time it opened would stop working within a day and look broken
 * rather than rate-limited. It is also the honest shape: the figures are the
 * product, and the prose is an optional second reading of them.
 *
 * When narration is off, unconfigured, or fails, this renders the reason and
 * nothing else. No panel ever shows a blank space where an explanation was
 * supposed to be.
 */

import { el } from "./dom.js";
import { get } from "./api.js";

/**
 * A panel that fetches prose on demand.
 *
 * @param {string} path   endpoint that accepts `narrate=true`
 * @param {object} params other query parameters
 * @param {string} label  button text
 */
export function explainPanel(path, params = {}, label = "Explain this in words") {
  const out = el("div");
  const button = el("button", {
    text: label,
    onclick: async () => {
      button.disabled = true;
      button.textContent = "Writing…";
      out.replaceChildren(el("p.note", { text: "Asking the model…" }));
      try {
        const body = await get(path, { ...params, narrate: "true" });
        out.replaceChildren(render(body));
      } catch (err) {
        out.replaceChildren(el("p.note.bad", { text: err.message }));
      }
      button.disabled = false;
      button.textContent = label;
    },
  });

  return el("div.narrative", el("div.actions", button), out);
}

function render(body) {
  if (body.narrative) {
    return el(
      "div.panel",
      el("p", { text: body.narrative }),
      el("p.note", {
        text:
          `Written by ${body.narrative_model || "the model"} from the figures on this ` +
          "page and nothing else. It cannot compute a number, and every request " +
          "is recorded in Settings so you can read what was sent.",
      }),
    );
  }

  // A refusal is reported, never hidden. Each status means something
  // different and the reader can act on some of them.
  const status = body.narrative_status;
  const advice = {
    NOT_REQUESTED: "Press the button to ask for a written summary.",
    UNAVAILABLE: "Set this up in Settings if you want written summaries.",
    CAPPED: "The daily limit is a setting; raise it in Settings if you want more.",
    REFUSED: "Nothing is shown when the model breaks the rules this tool sets it.",
    ERROR: "The figures above are unaffected.",
  }[status];

  return el(
    "p.note" + (status === "ERROR" ? ".bad" : ""),
    { text: body.narrative_reason || "No written summary." },
    advice ? el("span", { text: ` ${advice}` }) : null,
  );
}
