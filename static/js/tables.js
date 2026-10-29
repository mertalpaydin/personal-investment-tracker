/*
 * Interactive, client-side sortable and filterable table builder.
 *
 * Designed to provide spreadsheet-like ergonomics (column sort, instant text
 * search, row count) without any external dependencies or frameworks, while
 * strictly adhering to the project's zero-innerHTML safety guarantee.
 */

import { el, row } from "./dom.js";

/**
 * Extract a numeric value from a cell for reliable numerical sorting.
 * Strips currency symbols, percent signs, basis points, and commas.
 */
function parseNumeric(val) {
  if (val === null || val === undefined || val === "" || val === "—") return -Infinity;
  if (typeof val === "number") return val;
  const cleaned = String(val)
    .replace(/[€$£%\s+,bp]/g, "")
    .trim();
  const num = Number(cleaned);
  return Number.isNaN(num) ? -Infinity : num;
}

/**
 * @typedef {Object} ColumnDef
 * @property {string} label - Header text displayed to user
 * @property {string|function} [key] - Property key on item, or function(item) -> value
 * @property {string|function} [render] - Optional custom cell renderer function(item, index) -> cell spec
 * @property {string} [type] - 'num' | 'pct' | 'str' | 'date' | 'none'
 * @property {string} [cls] - CSS class for <td>
 * @property {string} [align] - 'left' | 'right' | 'center'
 * @property {boolean} [sortable] - Defaults to true if type !== 'none'
 * @property {boolean} [defaultSort] - If true, initial table sort is by this column
 * @property {'asc'|'desc'} [defaultDir] - Initial sort direction
 */

/**
 * Build an interactive sortable and filterable data table.
 *
 * @param {ColumnDef[]} columns - Column specifications
 * @param {Array<Object>} items - Array of data objects
 * @param {Object} [options]
 * @param {string} [options.placeholder] - Search input placeholder
 * @param {string[]} [options.searchKeys] - Keys to check during text search
 * @param {string} [options.emptyText] - Message when no items match
 * @param {boolean} [options.searchable] - Whether to show the search input (default true if items > 3)
 * @param {function} [options.onRowClick] - Callback function(item) when clicking a row
 * @returns {HTMLElement} - The full interactive container
 */
