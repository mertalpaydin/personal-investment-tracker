/*
 * Boot: build the nav, install the routes, start the router.
 *
 * Views are imported dynamically so that a syntax error in one screen breaks
 * that screen rather than the whole app -- the router catches the rejected
 * import and renders it as an error panel, and every other screen still works.
 */

import { el } from "./dom.js";
import * as router from "./router.js";
import { destroyAll } from "./charts.js";
import { refreshAlerts } from "./alerts.js";
import {
  ApiError,
  getSnapshotMeta,
  initApiMode,
  isStaticMode,
  isUnlocked,
  lockSnapshot,
  unlockSnapshot,
} from "./api.js";

//: The nav, in the order the screens are used: look at it, drill into it,
//: record something, check the tax, compare, deal with the inbox, configure.
const NAV = [
  { href: "#/", pattern: "/", label: "Overview" },
  { href: "#/holdings", pattern: "/holdings", label: "Holdings" },
  { href: "#/rebalance", pattern: "/rebalance", label: "Rebalance" },
  { href: "#/trades", pattern: "/trades", label: "Trades" },
  { href: "#/plan", pattern: "/plan", label: "Plan" },
  { href: "#/tax", pattern: "/tax", label: "Tax" },
  { href: "#/watchlist", pattern: "/watchlist", label: "Watchlist" },
  { href: "#/stocks", pattern: "/stocks", label: "Stocks" },
  { href: "#/inbox", pattern: "/inbox", label: "Inbox" },
  { href: "#/settings", pattern: "/settings", label: "Settings" },
];

const VIEWS = {
  "/": () => import("./views/overview.js?v=8"),
  "/holdings": () => import("./views/holdings.js?v=8"),
  "/holdings/:key": () => import("./views/holding.js?v=8"),
  "/rebalance": () => import("./views/rebalance.js?v=8"),
  "/trades": () => import("./views/trades.js?v=8"),
  "/plan": () => import("./views/plan.js?v=8"),
  "/tax": () => import("./views/tax.js?v=8"),
  "/watchlist": () => import("./views/watchlist.js?v=8"),
  "/stocks": () => import("./views/stocks.js?v=8"),
  "/inbox": () => import("./views/inbox.js?v=8"),
  "/settings": () => import("./views/settings.js?v=8"),
};

function buildNav() {
  const nav = document.getElementById("nav");
  nav.replaceChildren();
  for (const item of NAV) {
    nav.append(el("a", { href: item.href, text: item.label, "data-pattern": item.pattern }));
  }
}

function markNav(pattern) {
  for (const a of document.querySelectorAll("#nav a")) {
    const own = a.getAttribute("data-pattern");
    // A holding detail page is still "Holdings" as far as the nav is concerned.
    const active = own === pattern || (own === "/holdings" && pattern === "/holdings/:key");
    if (active) {
      a.setAttribute("aria-current", "page");
    } else {
      a.removeAttribute("aria-current");
    }
  }
}

/**
 * Render a thrown error as something a reader can act on.
 *
 * This backend explains its refusals in prose -- "no Basiszins published for
 * 2026" rather than a bare 409 -- so the detail string is the message, and the
 * status only decides the heading.
 */
function errorPanel(error) {
  const isApi = error instanceof ApiError;
  const heading = !isApi
    ? "Something went wrong on this page"
    : error.isMissing
      ? "There is nothing here yet"
      : error.isRefusal
        ? "This needs an input that is missing"
        : "The server refused this request";

  return el(
    "div.panel.bad",
    el("h3", { text: heading }),
    el("p", { text: error.message || String(error) }),
    isApi && error.url ? el("p.note", { text: error.url }) : null,
    el(
      "div.actions",
      el("button", { text: "Try again", onclick: () => router.refresh() }),
      el("button", { text: "Settings", onclick: () => router.go("#/settings") }),
    ),
  );
}

const THEMES = ["system", "light", "dark"];
const THEME_LABELS = {
  system: "Theme: Auto 💻",
  light: "Theme: Light ☀️",
  dark: "Theme: Dark 🌙",
};

function initThemeToggle() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;

  const getSaved = () => localStorage.getItem("pt_theme") || "system";

  const applyTheme = (theme) => {
    if (theme === "dark" || theme === "light") {
      document.documentElement.setAttribute("data-theme", theme);
      localStorage.setItem("pt_theme", theme);
    } else {
      document.documentElement.removeAttribute("data-theme");
      localStorage.removeItem("pt_theme");
    }
    btn.textContent = THEME_LABELS[theme] || THEME_LABELS.system;
  };

  btn.addEventListener("click", () => {
    const current = getSaved();
    const nextIndex = (THEMES.indexOf(current) + 1) % THEMES.length;
    applyTheme(THEMES[nextIndex]);
  });

  applyTheme(getSaved());
}

