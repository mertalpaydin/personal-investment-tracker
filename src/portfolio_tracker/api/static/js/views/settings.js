/*
 * Everything that used to be a command.
 *
 * The single most important control here is the daily update. Before it, new
 * prices arrived only when somebody typed `pt fetch-prices`; nothing was
 * scheduled, nothing noticed when it had not happened, and the page showed
 * plausible figures computed from days-old prices with no sign anything was
 * wrong. Turning on the schedule is one switch, and the run history underneath
 * it is how you know it is actually working.
 */

import { el, card, row, heading } from "../dom.js";
import { get, post, put, del, all, isError } from "../api.js";
import * as f from "../fmt.js";
import * as router from "../router.js";

export async function render() {
  const data = await all({
    updates: get("/api/updates"),
    llm: get("/api/settings/llm"),
    health: get("/api/health/data"),
    goal: get("/api/goal"),
    accounts: get("/api/accounts"),
    constants: get("/api/tax/constants"),
  });

  const view = el(
    "div",
    el("h1", { text: "Settings" }),
    el("p.sub", { text: "Everything this tool can do, without a command line." }),
  );

  if (!isError(data.updates)) {
    view.append(updateSection(data.updates));
    view.append(scheduleSection(data.updates.scheduler));
    view.append(historySection(data.updates.runs));
  }
  if (!isError(data.goal)) view.append(goalSection(data.goal));
  if (!isError(data.health)) view.append(healthSection(data.health));
  if (!isError(data.constants)) view.append(taxSection(data.constants));
  if (!isError(data.accounts)) view.append(accountsSection(data.accounts));
  if (!isError(data.llm)) view.append(narrationSection(data.llm));
  view.append(maintenanceSection());
  return view;
}

/* --------------------------------------------------------------- prices */

function updateSection(u) {
  const status = el("p.note");
  const running = Boolean(u.running);

  const button = el("button.primary", {
    text: running ? "Updating…" : "Update prices now",
    disabled: running,
    onclick: async () => {
      button.disabled = true;
      button.textContent = "Fetching…";
      status.className = "note";
      status.textContent = "Fetching rates and prices. This can take a minute.";
      try {
        await post("/api/updates/run", {});
        // The job runs in a background thread; poll until it stops reporting
        // itself as running rather than guessing how long it will take.
        await waitForFinish(status);
        router.refresh();
      } catch (err) {
        button.disabled = false;
        button.textContent = "Update prices now";
        status.className = "note bad";
        status.textContent = err.message;
      }
    },
  });

  return el(
    "section",
    heading("Price data"),
    el(
      "div.cards",
      card("Prices current to", f.date(u.last_price_date), {
        cls: u.stale ? "aside" : "total",
        sub: f.ago(u.last_price_date),
      }),
      card("Status", u.stale ? "Behind" : "Up to date", {
        valueCls: u.stale ? "loss" : "gain",
      }),
    ),
    el("div.actions", button),
    status,
    u.stale
      ? el("p.note.warn", {
          text:
            "Every value on this page is computed from the last price this " +
            "tool has. While it is behind, those figures are behind too.",
        })
      : null,
  );
}

async function waitForFinish(status) {
  for (let attempt = 0; attempt < 90; attempt++) {
    await new Promise((resolve) => window.setTimeout(resolve, 2000));
    try {
      const body = await get("/api/updates");
      if (!body.running) {
        const latest = body.runs?.[0];
        status.className = latest?.status === "OK" ? "note" : "note warn";
        status.textContent = latest
          ? `${latest.status}: ${latest.price_rows} new prices, ${latest.fx_rows} new rates.`
          : "Finished.";
        return;
      }
    } catch {
      // A transient failure while the job holds a write lock is expected.
    }
  }
  status.textContent = "Still running. Reload the page to see the result.";
}

/* ------------------------------------------------------------- schedule */