export function sortableTable(columns, items, options = {}) {
  const {
    placeholder = "Filter by symbol, name, category…",
    searchKeys = ["instrument", "name", "symbol", "category", "bucket", "asset_class", "notes"],
    emptyText = "No matching items found.",
    searchable = items.length > 2,
    onRowClick = null,
    pageSize = null,
    allowShowAll = true,
  } = options;

  let query = "";
  let sortColIndex = columns.findIndex((c) => c.defaultSort);
  let sortDir = sortColIndex >= 0 ? columns[sortColIndex].defaultDir || "asc" : "asc";
  let activePageSize = pageSize;
  let currentPage = 1;

  // If no defaultSort was specified, check if first column is sortable
  if (sortColIndex < 0 && columns.length && columns[0].sortable !== false) {
    sortColIndex = 0;
    sortDir = "asc";
  }

  const container = el("div.sortable-table-container");
  const countBadge = el("span.table-count-badge");
  const tbody = el("tbody");
  const paginationContainer = el("div.table-pagination");

  function getCellValue(col, item) {
    if (typeof col.key === "function") return col.key(item);
    if (typeof col.key === "string") return item[col.key];
    return null;
  }

  function getComparableValue(col, item) {
    const raw = getCellValue(col, item);
    const type = col.type || (typeof raw === "number" ? "num" : "str");
    if (type === "num" || type === "pct") return parseNumeric(raw);
    if (type === "date") return raw ? new Date(raw).getTime() : 0;
    return String(raw || "").toLowerCase();
  }

  function filterItems() {
    if (!query.trim()) return [...items];
    const q = query.toLowerCase().trim();
    return items.filter((item) => {
      for (const k of searchKeys) {
        const val = item[k];
        if (val !== null && val !== undefined && String(val).toLowerCase().includes(q)) {
          return true;
        }
      }
      return false;
    });
  }

  function sortFilteredItems(filtered) {
    if (sortColIndex < 0 || sortColIndex >= columns.length) return filtered;
    const col = columns[sortColIndex];
    if (col.sortable === false) return filtered;

    const dirMultiplier = sortDir === "asc" ? 1 : -1;
    return filtered.sort((a, b) => {
      const valA = getComparableValue(col, a);
      const valB = getComparableValue(col, b);
      if (valA < valB) return -1 * dirMultiplier;
      if (valA > valB) return 1 * dirMultiplier;
      return 0;
    });
  }

  function renderRows() {
    const filtered = filterItems();
    const sorted = sortFilteredItems(filtered);

    countBadge.textContent =
      query.trim() && filtered.length !== items.length
        ? `${filtered.length} of ${items.length}`
        : `${items.length} ${items.length === 1 ? "item" : "items"}`;

    tbody.replaceChildren();

    if (!sorted.length) {
      const emptyRow = el("tr", el("td", { colspan: columns.length, class: "empty-msg", text: emptyText }));
      tbody.append(emptyRow);
      paginationContainer.replaceChildren();
      return;
    }

    const totalItems = sorted.length;
    const isPaginated = activePageSize !== null && activePageSize > 0;
    const totalPages = isPaginated ? Math.max(1, Math.ceil(totalItems / activePageSize)) : 1;
    if (currentPage > totalPages) currentPage = totalPages;
    if (currentPage < 1) currentPage = 1;

    const pageItems = isPaginated
      ? sorted.slice((currentPage - 1) * activePageSize, currentPage * activePageSize)
      : sorted;
    const startIdx = isPaginated ? (currentPage - 1) * activePageSize : 0;

    for (let i = 0; i < pageItems.length; i++) {
      const item = pageItems[i];
      const actualIndex = startIdx + i;
      const cells = columns.map((col) => {
        if (typeof col.render === "function") {
          return col.render(item, actualIndex);
        }
        const val = getCellValue(col, item);
        return {
          text: val === null || val === undefined ? "—" : val,
          cls: col.cls || (col.type === "num" || col.type === "pct" ? "num" : ""),
        };
      });

      const tr = row(cells, {
        onClick: onRowClick ? () => onRowClick(item) : null,
      });
      tbody.append(tr);
    }

    // Render pagination controls if pageSize was specified
    paginationContainer.replaceChildren();
    if (pageSize !== null && pageSize > 0) {
      const startNum = totalItems === 0 ? 0 : startIdx + 1;
      const endNum = Math.min(startIdx + (activePageSize || totalItems), totalItems);
      const infoText = el("span.table-pagination-info", {
        text: isPaginated
          ? `Showing ${startNum}–${endNum} of ${totalItems} items`
          : `Showing all ${totalItems} items`,
      });

      const controls = el("div.table-pagination-controls");

      if (isPaginated && totalPages > 1) {
        const prevBtn = el("button.table-pagination-btn", {
          type: "button",
          text: "← Prev",
          disabled: currentPage <= 1,
          onclick: () => {
            if (currentPage > 1) {
              currentPage--;
              renderRows();
            }
          },
        });
        const pageLabel = el("span.table-pagination-page", {
          text: `Page ${currentPage} of ${totalPages}`,
          style: "font-variant-numeric: tabular-nums; padding: 0 0.25rem;",
        });
        const nextBtn = el("button.table-pagination-btn", {
          type: "button",
          text: "Next →",
          disabled: currentPage >= totalPages,
          onclick: () => {
            if (currentPage < totalPages) {
              currentPage++;
              renderRows();
            }
          },
        });
        controls.append(prevBtn, pageLabel, nextBtn);
      }

      if (allowShowAll && totalItems > pageSize) {
        const toggleAllBtn = el("button.table-pagination-btn", {
          type: "button",
          text: isPaginated ? `Show All (${totalItems})` : `Show ${pageSize} per page`,
          onclick: () => {
            activePageSize = isPaginated ? null : pageSize;
            currentPage = 1;
            renderRows();
          },
        });
        controls.append(toggleAllBtn);
      }

      paginationContainer.append(infoText, controls);
    }
  }

  function createHeader() {
    const tr = el("tr");
    columns.forEach((col, index) => {
      const isSortable = col.sortable !== false && col.type !== "none";
      const isCurrentSort = isSortable && index === sortColIndex;
      const th = el("th", {
        class: isSortable ? "sortable" : "",
        "aria-sort": isCurrentSort ? (sortDir === "asc" ? "ascending" : "descending") : "none",
      });

      const labelEl = el("span.th-label", { text: col.label });
      if (col.tooltip || col.title) {
        const infoEl = el("span.th-info", {
          text: " ⓘ",
          title: col.tooltip || col.title,
          style: "cursor: help; opacity: 0.7; font-size: 0.85em; margin-left: 2px;",
        });
        labelEl.append(infoEl);
      }

      const headerContent = el("span.th-content", labelEl);

      if (isSortable) {
        const sortIndicator = el("span.sort-icon", {
          text: isCurrentSort ? (sortDir === "asc" ? " ↑" : " ↓") : " ↕",
        });
        if (!isCurrentSort) sortIndicator.classList.add("muted");
        headerContent.append(sortIndicator);

        th.style.cursor = "pointer";
        th.title = (col.tooltip || col.title) ? `Sort by ${col.label} — ${col.tooltip || col.title}` : `Sort by ${col.label}`;
        th.addEventListener("click", () => {
          if (sortColIndex === index) {
            sortDir = sortDir === "asc" ? "desc" : "asc";
          } else {
            sortColIndex = index;
            sortDir = col.defaultDir || (col.type === "num" || col.type === "pct" ? "desc" : "asc");
          }
          currentPage = 1;
          // Update headers aria-sort and icons
          const thElements = tr.querySelectorAll("th");
          thElements.forEach((otherTh, otherIndex) => {
            const otherIcon = otherTh.querySelector(".sort-icon");
            if (otherIndex === sortColIndex) {
              otherTh.setAttribute("aria-sort", sortDir === "asc" ? "ascending" : "descending");
              if (otherIcon) {
                otherIcon.textContent = sortDir === "asc" ? " ↑" : " ↓";
                otherIcon.classList.remove("muted");
              }
            } else {
              otherTh.setAttribute("aria-sort", "none");
              if (otherIcon) {
                otherIcon.textContent = " ↕";
                otherIcon.classList.add("muted");
              }
            }
          });
          renderRows();
        });
      } else if (col.tooltip || col.title) {
        th.title = col.tooltip || col.title;
      }

      th.append(headerContent);
      tr.append(th);
    });
    return el("thead", tr);
  }

  // Build top control bar if searchable
  if (searchable) {
    const searchInput = el("input.table-search-input", {
      type: "search",
      placeholder,
      "aria-label": "Search table",
    });
    searchInput.addEventListener("input", (e) => {
      query = e.target.value;
      currentPage = 1;
      renderRows();
    });

    const toolbar = el(
      "div.table-toolbar",
      el("div.search-box", searchInput),
      el("div.table-meta", countBadge),
    );
    container.append(toolbar);
  }

  const tableElement = el("table.data-table", createHeader(), tbody);
  const wrap = el("div.wrap", tableElement);
  container.append(wrap);

  if (pageSize !== null && pageSize > 0) {
    container.append(paginationContainer);
  }

  renderRows();
  return container;
}