let routerStarted = false;

function syncStaticChrome() {
  const nav = document.getElementById("nav");
  const right = document.querySelector(".topbar-right");
  const alerts = document.getElementById("alerts");
  if (!isStaticMode()) return;

  let lockBtn = document.getElementById("lock-toggle");
  if (!lockBtn && right) {
    lockBtn = el("button#lock-toggle.theme-toggle", {
      type: "button",
      title: "Lock encrypted portfolio snapshot",
      text: "Lock 🔒",
      onclick: () => {
        destroyAll();
        lockSnapshot();
        syncStaticChrome();
        showUnlockScreen();
      },
    });
    right.prepend(lockBtn);
  }

  const unlocked = isUnlocked();
  if (nav) nav.hidden = !unlocked;
  if (lockBtn) lockBtn.hidden = !unlocked;
  if (!unlocked && alerts) alerts.hidden = true;

  const meta = getSnapshotMeta();
  const asof = document.getElementById("asof");
  if (unlocked && meta && asof && !asof.textContent) {
    asof.textContent = `Snapshot ${meta.as_of || ""}`;
  }
}

function showUnlockScreen() {
  const mount = document.getElementById("view");
  if (!mount) return;

  const tokenInput = el("input.unlock-input", {
    type: "password",
    placeholder: "Enter access token…",
    autocomplete: "current-password",
    required: true,
  });
  const rememberCheck = el("input", { type: "checkbox", checked: true });
  const submitBtn = el("button.primary.unlock-submit", {
    type: "submit",
    text: "Unlock Portfolio",
  });
  const statusMsg = el("p.note.bad", { hidden: true });

  const onSubmit = async (e) => {
    e.preventDefault();
    const val = tokenInput.value.trim();
    if (!val) return;
    submitBtn.disabled = true;
    submitBtn.textContent = "Decrypting…";
    statusMsg.hidden = true;
    try {
      await unlockSnapshot(val, rememberCheck.checked);
      syncStaticChrome();
      startOrRefreshRouter();
    } catch (err) {
      statusMsg.textContent = err.message || "Could not unlock snapshot.";
      statusMsg.hidden = false;
      submitBtn.disabled = false;
      submitBtn.textContent = "Unlock Portfolio";
      tokenInput.select();
    }
  };

  const screen = el(
    "div.unlock-wrapper",
    el(
      "div.card.unlock-card",
      el("div.unlock-icon", { text: "🔒" }),
      el("h1", { text: "Private Portfolio" }),
      el("p.sub", {
        text:
          "This static snapshot is encrypted with AES-256-GCM. " +
          "Enter your personal access token to decrypt it locally in your browser.",
      }),
      el(
        "form.unlock-form",
        { onsubmit: onSubmit },
        el(
          "label.field",
          el("span", { text: "Access Token" }),
          tokenInput,
        ),
        el(
          "label.check",
          rememberCheck,
          el("span", { text: "Remember token on this device" }),
        ),
        statusMsg,
        submitBtn,
      ),
      el("p.note.unlock-hint", {
        text:
          "Zero-knowledge encryption: your figures are decrypted in memory using Web Crypto API and never leave your device.",
      }),
    ),
  );

  mount.replaceChildren(screen);
  setTimeout(() => tokenInput.focus(), 50);
}

function startOrRefreshRouter() {
  if (!routerStarted) {
    routerStarted = true;
    router.start({
      mount: document.getElementById("view"),
      onError: errorPanel,
      onRender: ({ pattern }) => {
        markNav(pattern);
        // Alerts are chrome, not part of any view: they must survive navigation
        // and must not be re-fetched by every screen that happens to care.
        refreshAlerts();
      },
    });
  } else {
    router.refresh();
  }
}

async function boot() {
  buildNav();
  initThemeToggle();

  for (const [pattern, load] of Object.entries(VIEWS)) {
    router.route(pattern, async (ctx) => {
      if (isStaticMode() && !isUnlocked()) {
        showUnlockScreen();
        return document.getElementById("view").firstChild;
      }
      const module = await load();
      return module.render(ctx);
    });
  }

  router.fallback(() =>
    el(
      "div.empty",
      el("h3", { text: "No such page" }),
      el("p", el("a", { href: "#/", text: "Back to the overview" })),
    ),
  );

  await initApiMode();
  syncStaticChrome();

  if (isStaticMode() && !isUnlocked()) {
    showUnlockScreen();
    return;
  }

  startOrRefreshRouter();
}

// Every navigation replaces the view wholesale, so charts belonging to the
// outgoing screen have to be released or their observers leak against nodes
// that no longer exist.
window.addEventListener("hashchange", destroyAll);

boot().catch((err) => {
  const mount = document.getElementById("view");
  if (mount) mount.replaceChildren(errorPanel(err));
});
