/*
 * A tiny element builder. The whole point is that it has no way to write HTML.
 *
 * Instrument names in this database come out of a broker's CSV export. They
 * are attacker-controlled in the only sense that matters here: nobody audited
 * them, and one containing `<img onerror=...>` would execute if it were ever
 * concatenated into innerHTML. The old server-rendered page did exactly that
 * in its sale-pricing script, and that habit is not carried over.
 *
 * So: text always goes through textContent, and tests/test_static_assets.py
 * fails the build if `innerHTML` appears anywhere under js/.
 */

/**
 * Build an element.
 *
 * @param {string} tag - tag name, optionally with `.class.names`
 * @param {object|null} attrs - attributes; `class`, `text`, `on*` handled specially
 * @param {...(Node|string|null|undefined|false|Array)} children
 */
export function el(tag, attrs, ...children) {
  const [name, ...classes] = String(tag).split(".");
  const node = document.createElement(name || "div");
  if (classes.length) node.classList.add(...classes);

  if (attrs && typeof attrs === "object" && !(attrs instanceof Node) && !Array.isArray(attrs)) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") {
        node.classList.add(...String(value).split(/\s+/).filter(Boolean));
      } else if (key === "text") {
        node.textContent = String(value);
      } else if (key === "dataset") {
        Object.assign(node.dataset, value);
      } else if (key.startsWith("on") && typeof value === "function") {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (value === true) {
        node.setAttribute(key, "");
      } else {
        node.setAttribute(key, String(value));
      }
    }
  } else if (attrs !== null && attrs !== undefined) {
    children.unshift(attrs);
  }

  append(node, children);
  return node;
}

/** Append children, flattening arrays and skipping nullish/false entries. */
export function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === "") continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** Replace an element's children in one go. */
export function replace(node, ...children) {
  node.replaceChildren();
  return append(node, children);
}

/** A `<tr>` of `<td>`s. Cells may be strings, numbers, nodes, or {text, cls, title}. */
export function row(cells, { onClick = null } = {}) {
  const tr = el("tr");
  for (const cell of cells) {
    const spec = cell && typeof cell === "object" && !(cell instanceof Node) ? cell : { text: cell };
    const td = el("td");
    if (spec.cls) td.classList.add(...String(spec.cls).split(/\s+/).filter(Boolean));
    if (spec.title) td.title = spec.title;
    if (spec.colspan) td.colSpan = spec.colspan;
    if (spec.node) td.append(spec.node);
    else td.textContent = spec.text === null || spec.text === undefined ? "—" : String(spec.text);
    tr.append(td);
  }
  if (onClick) {
    tr.style.cursor = "pointer";
    tr.addEventListener("click", onClick);
  }
  return tr;
}

/** A full table. `head` is an array of column labels; `rows` an array of `<tr>`. */
export function table(head, rows, { empty = "Nothing to show." } = {}) {
  if (!rows.length) return el("p.nodata", { text: empty });
  return el(
    "div.wrap",
    el(
      "table",
      el("thead", el("tr", head.map((h) => el("th", { text: h })))),
      el("tbody", rows),
    ),
  );
}

/** A labelled statistic card. */
export function card(label, value, { sub = null, cls = "", valueCls = "" } = {}) {
  return el(
    `div.card${cls ? "." + cls.split(/\s+/).join(".") : ""}`,
    el("span.k", { text: label }),
    el("div.v", { class: valueCls, text: value }),
    sub ? el("div.sub2", { text: sub }) : null,
  );
}

/** A section heading. `german` renders in parentheses, muted, never alone. */
export function heading(text, german = null) {
  const h = el("h2", { text });
  if (german) {
    h.append(" ", el("span.de", { lang: "de", text: `(${german})` }));
  }
  return h;
}

/** An anchor into the hash router. */
export function link(href, text, attrs = {}) {
  return el("a", { href, text, ...attrs });
}