function scheduleSection(scheduler) {
  const section = el("section", heading("Update automatically every day"));

  if (!scheduler || !scheduler.supported) {
    section.append(
      el("p.note", {
        text:
          scheduler?.problem ||
          "Automatic daily updates use Windows Task Scheduler, which is not available here.",
      }),
    );
    return section;
  }

  const time = el("input", {
    type: "time",
    value: scheduler.time || "18:30",
    step: "60",
  });
  const status = el("p.note");

  const save = async () => {
    status.className = "note";
    status.textContent = "Asking Windows to schedule it…";
    try {
      await post("/api/scheduler", { time: time.value });
      router.refresh();
    } catch (err) {
      status.className = "note bad";
      status.textContent = err.message;
    }
  };

  const remove = async () => {
    if (!window.confirm("Stop updating automatically?")) return;
    try {
      await del("/api/scheduler");
      router.refresh();
    } catch (err) {
      status.className = "note bad";
      status.textContent = err.message;
    }
  };

  const testRun = async () => {
    status.className = "note";
    status.textContent = "Asked Windows to run it now. Check the history below in a minute.";
    try {
      await post("/api/scheduler/run-now", {});
    } catch (err) {
      status.className = "note bad";
      status.textContent = err.message;
    }
  };

  section.append(
    el(
      "div.panel",
      el("p", {
        text: scheduler.registered
          ? `Scheduled: every day at ${scheduler.time}. If the machine is off at that time, it runs at the next opportunity and picks up every day it missed.`
          : "Not scheduled. Prices will only update when you press the button above.",
      }),
      el(
        "form.row",
        { onsubmit: (e) => (e.preventDefault(), save()) },
        el("label.field", el("span", { text: "Run at" }), time),
        el("button.primary", {
          type: "submit",
          text: scheduler.registered ? "Change the time" : "Turn it on",
        }),
        scheduler.registered ? el("button", { type: "button", text: "Test it now", onclick: testRun }) : null,
        scheduler.registered ? el("button.danger", { type: "button", text: "Turn it off", onclick: remove }) : null,
      ),
      status,
      scheduler.registered && scheduler.command
        ? el("p.note", { text: `Runs: ${scheduler.command}` })
        : null,
    ),
    el("p.note", {
      text:
        "Evening is a good time: European markets close at 17:30 CET, and " +
        "closing prices are published shortly afterwards.",
    }),
  );
  return section;
}

function historySection(runs) {
  if (!runs?.length) return el("div");
  const rows = runs.map((r) =>
    row([
      f.date(r.started_at),
      { node: statusTag(r) },
      r.trigger.toLowerCase(),
      r.price_rows,
      r.fx_rows,
      r.rebuilt ? "yes" : "no",
      {
        text: r.problems.length ? `${r.problems.length}` : "—",
        title: r.problems.join("\n") || undefined,
        cls: r.problems.length ? "loss" : "",
      },
    ]),
  );
  return el(
    "section",
    heading("Recent updates"),
    el(
      "div.wrap",
      el(
        "table",
        el("thead", el("tr", ["When", "Result", "Started by", "Prices", "Rates", "Recomputed", "Problems"].map((t) => el("th", { text: t })))),
        el("tbody", rows),
      ),
    ),
    el("p.note", {
      text:
        "“Partial” means some prices arrived and some did not — usually one " +
        "provider being briefly unavailable. It is not an error, and the next " +
        "run picks up whatever was missed.",
    }),
  );
}

function statusTag(r) {
  const cls = { OK: "ok", PARTIAL: "warn", FAILED: "bad", SKIPPED: "", RUNNING: "" }[r.status] || "";
  return el("span", {
    class: `tag ${cls}`,
    text: r.status.toLowerCase(),
    title: r.error || undefined,
  });
}

/* ------------------------------------------------------------------ goal */

function goalSection(g) {
  const input = el("input", {
    type: "number",
    step: "any",
    min: "0",
    value: g.target_eur ?? "",
  });
  const status = el("p.note");
  return el(
    "section",
    heading("Goal"),
    el(
      "form.row",
      {
        onsubmit: async (e) => {
          e.preventDefault();
          try {
            await put("/api/goal", { target_eur: input.value });
            status.className = "note";
            status.textContent = "Saved.";
          } catch (err) {
            status.className = "note bad";
            status.textContent = err.message;
          }
        },
      },
      el("label.field", el("span", { text: "Target amount (EUR)" }), input),
      el("button.primary", { type: "submit", text: "Save" }),
    ),
    status,
  );
}

/* ---------------------------------------------------------------- health */

