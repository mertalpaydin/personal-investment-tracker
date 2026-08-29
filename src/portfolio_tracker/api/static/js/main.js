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
import { ApiError } from "./api.js";

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
  { href: "#/inbox", pattern: "/inbox", label: "Inbox" },
  { href: "#/settings", pattern: "/settings", label: "Settings" },
];

const VIEWS = {
  "/": () => import("./views/overview.js"),
  "/holdings": () => import("./views/holdings.js"),
  "/holdings/:key": () => import("./views/holding.js"),
  "/rebalance": () => import("./views/rebalance.js"),
  "/trades": () => import("./views/trades.js"),
  "/plan": () => import("./views/plan.js"),
  "/tax": () => import("./views/tax.js"),
  "/watchlist": () => import("./views/watchlist.js"),
  "/inbox": () => import("./views/inbox.js"),
  "/settings": () => import("./views/settings.js"),
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
    if (active) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
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

function boot() {
  buildNav();
  initThemeToggle();

  for (const [pattern, load] of Object.entries(VIEWS)) {
    router.route(pattern, async (ctx) => {
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
}

// Every navigation replaces the view wholesale, so charts belonging to the
// outgoing screen have to be released or their observers leak against nodes
// that no longer exist.
window.addEventListener("hashchange", destroyAll);

boot();
