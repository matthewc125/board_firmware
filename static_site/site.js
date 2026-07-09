(() => {
  "use strict";

  const ARCHIVE_TABLES = new Set(["deleted_boards", "deleted_firmware_history"]);
  const FORBIDDEN_TOKENS = new Set([
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
    "REPLACE", "ATTACH", "DETACH", "PRAGMA", "VACUUM",
  ]);
  const BOARD_SEARCH_COLUMNS = [
    "CAST({a}.board_id AS TEXT)",
    "{a}.tool",
    "{a}.board_slot",
    "{a}.serial",
    "{a}.inventory_serial",
    "{a}.part_number",
    "{a}.product_name",
    "{a}.board_name",
    "{a}.manufacturer",
    "{a}.revision",
    "{a}.file_id",
    "{a}.ddr_fbga",
    "{a}.status",
    "{a}.role",
    "{a}.comment",
  ];
  const MAX_QUERY_ROWS = 5000;

  let database = null;
  let lastQueryResult = null;
  let indexSearchBound = false;
  let hardwareSearchBound = false;
  let dataFormBound = false;

  function basePath() {
    const base = window.SITE_BASE || "/";
    return base.endsWith("/") ? base : `${base}/`;
  }

  function siteUrl(path) {
    const clean = String(path || "").replace(/^\//, "");
    return `${basePath()}${clean}`;
  }

  function boardUrl(boardId) {
    return `${siteUrl("board.html")}?id=${encodeURIComponent(boardId)}`;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function dash(value) {
    return value == null || value === "" ? '<span class="text-muted">—</span>' : escapeHtml(value);
  }

  function resultBadge(result) {
    if (!result) {
      return '<span class="text-muted">—</span>';
    }
    const upper = String(result).toUpperCase();
    if (upper === "PASS") {
      return '<span class="badge text-bg-success">PASS</span>';
    }
    if (upper === "FAIL") {
      return '<span class="badge text-bg-danger">FAIL</span>';
    }
    return `<span class="badge text-bg-secondary">${escapeHtml(result)}</span>`;
  }

  function queryTokens(upperSql) {
    return new Set(upperSql.match(/[A-Z_][A-Z0-9_]*/g) || []);
  }

  function validateReadonlyQuery(sql) {
    const cleaned = sql.trim().replace(/;+\s*$/, "");
    const upper = cleaned.toUpperCase();
    if (!(upper.startsWith("SELECT") || upper.startsWith("WITH"))) {
      throw new Error("Only SELECT queries are allowed.");
    }
    const blocked = [...queryTokens(upper)].filter((token) => FORBIDDEN_TOKENS.has(token));
    if (blocked.length) {
      throw new Error(`Query may not contain ${blocked[0]}.`);
    }
    for (const table of ARCHIVE_TABLES) {
      if (upper.includes(table.toUpperCase())) {
        throw new Error(`Query may not access archive table ${table}.`);
      }
    }
    return cleaned;
  }

  function boardSearchClause(search, alias, extraColumns = []) {
    if (!search) {
      return { clause: "", params: [] };
    }
    const like = `%${search}%`;
    const columns = BOARD_SEARCH_COLUMNS.map((col) => col.replace(/\{a\}/g, alias)).concat(extraColumns);
    const clause = "(" + columns.map((col) => `${col} LIKE ?`).join(" OR ") + ")";
    return { clause, params: columns.map(() => like) };
  }

  function rowsFromStatement(stmt) {
    const rows = [];
    while (stmt.step()) {
      rows.push(stmt.getAsObject());
    }
    stmt.free();
    return rows;
  }

  function runSelect(sql, params = []) {
    const stmt = database.prepare(sql);
    try {
      stmt.bind(params);
      return rowsFromStatement(stmt);
    } catch (error) {
      stmt.free();
      throw error;
    }
  }

  function runReadonlyQuery(sql, maxRows = MAX_QUERY_ROWS) {
    const cleaned = validateReadonlyQuery(sql);
    const rows = runSelect(cleaned);
    const truncated = rows.length > maxRows;
    const limited = truncated ? rows.slice(0, maxRows) : rows;
    const columns = limited.length ? Object.keys(limited[0]) : [];
    return { columns, rows: limited, truncated };
  }

  function getParams() {
    return new URLSearchParams(window.location.search);
  }

  function setParams(params) {
    const url = new URL(window.location.href);
    url.search = params.toString();
    window.history.replaceState({}, "", url);
  }

  function showError(message) {
    const loading = document.getElementById("site-loading");
    const error = document.getElementById("site-error");
    const content = document.getElementById("site-content");
    if (loading) loading.classList.add("d-none");
    if (content) content.classList.add("d-none");
    if (error) {
      error.textContent = message;
      error.classList.remove("d-none");
    }
  }

  function showContent() {
    const loading = document.getElementById("site-loading");
    const error = document.getElementById("site-error");
    const content = document.getElementById("site-content");
    if (loading) loading.classList.add("d-none");
    if (error) error.classList.add("d-none");
    if (content) content.classList.remove("d-none");
  }

  function markActiveNav() {
    const page = window.SITE_PAGE;
    document.querySelectorAll("[data-nav]").forEach((link) => {
      if (link.getAttribute("data-nav") === page) {
        link.classList.add("active");
      }
    });
  }

  function sortLink(label, column, sort, order, extraParams) {
    const newOrder = sort === column && order === "asc" ? "desc" : "asc";
    const params = new URLSearchParams(extraParams);
    params.set("sort", column);
    params.set("order", newOrder);
    const arrow = sort === column ? (order === "asc" ? " ▲" : " ▼") : "";
    return `<a href="?${params.toString()}" class="text-decoration-none text-reset">${escapeHtml(label)}${arrow}</a>`;
  }

  function listBoards({ product, firmware, tool, search, sort = "board_id", order = "asc" }) {
    const allowedSort = {
      board_id: "b.board_id",
      tool: "b.tool",
      board_name: "b.board_name",
      product_name: "b.product_name",
      serial: "b.serial",
      firmware: "cf.firmware",
      event_date: "cf.event_date",
    };
    const sortCol = allowedSort[sort] || "b.board_id";
    const sortDir = order === "desc" ? "DESC" : "ASC";
    const clauses = [];
    const params = [];

    if (product) {
      clauses.push("b.product_name = ?");
      params.push(product);
    }
    if (firmware) {
      clauses.push("cf.firmware = ?");
      params.push(firmware);
    }
    if (tool) {
      if (tool === "(Unassigned)") {
        clauses.push("b.tool IS NULL");
      } else {
        clauses.push("b.tool = ?");
        params.push(tool);
      }
    }
    const searchBits = boardSearchClause(search, "b", ["cf.firmware"]);
    if (searchBits.clause) {
      clauses.push(searchBits.clause);
      params.push(...searchBits.params);
    }

    const where = clauses.length ? `WHERE ${clauses.join(" AND ")}` : "";
    return runSelect(
      `
      SELECT
        b.*,
        cf.firmware AS current_firmware,
        cf.fpga AS current_fpga,
        COALESCE(cf.event_date, b.source_updated_at) AS last_update
      FROM boards b
      LEFT JOIN current_firmware cf ON cf.board_id = b.board_id
      ${where}
      ORDER BY ${sortCol} ${sortDir}, b.board_id ASC
      `,
      params,
    );
  }

  function listHistory({ product, firmware, tool, search }) {
    const clauses = [];
    const params = [];

    if (product) {
      clauses.push("b.product_name = ?");
      params.push(product);
    }
    if (firmware) {
      clauses.push("h.firmware = ?");
      params.push(firmware);
    }
    if (tool) {
      if (tool === "(Unassigned)") {
        clauses.push("b.tool IS NULL");
      } else {
        clauses.push("b.tool = ?");
        params.push(tool);
      }
    }
    const searchBits = boardSearchClause(search, "b", ["h.firmware"]);
    if (searchBits.clause) {
      clauses.push(searchBits.clause);
      params.push(...searchBits.params);
    }

    const where = clauses.length ? `WHERE ${clauses.join(" AND ")}` : "";
    return runSelect(
      `
      SELECT
        h.event_id,
        h.event_date,
        h.event_time,
        h.firmware,
        h.fpga,
        h.installer,
        h.result,
        b.board_id,
        b.board_name,
        b.product_name,
        b.tool,
        b.serial
      FROM firmware_history h
      JOIN boards b ON b.board_id = h.board_id
      ${where}
      ORDER BY h.event_date DESC, COALESCE(h.event_time, '00:00:00') DESC, h.event_id DESC
      `,
      params,
    );
  }

  function listHardware({ search, sort = "board_id", order = "asc" }) {
    const allowedSort = {
      board_id: "board_id",
      tool: "tool",
      board_slot: "board_slot",
      board_name: "board_name",
      product_name: "product_name",
      serial: "serial",
      inventory_serial: "inventory_serial",
      status: "status",
      part_number: "part_number",
      revision: "revision",
      file_id: "file_id",
      ddr_fbga: "ddr_fbga",
      manufacturer: "manufacturer",
    };
    const sortCol = allowedSort[sort] || "board_id";
    const sortDir = order === "desc" ? "DESC" : "ASC";
    const clauses = [];
    const params = [];
    const searchBits = boardSearchClause(search, "boards");
    if (searchBits.clause) {
      clauses.push(searchBits.clause);
      params.push(...searchBits.params);
    }
    const where = clauses.length ? `WHERE ${clauses.join(" AND ")}` : "";
    return runSelect(
      `
      SELECT *
      FROM boards
      ${where}
      ORDER BY ${sortCol} ${sortDir}, board_id ASC
      `,
      params,
    );
  }

  function dashboardStats() {
    return runSelect(
      `
      SELECT
        (SELECT COUNT(*) FROM boards) AS board_count,
        (SELECT COUNT(*) FROM firmware_history) AS history_count,
        (SELECT COUNT(DISTINCT firmware) FROM firmware_history) AS firmware_versions
      `,
    )[0];
  }

  function boardProducts() {
    return runSelect(
      `
      SELECT product_name, COUNT(*) AS board_count
      FROM boards
      GROUP BY product_name
      ORDER BY product_name
      `,
    );
  }

  function firmwareStats() {
    return runSelect(
      `
      SELECT firmware, COUNT(*) AS board_count
      FROM current_firmware
      GROUP BY firmware
      ORDER BY board_count DESC, firmware DESC
      LIMIT 10
      `,
    );
  }

  function firmwareForProduct(productName) {
    return new Set(
      runSelect(
        `
        SELECT DISTINCT cf.firmware
        FROM current_firmware cf
        JOIN boards b ON b.board_id = cf.board_id
        WHERE b.product_name = ? AND cf.firmware IS NOT NULL
        `,
        [productName],
      ).map((row) => row.firmware),
    );
  }

  function productsForFirmware(firmware) {
    return new Set(
      runSelect(
        `
        SELECT DISTINCT b.product_name
        FROM current_firmware cf
        JOIN boards b ON b.board_id = cf.board_id
        WHERE cf.firmware = ?
        `,
        [firmware],
      ).map((row) => row.product_name),
    );
  }

  function toolStats() {
    return runSelect(
      `
      SELECT COALESCE(tool, '(Unassigned)') AS tool, COUNT(*) AS board_count
      FROM boards
      GROUP BY COALESCE(tool, '(Unassigned)')
      ORDER BY CASE WHEN tool = '(Unassigned)' THEN 1 ELSE 0 END, tool ASC
      `,
    );
  }

  function productsForTool(tool) {
    if (tool === "(Unassigned)") {
      return new Set(runSelect("SELECT DISTINCT product_name FROM boards WHERE tool IS NULL").map((row) => row.product_name));
    }
    return new Set(runSelect("SELECT DISTINCT product_name FROM boards WHERE tool = ?", [tool]).map((row) => row.product_name));
  }

  function firmwareForTool(tool) {
    if (tool === "(Unassigned)") {
      return new Set(
        runSelect(
          `
          SELECT DISTINCT cf.firmware
          FROM current_firmware cf
          JOIN boards b ON b.board_id = cf.board_id
          WHERE b.tool IS NULL AND cf.firmware IS NOT NULL
          `,
        ).map((row) => row.firmware),
      );
    }
    return new Set(
      runSelect(
        `
        SELECT DISTINCT cf.firmware
        FROM current_firmware cf
        JOIN boards b ON b.board_id = cf.board_id
        WHERE b.tool = ? AND cf.firmware IS NOT NULL
        `,
        [tool],
      ).map((row) => row.firmware),
    );
  }

  function toolsForProduct(productName) {
    return new Set(
      runSelect(
        "SELECT DISTINCT COALESCE(tool, '(Unassigned)') AS tool FROM boards WHERE product_name = ?",
        [productName],
      ).map((row) => row.tool),
    );
  }

  function toolsForFirmware(firmware) {
    return new Set(
      runSelect(
        `
        SELECT DISTINCT COALESCE(b.tool, '(Unassigned)') AS tool
        FROM current_firmware cf
        JOIN boards b ON b.board_id = cf.board_id
        WHERE cf.firmware = ?
        `,
        [firmware],
      ).map((row) => row.tool),
    );
  }

  function renderIndex() {
    const params = getParams();
    const product = params.get("product") || "";
    const firmware = params.get("firmware") || "";
    const tool = params.get("tool") || "";
    const search = params.get("q") || "";
    const sort = params.get("sort") || "board_id";
    const order = params.get("order") || "asc";

    const searchInput = document.getElementById("board-search");
    if (searchInput) searchInput.value = search;

    const filterSummary = document.getElementById("index-filter-summary");
    if (filterSummary && (product || firmware || tool)) {
      const bits = [];
      if (product) {
        const remove = new URLSearchParams(params);
        remove.delete("product");
        bits.push(
          `<a href="?${remove.toString()}" class="badge text-bg-primary text-decoration-none" title="Click to remove">${escapeHtml(product)} ×</a>`,
        );
      }
      if (tool) {
        const remove = new URLSearchParams(params);
        remove.delete("tool");
        bits.push(
          `<a href="?${remove.toString()}" class="badge text-bg-primary text-decoration-none" title="Click to remove">${escapeHtml(tool)} ×</a>`,
        );
      }
      if (firmware) {
        const remove = new URLSearchParams(params);
        remove.delete("firmware");
        bits.push(
          `<a href="?${remove.toString()}" class="badge text-bg-primary text-decoration-none" title="Click to remove"><code class="text-white">${escapeHtml(firmware)}</code> ×</a>`,
        );
      }
      const clear = new URLSearchParams(params);
      clear.delete("product");
      clear.delete("firmware");
      clear.delete("tool");
      filterSummary.innerHTML = `Filtered: ${bits.join(" ")} <a href="?${clear.toString()}" class="small ms-1">Clear filters</a>`;
      filterSummary.classList.remove("d-none");
    } else if (filterSummary) {
      filterSummary.classList.add("d-none");
    }

    const boards = listBoards({ product, firmware, tool, search, sort, order });
    const history = listHistory({ product, firmware, tool, search });
    const stats = dashboardStats();
    const products = boardProducts();
    const fwStats = firmwareStats();
    const toolList = toolStats();
    const availableFirmware = product ? firmwareForProduct(product) : new Set();
    const availableProducts = firmware ? productsForFirmware(firmware) : new Set();
    const availableTools = product ? toolsForProduct(product) : (firmware ? toolsForFirmware(firmware) : new Set());
    const availableProductsForTool = tool ? productsForTool(tool) : new Set();
    const availableFirmwareForTool = tool ? firmwareForTool(tool) : new Set();

    const linkParams = new URLSearchParams();
    if (product) linkParams.set("product", product);
    if (firmware) linkParams.set("firmware", firmware);
    if (tool) linkParams.set("tool", tool);
    if (search) linkParams.set("q", search);

    const head = document.getElementById("boards-head");
    if (head) {
      head.innerHTML = [
        sortLink("ID", "board_id", sort, order, linkParams),
        sortLink("Product", "product_name", sort, order, linkParams),
        sortLink("Type", "board_name", sort, order, linkParams),
        sortLink("Location", "tool", sort, order, linkParams),
        sortLink("Serial", "serial", sort, order, linkParams),
        sortLink("Firmware", "firmware", sort, order, linkParams),
        "FPGA",
        sortLink("Updated", "event_date", sort, order, linkParams),
      ].map((cell) => `<th>${cell}</th>`).join("");
    }

    const boardsBody = document.getElementById("boards-body");
    if (boardsBody) {
      boardsBody.innerHTML = boards.length
        ? boards.map((board) => `
          <tr>
            <td><a href="${boardUrl(board.board_id)}">${escapeHtml(board.board_id)}</a></td>
            <td>${escapeHtml(board.product_name)}</td>
            <td>${escapeHtml(board.board_name)}</td>
            <td>${dash(board.tool)}</td>
            <td>${escapeHtml(board.serial)}</td>
            <td>${board.current_firmware ? `<code>${escapeHtml(board.current_firmware)}</code>` : '<span class="text-muted">—</span>'}</td>
            <td>${dash(board.current_fpga)}</td>
            <td>${dash(board.last_update)}</td>
          </tr>
        `).join("")
        : '<tr><td colspan="8" class="text-muted">No boards match your filters.</td></tr>';
    }

    const historyBody = document.getElementById("history-body");
    if (historyBody) {
      historyBody.innerHTML = history.length
        ? history.map((row) => `
          <tr>
            <td>${escapeHtml(row.event_date)}</td>
            <td><a href="${boardUrl(row.board_id)}">${escapeHtml(row.product_name)} ${escapeHtml(row.board_name)}</a></td>
            <td>${dash(row.tool)}</td>
            <td><code>${escapeHtml(row.firmware)}</code></td>
            <td>${dash(row.fpga)}</td>
            <td>${dash(row.installer)}</td>
            <td>${resultBadge(row.result)}</td>
          </tr>
        `).join("")
        : '<tr><td colspan="7" class="text-muted">No firmware history matches your filters.</td></tr>';
    }

    const boardsCount = document.getElementById("boards-count");
    if (boardsCount) {
      boardsCount.textContent = `${boards.length} board(s)${boards.length > 12 ? " · scroll for more" : ""}`;
    }

    const historyCount = document.getElementById("history-count");
    if (historyCount) {
      historyCount.textContent = `${history.length} event(s)${history.length > 10 ? " · scroll for more" : ""}`;
    }

    const statsEl = document.getElementById("dashboard-stats");
    if (statsEl) {
      statsEl.innerHTML = `
        <div class="d-flex justify-content-between"><span>Boards</span><strong>${stats.board_count}</strong></div>
        <div class="d-flex justify-content-between"><span>Firmware events</span><strong>${stats.history_count}</strong></div>
        <div class="d-flex justify-content-between"><span>Unique versions</span><strong>${stats.firmware_versions}</strong></div>
      `;
    }

    const productFilters = document.getElementById("product-filters");
    if (productFilters) {
      productFilters.innerHTML = products.map((item) => {
        const paramsForLink = new URLSearchParams(getParams());
        paramsForLink.set("product", item.product_name);
        const disabled = (firmware && !availableProducts.has(item.product_name) && product !== item.product_name)
          || (tool && !availableProductsForTool.has(item.product_name) && product !== item.product_name);
        if (disabled) {
          return `<span class="list-group-item d-flex justify-content-between py-2 filter-disabled" aria-disabled="true">
            <span>${escapeHtml(item.product_name)}</span>
            <span class="badge text-bg-secondary rounded-pill">${item.board_count}</span>
          </span>`;
        }
        if (product === item.product_name) {
          paramsForLink.delete("product");
          return `<a class="list-group-item list-group-item-action d-flex justify-content-between py-2 active" href="?${paramsForLink.toString()}">
            <span>${escapeHtml(item.product_name)}</span>
            <span class="badge text-bg-light rounded-pill">${item.board_count}</span>
          </a>`;
        }
        return `<a class="list-group-item list-group-item-action d-flex justify-content-between py-2" href="?${paramsForLink.toString()}">
          <span>${escapeHtml(item.product_name)}</span>
          <span class="badge text-bg-secondary rounded-pill">${item.board_count}</span>
        </a>`;
      }).join("");
    }

    const toolFilters = document.getElementById("tool-filters");
    if (toolFilters) {
      toolFilters.innerHTML = toolList.map((item) => {
        const paramsForLink = new URLSearchParams(getParams());
        paramsForLink.set("tool", item.tool);
        const disabled = (product && !availableTools.has(item.tool) && tool !== item.tool)
          || (firmware && !availableTools.has(item.tool) && tool !== item.tool);
        if (disabled) {
          return `<span class="list-group-item d-flex justify-content-between py-2 filter-disabled" aria-disabled="true">
            <span>${escapeHtml(item.tool)}</span>
            <span class="badge text-bg-secondary rounded-pill">${item.board_count}</span>
          </span>`;
        }
        if (tool === item.tool) {
          paramsForLink.delete("tool");
          return `<a class="list-group-item list-group-item-action d-flex justify-content-between py-2 active" href="?${paramsForLink.toString()}">
            <span>${escapeHtml(item.tool)}</span>
            <span class="badge text-bg-light rounded-pill">${item.board_count}</span>
          </a>`;
        }
        return `<a class="list-group-item list-group-item-action d-flex justify-content-between py-2" href="?${paramsForLink.toString()}">
          <span>${escapeHtml(item.tool)}</span>
          <span class="badge text-bg-secondary rounded-pill">${item.board_count}</span>
        </a>`;
      }).join("");
    }

    const firmwareFilters = document.getElementById("firmware-filters");
    if (firmwareFilters) {
      firmwareFilters.innerHTML = fwStats.length
        ? fwStats.map((row) => {
          const paramsForLink = new URLSearchParams(getParams());
          paramsForLink.set("firmware", row.firmware);
          const disabled = (product && !availableFirmware.has(row.firmware) && firmware !== row.firmware)
            || (tool && !availableFirmwareForTool.has(row.firmware) && firmware !== row.firmware);
          if (disabled) {
            return `<span class="list-group-item d-flex justify-content-between py-2 filter-disabled" aria-disabled="true">
              <span><code class="small">${escapeHtml(row.firmware)}</code></span>
              <span class="badge text-bg-secondary rounded-pill">${row.board_count}</span>
            </span>`;
          }
          if (firmware === row.firmware) {
            paramsForLink.delete("firmware");
            return `<a class="list-group-item list-group-item-action d-flex justify-content-between py-2 active" href="?${paramsForLink.toString()}">
              <span><code class="small">${escapeHtml(row.firmware)}</code></span>
              <span class="badge text-bg-light rounded-pill">${row.board_count}</span>
            </a>`;
          }
          return `<a class="list-group-item list-group-item-action d-flex justify-content-between py-2" href="?${paramsForLink.toString()}">
            <span><code class="small">${escapeHtml(row.firmware)}</code></span>
            <span class="badge text-bg-secondary rounded-pill">${row.board_count}</span>
          </a>`;
        }).join("")
        : '<div class="list-group-item text-muted py-2">No data.</div>';
    }

    const searchForm = document.getElementById("search-form");
    if (searchForm && !indexSearchBound) {
      indexSearchBound = true;
      searchForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const next = new URLSearchParams(getParams());
        const value = searchInput ? searchInput.value.trim() : "";
        if (value) next.set("q", value);
        else next.delete("q");
        setParams(next);
        renderIndex();
      });

      if (searchInput) {
        const submitIfCleared = () => {
          if (searchInput.value === "") {
            searchForm.requestSubmit();
          }
        };
        searchInput.addEventListener("search", submitIfCleared);
        searchInput.addEventListener("input", submitIfCleared);
      }
    }
  }

  function renderHardware() {
    const params = getParams();
    const search = params.get("q") || "";
    const sort = params.get("sort") || "board_id";
    const order = params.get("order") || "asc";
    const boards = listHardware({ search, sort, order });

    const searchInput = document.getElementById("board-search");
    if (searchInput) searchInput.value = search;

    const linkParams = new URLSearchParams();
    if (search) linkParams.set("q", search);

    const columns = [
      ["ID", "board_id"],
      ["Product", "product_name"],
      ["Type", "board_name"],
      ["Location", "tool"],
      ["Serial", "serial"],
      ["Inventory SN", "inventory_serial"],
      ["Status", "status"],
      ["Part Number", "part_number"],
      ["Revision", "revision"],
      ["File ID", "file_id"],
      ["DDR FBGA", "ddr_fbga"],
      ["Manufacturer", "manufacturer"],
    ];

    const head = document.getElementById("hardware-head");
    if (head) {
      head.innerHTML = columns
        .map(([label, column]) => `<th>${sortLink(label, column, sort, order, linkParams)}</th>`)
        .join("");
    }

    const body = document.getElementById("hardware-body");
    if (body) {
      body.innerHTML = boards.length
        ? boards.map((board) => `
          <tr>
            <td><a href="${boardUrl(board.board_id)}">${escapeHtml(board.board_id)}</a></td>
            <td>${escapeHtml(board.product_name)}</td>
            <td>${escapeHtml(board.board_name)}</td>
            <td>${dash(board.tool)}</td>
            <td>${escapeHtml(board.serial)}</td>
            <td>${dash(board.inventory_serial)}</td>
            <td>${dash(board.status)}</td>
            <td>${dash(board.part_number)}</td>
            <td>${dash(board.revision)}</td>
            <td>${dash(board.file_id)}</td>
            <td>${dash(board.ddr_fbga)}</td>
            <td>${dash(board.manufacturer)}</td>
          </tr>
        `).join("")
        : '<tr><td colspan="12" class="text-muted">No boards match your search.</td></tr>';
    }

    const count = document.getElementById("hardware-count");
    if (count) count.textContent = `${boards.length} board(s)`;

    const searchForm = document.getElementById("search-form");
    if (searchForm && !hardwareSearchBound) {
      hardwareSearchBound = true;
      searchForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const next = new URLSearchParams(getParams());
        const value = searchInput ? searchInput.value.trim() : "";
        if (value) next.set("q", value);
        else next.delete("q");
        setParams(next);
        renderHardware();
      });

      if (searchInput) {
        const submitIfCleared = () => {
          if (searchInput.value === "") {
            searchForm.requestSubmit();
          }
        };
        searchInput.addEventListener("search", submitIfCleared);
        searchInput.addEventListener("input", submitIfCleared);
      }
    }
  }

  function renderBoard() {
    const params = getParams();
    const boardId = Number.parseInt(params.get("id") || "", 10);
    if (!Number.isFinite(boardId)) {
      showError("Board not found.");
      return;
    }

    const board = runSelect("SELECT * FROM boards WHERE board_id = ?", [boardId])[0];
    if (!board) {
      showError("Board not found.");
      return;
    }

    const history = runSelect(
      `
      SELECT *
      FROM firmware_history
      WHERE board_id = ?
      ORDER BY event_date DESC, COALESCE(event_time, '00:00:00') DESC, event_id DESC
      `,
      [boardId],
    );

    const events = runSelect(
      `
      SELECT *
      FROM board_events
      WHERE board_id = ?
      ORDER BY event_date DESC, COALESCE(event_time, '00:00:00') DESC, event_id DESC
      `,
      [boardId],
    );

    const title = document.getElementById("board-title");
    if (title) title.textContent = `${board.product_name} ${board.board_name}`;
    const subtitle = document.getElementById("board-subtitle");
    if (subtitle) subtitle.textContent = `Board ID ${board.board_id}`;

    const info = document.getElementById("board-info");
    if (info) {
      const fields = [
        ["Product", board.product_name],
        ["Board type", board.board_name],
        ["Location", board.tool],
        ["Serial", board.serial],
        ["Inventory serial", board.inventory_serial],
        ["Status", board.status],
        ["Role", board.role],
        ["Part number", board.part_number],
        ["Revision", board.revision],
        ["File ID", board.file_id],
        ["DDR FBGA", board.ddr_fbga],
        ["Manufacturer", board.manufacturer],
      ];
      if (board.dc_status || board.ac_status || board.gcal_status || board.adc_status || board.eeprom_status) {
        const testParts = [];
        if (board.dc_status) testParts.push(`DC: ${board.dc_status}`);
        if (board.ac_status) testParts.push(`AC: ${board.ac_status}`);
        if (board.gcal_status) testParts.push(`GCAL: ${board.gcal_status}`);
        if (board.adc_status) testParts.push(`ADC: ${board.adc_status}`);
        if (board.eeprom_status) testParts.push(`EEPROM: ${board.eeprom_status}`);
        fields.push(["Test status", testParts.join(" · ")]);
      }
      if (board.comment) fields.push(["Comment", board.comment]);
      if (board.data_source) fields.push(["Data source", board.data_source]);
      info.innerHTML = fields.map(([label, value]) => `
        <dt class="col-sm-4">${escapeHtml(label)}</dt>
        <dd class="col-sm-8">${dash(value)}</dd>
      `).join("");
    }

    const eventsSection = document.getElementById("board-events-section");
    const eventsBody = document.getElementById("board-events-body");
    if (eventsSection && eventsBody) {
      if (events.length) {
        eventsSection.classList.remove("d-none");
        eventsBody.innerHTML = events.map((row) => `
          <tr>
            <td>${escapeHtml(row.event_date)}${row.event_time ? ` ${escapeHtml(row.event_time)}` : ""}</td>
            <td>${escapeHtml(row.event_type)}</td>
            <td>${escapeHtml(row.description)}</td>
            <td>${dash(row.tool)}</td>
          </tr>
        `).join("");
      } else {
        eventsSection.classList.add("d-none");
      }
    }

    const historyBody = document.getElementById("board-history-body");
    if (historyBody) {
      historyBody.innerHTML = history.length
        ? history.map((row) => `
          <tr>
            <td>${escapeHtml(row.event_date)}${row.event_time ? ` ${escapeHtml(row.event_time)}` : ""}</td>
            <td>${dash(row.fpga)}</td>
            <td><code>${escapeHtml(row.firmware)}</code></td>
            <td>${dash(row.installer)}</td>
            <td>${resultBadge(row.result)}</td>
          </tr>
        `).join("")
        : '<tr><td colspan="5" class="text-muted">No firmware history for this board.</td></tr>';
    }
  }

  function renderDataQueryResult(result) {
    const error = document.getElementById("data-query-error");
    const truncated = document.getElementById("data-query-truncated");
    const count = document.getElementById("data-query-count");
    const table = document.getElementById("data-query-table");
    const head = document.getElementById("data-query-head");
    const body = document.getElementById("data-query-body");
    const download = document.getElementById("data-query-download");

    if (error) error.classList.add("d-none");
    if (truncated) truncated.classList.toggle("d-none", !result.truncated);
    if (count) {
      count.textContent = `${result.rows.length} row(s)`;
      count.classList.remove("d-none");
    }
    if (table) table.classList.remove("d-none");
    if (head) {
      head.innerHTML = result.columns.map((col) => `<th>${escapeHtml(col)}</th>`).join("");
    }
    if (body) {
      body.innerHTML = result.rows.map((row) => `
        <tr>
          ${result.columns.map((col) => `<td>${escapeHtml(row[col] ?? "")}</td>`).join("")}
        </tr>
      `).join("");
    }
    if (download) download.classList.toggle("d-none", !result.columns.length);
    lastQueryResult = result;
  }

  function renderData() {
    const form = document.getElementById("data-query-form");
    const textarea = document.getElementById("data-query");
    const error = document.getElementById("data-query-error");
    const download = document.getElementById("data-query-download");
    const defaultQuery = "SELECT * FROM current_firmware ORDER BY board_id";

    if (textarea && !textarea.value.trim()) {
      textarea.value = defaultQuery;
    }

    if (form && !dataFormBound) {
      dataFormBound = true;
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        try {
          const result = runReadonlyQuery(textarea.value);
          renderDataQueryResult(result);
        } catch (err) {
          if (error) {
            error.textContent = err.message || String(err);
            error.classList.remove("d-none");
          }
          document.getElementById("data-query-table")?.classList.add("d-none");
          document.getElementById("data-query-count")?.classList.add("d-none");
          document.getElementById("data-query-truncated")?.classList.add("d-none");
          download?.classList.add("d-none");
        }
      });
    }

    if (download) {
      download.addEventListener("click", () => {
        if (!lastQueryResult || !lastQueryResult.columns.length) return;
        const lines = [lastQueryResult.columns.join(",")];
        for (const row of lastQueryResult.rows) {
          lines.push(
            lastQueryResult.columns
              .map((col) => {
                const value = row[col] ?? "";
                const text = String(value);
                return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
              })
              .join(","),
          );
        }
        const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = "query_results.csv";
        anchor.click();
        URL.revokeObjectURL(url);
      });
    }
  }

  async function initDatabase() {
    const cacheKey = encodeURIComponent(window.SITE_BUILT_AT || String(Date.now()));
    const response = await fetch(`${siteUrl("data/board_firmware.db")}?v=${cacheKey}`);
    if (!response.ok) {
      throw new Error(`Could not load database (${response.status}).`);
    }
    const buffer = await response.arrayBuffer();
    const SQL = await initSqlJs({
      locateFile: (file) => `https://cdn.jsdelivr.net/npm/sql.js@1.10.3/dist/${file}`,
    });
    database = new SQL.Database(new Uint8Array(buffer));
  }

  function renderPage() {
    switch (window.SITE_PAGE) {
      case "index":
        renderIndex();
        break;
      case "hardware":
        renderHardware();
        break;
      case "board":
        renderBoard();
        break;
      case "data":
        renderData();
        break;
      default:
        break;
    }
  }

  async function boot() {
    markActiveNav();
    try {
      await initDatabase();
      showContent();
      renderPage();
    } catch (error) {
      showError(error.message || String(error));
    }
  }

  boot();
})();
