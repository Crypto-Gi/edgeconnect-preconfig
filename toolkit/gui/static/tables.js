// Paged tables, copied unchanged from edgeconnect-orchestrator-automation 7af5e9d (static/app.js, class PagedTable). Load after
// app.js (it uses el, $ and api). Every value is written with textContent, never as HTML.
"use strict";

/* paged tables: the server searches, filters, sorts and pages; this only shows the page it sends */
const SHAPES = { ok: "●", bad: "■" };

function toneCell(text, tone) {
  return el("span", { class: "tone-" + tone, text: (SHAPES[tone] ? SHAPES[tone] + " " : "") + text });
}

function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== "" && value != null && value !== false) search.set(key, value === true ? "1" : value);
  return search.toString();
}

class PagedTable {
  // columns: [{ key, label, cell(row) -> node or text, mono }]; filters: [{ key, label }] drawn from the server's facets.
  constructor(options) {
    Object.assign(this, { filters: [], problems: "", exportName: "" }, options);
    this.params = Object.assign({ q: "", page: 1, size: 50, sort: "", order: "", problems: false }, ...this.filters.map((filter) => ({ [filter.key]: "" })));
    this.sequence = 0;
    this.timer = null;
    this.build();
  }

  build() {
    const id = this.id;
    this.search = el("input", { type: "search", id: id + "-q", placeholder: "Search", "aria-controls": id + "-grid", autocomplete: "off" });
    this.search.addEventListener("input", () => {
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.set({ q: this.search.value, page: 1 }), 250);
    });
    const toolbar = el("div", { class: "toolbar" }, el("label", { for: id + "-q", class: "sr-only", text: "Search " + this.label }), this.search);
    this.selects = {};
    for (const filter of this.filters) {
      const select = el("select", { id: id + "-" + filter.key, "aria-label": filter.label });
      select.addEventListener("change", () => this.set({ [filter.key]: select.value, page: 1 }));
      this.selects[filter.key] = select;
      toolbar.append(select);
    }
    if (this.problems) {
      const box = el("input", { type: "checkbox", id: id + "-problems" });
      box.addEventListener("change", () => this.set({ problems: box.checked, page: 1 }));
      toolbar.append(el("label", { class: "check", for: id + "-problems" }, box, this.problems));
    }
    const size = el("select", { id: id + "-size", "aria-label": "Rows per page" }, ...[25, 50, 100, 200].map((value) => el("option", { value: String(value), text: value + " per page" })));
    size.value = "50";
    size.addEventListener("change", () => this.set({ size: size.value, page: 1 }));
    toolbar.append(size);
    if (this.exportPath) {
      this.exportLink = el("a", { class: "btn", text: "Export CSV" });
      toolbar.append(this.exportLink);
    }
    this.head = el("tr");
    this.body = el("tbody");
    this.count = el("span", { class: "count", role: "status" });
    this.prev = el("button", { class: "btn", type: "button", text: "← Previous" });
    this.next = el("button", { class: "btn", type: "button", text: "Next →" });
    this.where = el("span", { class: "count" });
    this.prev.addEventListener("click", () => this.set({ page: this.params.page - 1 }));
    this.next.addEventListener("click", () => this.set({ page: this.params.page + 1 }));
    this.container.replaceChildren(toolbar,
      el("div", { class: "table-wrap" }, el("table", { id: id + "-grid" }, el("caption", { class: "sr-only", text: this.label }), el("thead", null, this.head), this.body)),
      el("div", { class: "pager" }, this.count, el("div", { class: "btns" }, this.prev, this.where, this.next)));
  }

  set(changes) {
    clearTimeout(this.timer);  // a search still being typed goes with this request, not after it
    Object.assign(this.params, { q: this.search.value }, changes);
    this.load();
  }

  reset() {
    Object.assign(this.params, { q: "", page: 1, problems: false }, ...this.filters.map((filter) => ({ [filter.key]: "" })));
    this.search.value = "";
    const box = $(this.id + "-problems");
    if (box) box.checked = false;
  }

  async load() {
    const path = this.path();
    if (!path) return;
    const sequence = ++this.sequence;
    this.container.setAttribute("aria-busy", "true");
    let data;
    try {
      data = await api(path + "?" + query(this.params));
    } catch (error) {
      if (sequence !== this.sequence) return;
      this.container.setAttribute("aria-busy", "false");
      this.body.replaceChildren(el("tr", null, el("td", { colspan: String(this.columns.length), class: "tone-bad", text: "Could not load: " + error.message })));
      return;
    }
    if (sequence !== this.sequence) return;  // a newer request is on its way; never show an older answer over it
    this.container.setAttribute("aria-busy", "false");
    this.show(this.table ? this.table(data) : data, data);
  }

  show(table, data) {
    Object.assign(this.params, { page: table.page, size: table.size, sort: table.sort, order: table.order });
    this.head.replaceChildren(...this.columns.map((column) => {
      const sorted = table.sort === column.key;
      const button = el("button", { type: "button", text: column.label + (sorted ? (table.order === "asc" ? " ↑" : " ↓") : "") });
      button.addEventListener("click", () => this.set({ sort: column.key, order: sorted && table.order === "asc" ? "desc" : "asc", page: 1 }));
      const th = el("th", { scope: "col" }, button);
      if (sorted) th.setAttribute("aria-sort", table.order === "asc" ? "ascending" : "descending");
      return th;
    }));
    for (const filter of this.filters) {
      const select = this.selects[filter.key];
      const values = (table.facets && table.facets[filter.key]) || [];
      select.replaceChildren(el("option", { value: "", text: filter.label }), ...values.map((value) => el("option", { value, text: value || "(blank)" })));
      select.value = this.params[filter.key] || "";
    }
    const rows = table.rows.map((row) => el("tr", null, ...this.columns.map((column) => {
      const value = column.cell ? column.cell(row) : row[column.key];
      return el("td", column.mono ? { class: "mono" } : null, value == null ? "" : value instanceof Node ? value : String(value));
    })));
    const empty = table.total ? "Nothing matches. Clear the search or filters." : (this.empty || "Nothing here yet.");
    this.body.replaceChildren(...(rows.length ? rows : [el("tr", null, el("td", { colspan: String(this.columns.length), class: "muted", text: empty }))]));
    const first = table.matching ? (table.page - 1) * table.size + 1 : 0;
    const last = Math.min(table.page * table.size, table.matching);
    this.count.textContent = "Showing " + first.toLocaleString() + "–" + last.toLocaleString() + " of " + table.matching.toLocaleString() + (table.matching !== table.total ? " matching (" + table.total.toLocaleString() + " in all)" : "");
    this.where.textContent = "Page " + table.page + " of " + table.pages;
    this.prev.disabled = table.page <= 1;
    this.next.disabled = table.page >= table.pages;
    if (this.exportLink) {
      const params = Object.assign({}, this.params, { page: "", size: "" });
      this.exportLink.href = this.exportPath() + "?" + query(params);
      this.exportLink.setAttribute("download", this.exportName());
    }
    if (this.after) this.after(data);
  }
}