function healthSection(h) {
  const q = h.data_quality || {};
  const entries = [
    ["Holdings priced from an older day", q.stale_or_missing_prices, "#/holdings"],
    ["Funds with no partial exemption rate", q.unresolved_teilfreistellung, null],
    ["Holdings outside German investment tax", q.instruments_out_of_tax_scope, "#/tax"],
    ["Trades with no dealing fee recorded", q.trades_without_fees, "#/trades"],
    ["Cash movements that were inferred", q.inferred_flows, "#/inbox"],
    ["Quantities that were estimated", q.estimated_quantities, null],
  ].filter(([, count]) => count);

  return el(
    "section",
    heading("What the numbers rest on"),
    entries.length
      ? el(
          "div.wrap",
          el(
            "table",
            el("tbody", entries.map(([label, count, href]) =>
              row([
                { node: href ? el("a", { href, text: label }) : el("span", { text: label }), cls: "name" },
                count,
              ]),
            )),
          ),
        )
      : el("p.note", { text: "Nothing outstanding — every figure rests on recorded evidence." }),
    el("p.note", {
      text:
        "These are not errors. They are the places where a figure rests on " +
        "something weaker than a contract note, and they are listed so you can " +
        "discount the numbers accordingly.",
    }),
  );
}

/* ------------------------------------------------------------------ tax */

function taxSection(constants) {
  return el(
    "section",
    heading("Published tax rates"),
    el("p.note", { text: constants.source }),
    constants.missing_years?.length
      ? el("p.note.warn", {
          text: `No base rate on file for ${constants.missing_years.join(", ")}. Tax figures for those years cannot be computed.`,
        })
      : null,
    el("p", el("a", { href: "#/tax", text: "Set them on the Tax page" })),
  );
}

/* ------------------------------------------------------------- accounts */

function accountsSection(body) {
  const rows = (body.accounts || []).map((a) =>
    row([
      { text: a.name, cls: "name" },
      a.kind.toLowerCase(),
      a.currency,
      a.broker || "—",
      a.active ? "active" : "closed",
    ]),
  );
  return el(
    "section",
    heading("Accounts"),
    el(
      "div.wrap",
      el(
        "table",
        el("thead", el("tr", ["Name", "Kind", "Currency", "Bank", ""].map((t) => el("th", { text: t })))),
        el("tbody", rows),
      ),
    ),
  );
}

/* ---------------------------------------------------------- maintenance */

function maintenanceSection() {
  const out = el("div");

  const recompute = el("button", {
    text: "Recompute everything",
    onclick: async () => {
      recompute.disabled = true;
      recompute.textContent = "Recomputing…";
      try {
        const body = await post("/api/admin/recompute", {});
        out.replaceChildren(
          el("p.note", {
            text: `Rebuilt ${body.position_days} position-days from ${body.lots_created} lots.`,
          }),
          ...(body.breaks || []).map((b) => el("p.note.bad", { text: b })),
        );
      } catch (err) {
        out.replaceChildren(el("p.note.bad", { text: err.message }));
      }
      recompute.disabled = false;
      recompute.textContent = "Recompute everything";
    },
  });

  const check = el("button", {
    text: "Run integrity checks",
    onclick: async () => {
      check.disabled = true;
      check.textContent = "Checking…";
      try {
        const body = await get("/api/admin/doctor");
        const findings = [...body.errors, ...body.warnings];
        out.replaceChildren(
          findings.length
            ? el(
                "div",
                findings.map((finding) =>
                  el("p", {
                    class: finding.severity === "ERROR" ? "note bad" : "note warn",
                    text: `${finding.code}: ${finding.message}`,
                  }),
                ),
              )
            : el("p.note", { text: "Everything checks out." }),
        );
      } catch (err) {
        out.replaceChildren(el("p.note.bad", { text: err.message }));
      }
      check.disabled = false;
      check.textContent = "Run integrity checks";
    },
  });

  return el(
    "section",
    heading("Maintenance"),
    el("p.note", {
      text:
        "Recomputing throws away every derived figure and rebuilds it from " +
        "your trades, prices and rates. It cannot lose anything you recorded, " +
        "and it is the correct fix for any figure that looks wrong.",
    }),
    el("div.actions", recompute, check),
    out,
    el("p", el("a", { href: "/debug", text: "Server-rendered debug page" })),
  );
}


/* ------------------------------------------------------- written summaries */

/*
 * Switched off until somebody turns it on, and the panel says in one sentence
 * exactly what would leave the machine. Sending a household's figures to a
 * third party is a decision to make deliberately, not one to discover has
 * already been made.
 */
