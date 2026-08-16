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

  if (!isError(data.health)) {
    const h = data.health;

    if (h.as_of) {
      const stamp = document.getElementById("asof");
      if (stamp) stamp.textContent = `Priced to ${ago(h.as_of)}`;
    }

    // Stale prices. The number that makes every other number on the page a
    // little bit wrong, and the one thing the app used to be silent about.
    const stale = h.data_quality?.stale_or_missing_prices ?? 0;
    if (stale > 0) {
      items.push(
        alert(
          "warn",
          stale === 1
            ? "One holding is being valued at a carried-forward price."
            : `${stale} holdings are being valued at carried-forward prices.`,
          "Update prices",
          "#/settings",
        ),
      );
    }

    if (h.completeness && h.completeness !== "FULL") {
      items.push(
        alert(
          "warn",
          "Some figures are computed from incomplete data for the latest day.",
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
          `${unresolved} ${unresolved === 1 ? "fund has" : "funds have"} no partial tax ` +
            "exemption rate set, so their tax figures are incomplete.",
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
          "Figures on this page are older than your most recent decision.",
          "Recompute now",
          "#/settings",
        ),
      );
    }

    const open = (data.pending.entries || []).length;
    if (open > 0) {
      items.push(
        alert(
          "",
          open === 1
            ? "One inferred entry is waiting for you to confirm or reject it."
            : `${open} inferred entries are waiting for you to confirm or reject them.`,
          "Review them",
          "#/inbox",
        ),
      );
    }
  }

  box.replaceChildren(...items);
  box.hidden = items.length === 0;
}
