/*
 * The alert strip under the header.
 *
 * This is the single most important piece of chrome in a tool whose owner does
 * not use a terminal. Previously the app knew that prices were three days old,
 * that four inferences were waiting to be confirmed, and that the derived
 * figures were stale -- and said so only in a JSON field nobody reads, or in a
 * CLI command nobody runs. The result is a page showing plausible, wrong
 * numbers with no visible sign anything is amiss.
 *
 * Each alert names what is wrong and links to the screen that fixes it.
 * Nothing here fixes anything by itself: the alert is the notification, the
 * screen is the decision.
 */

import { el } from "./dom.js";
import { get, all, isError } from "./api.js";
import { ago } from "./fmt.js";

const strip = () => document.getElementById("alerts");

function alert(level, what, actionLabel, href) {
  return el(
    `div.alert${level ? "." + level : ""}`,
    el("span.what", { text: what }),
    actionLabel ? el("a", { href, text: actionLabel }) : null,
  );
}

/** Re-read the health endpoints and redraw the strip. Never throws. */
export async function refreshAlerts() {
  const box = strip();
  if (!box) return;

  const data = await all({
    health: get("/api/health/data"),
    pending: get("/api/pending"),
  });

  const items = [];

  if (localStorage.getItem("pt_dismiss_lybk_split_reminder") !== "true") {
    const splitAlert = el(
      "div.alert.warn.alert-lybk-split",
      el("span.what", {
        text: "LYBK Stock Split (1:80): Holding adjusted to 1,944.88 shares. Remember to double-check against your bank depot when your bank completes processing.",
      }),
      el("a", {
        href: "#",
        text: "Dismiss",
        onclick: (e) => {
          e.preventDefault();
          localStorage.setItem("pt_dismiss_lybk_split_reminder", "true");
          splitAlert.remove();
          if (box.children.length === 0) box.hidden = true;
          document.getElementById("lybk-split-banner")?.remove();
        },
      }),
    );
    items.push(splitAlert);
  }

  if (!isError(data.health)) {
    const h = data.health;

    // The oldest price behind an owned position, not the newest row: a cash
    // proxy is "priced" every day and would otherwise make stale ETFs look current.
    const pricedTo = h.priced_to || h.as_of;
    if (pricedTo) {
      const stamp = document.getElementById("asof");
      if (stamp) stamp.textContent = `Priced to ${ago(pricedTo)}`;
    }

    // The one that changes what every other figure means: money that funded a
    // purchase but was never recorded is read as investment return.
    const unexplained = h.data_quality?.unexplained_purchases ?? 0;
    if (unexplained > 0) {
      items.push(
        alert(
          "bad",
          `${unexplained} purchases were made with money this tool has no record of. ` +
            "Your return is overstated until you say where it came from.",
          "Sort it out",
          "#/trades?review=funding",
        ),
      );
    }

    // Stale prices. The number that makes every other number on the page a
    // little bit wrong, and the one thing the app used to be silent about.
    const stale = h.data_quality?.stale_or_missing_prices ?? 0;
    if (stale > 0) {
      items.push(
        alert(
          "warn",
          stale === 1 ? "1 holding has a stale price." : `${stale} holdings have stale prices.`,
          "Update prices",
          "#/settings",
        ),
      );
    }

    if (h.completeness && h.completeness !== "FULL") {
      items.push(
        alert(
          "warn",
          "Latest day is incomplete.",
          "What is missing",
          "#/settings",
        ),
      );
    }

    const unresolved = h.data_quality?.unresolved_teilfreistellung ?? 0;
    if (unresolved > 0) {
      items.push(
        alert(
          "warn",
          `${unresolved} ${unresolved === 1 ? "fund is" : "funds are"} missing a tax exemption rate.`,
          "Set the rates",
          "#/settings",
        ),
      );
    }
  }

  if (!isError(data.pending)) {
    // A decision was confirmed but the derived layer has not caught up, so
    // every figure on the page predates it. This one is actionable in one
    // click, which is why it is listed first.
    if (data.pending.rebuild_required) {
      items.unshift(
        alert(
          "warn",
          "Figures are older than your last change.",
          "Recompute now",
          "#/settings",
        ),
      );
    }

    // Only what is actionable today. Anything dated in the future is on the
    // Inbox page and does not nag from here.
    const open = data.pending.due_now ?? 0;
    if (open > 0) {
      items.push(
        alert(
          "",
          open === 1 ? "1 entry needs a decision." : `${open} entries need a decision.`,
          "Review them",
          "#/inbox",
        ),
      );
    }
  }

  box.replaceChildren(...items);
  box.hidden = items.length === 0;
}