function narrationSection(llm) {
  const key = el("input", {
    type: "password",
    placeholder: llm.configured ? llm.hint : "AIza…",
    autocomplete: "off",
  });
  const model = el("input", { type: "text", value: llm.model });
  const cap = el("input", { type: "number", min: "1", max: "1000", value: String(llm.daily_cap) });
  const enabled = el("input", { type: "checkbox", checked: llm.enabled });
  const amounts = el("input", { type: "checkbox", checked: llm.send_amounts });
  const status = el("p.note");
  const models = el("div");

  const save = async () => {
    status.className = "note";
    status.textContent = "Saving…";
    try {
      const body = {
        model: model.value.trim(),
        enabled: enabled.checked,
        daily_cap: Number(cap.value),
        send_amounts: amounts.checked,
      };
      if (key.value.trim()) body.api_key = key.value.trim();
      await put("/api/settings/llm", body);
      router.refresh();
    } catch (err) {
      status.className = "note bad";
      status.textContent = err.message;
    }
  };

  const listModels = async () => {
    models.replaceChildren(el("p.note", { text: "Asking which models this key can use…" }));
    try {
      const body = await get("/api/settings/llm/models");
      if (body.problem) {
        models.replaceChildren(el("p.note.bad", { text: body.problem }));
        return;
      }
      // A picker rather than a text field: model names change, and a field
      // that lets you type one lets you type one that does not exist.
      const select = el(
        "select",
        body.models.map((m) =>
          el("option", { value: m, text: m, selected: m === body.current }),
        ),
      );
      select.addEventListener("change", () => {
        model.value = select.value;
      });
      models.replaceChildren(
        el("label.field", el("span", { text: "Models this key can use" }), select),
      );
    } catch (err) {
      models.replaceChildren(el("p.note.bad", { text: err.message }));
    }
  };

  const forget = async () => {
    if (!window.confirm("Remove the key and switch written summaries off?")) return;
    try {
      await del("/api/settings/llm/key");
      router.refresh();
    } catch (err) {
      status.className = "note bad";
      status.textContent = err.message;
    }
  };

  return el(
    "section",
    heading("Written summaries"),
    el("p.note", { text: llm.what_leaves_this_machine }),
    el(
      "div.panel",
      el("p", {
        text: llm.configured
          ? `A key is stored (${llm.hint}). Written summaries are ${llm.enabled ? "on" : "off"}. ` +
            `${llm.calls_today} of ${llm.daily_cap} used today.`
          : "No key stored. Get a free one from Google AI Studio and paste it here.",
      }),
      el(
        "form.entry",
        { onsubmit: (e) => (e.preventDefault(), save()) },
        el(
          "label.field",
          el("span", { text: llm.configured ? "Replace the key" : "API key" }),
          key,
          el("span.note", { text: "stored on this machine; never shown again" }),
        ),
        el("label.field", el("span", { text: "Model" }), model),
        el("label.field", el("span", { text: "Daily limit" }), cap),
        el("label.check.wide", enabled, el("span", { text: "Write summaries when I ask for them" })),
        el(
          "label.check.wide",
          amounts,
          el("span", { text: "Include amounts in euros" }),
          el("span.note", {
            text: " — untick to send only percentages and differences",
          }),
        ),
        el(
          "div.wide.actions",
          el("button.primary", { type: "submit", text: "Save" }),
          el("button", { type: "button", text: "Which models?", onclick: listModels }),
          llm.configured
            ? el("button.danger", { type: "button", text: "Forget the key", onclick: forget })
            : null,
        ),
      ),
      models,
      status,
    ),
    el("p", el("a", { href: "#/settings?log=llm", text: "Read what has been sent", onclick: showLog })),
  );
}

/** The audit trail: exactly what was sent, and what came back. */
async function showLog(event) {
  event.preventDefault();
  const target = event.target;
  target.replaceWith(el("p.note", { text: "Loading…" }));
  try {
    const body = await get("/api/llm/runs", { limit: 20 });
    const rows = (body.runs || []).map((r) =>
      row([
        f.date(r.created_at),
        r.task,
        { node: el("span.tag", { text: r.status.toLowerCase() }) },
        { text: r.output_text || r.error || "—", cls: "name", title: r.input_json },
      ]),
    );
    document.querySelector("#view").append(
      el(
        "section",
        heading("What has been sent"),
        rows.length
          ? el(
              "div.wrap",
              el(
                "table",
                el("thead", el("tr", ["When", "About", "Result", "What came back"].map((t) => el("th", { text: t })))),
                el("tbody", rows),
              ),
            )
          : el("p.nodata", { text: "Nothing has been sent." }),
        el("p.note", {
          text:
            "Hover a row to see the exact JSON that was sent. Every number in a " +
            "summary should appear in it — that is how you check the model did " +
            "not invent one.",
        }),
      ),
    );
  } catch (err) {
    document.querySelector("#view").append(el("p.note.bad", { text: err.message }));
  }
}
