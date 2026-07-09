(function (global) {
  "use strict";

  const STATEMENT_KEYWORDS = ["SELECT", "WITH", "UPDATE", "INSERT", "DELETE"];
  const CLAUSE_KEYWORDS = [
    "FROM", "WHERE", "JOIN", "INNER", "LEFT", "RIGHT", "OUTER", "CROSS",
    "ON", "AND", "OR", "SET", "VALUES", "INTO", "ORDER", "BY", "GROUP",
    "HAVING", "LIMIT", "AS", "DISTINCT", "UNION", "ALL",
  ];

  function inStringLiteral(text) {
    let single = false;
    let double = false;
    for (let i = 0; i < text.length; i += 1) {
      const ch = text[i];
      if (ch === "'" && !double) {
        single = !single;
      } else if (ch === '"' && !single) {
        double = !double;
      }
    }
    return single || double;
  }

  function lastStatement(text) {
    const parts = text.split(";");
    return parts[parts.length - 1] || "";
  }

  function parseTableRefs(statement, schema) {
    const refs = {};
    const tableNames = Object.keys(schema);
    const addRef = (table, alias) => {
      const resolved = tableNames.includes(table) ? table : null;
      if (!resolved) {
        return;
      }
      refs[table] = resolved;
      if (alias && alias.toUpperCase() !== resolved.toUpperCase()) {
        refs[alias] = resolved;
      }
    };

    const fromJoin = /\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?/gi;
    let match;
    while ((match = fromJoin.exec(statement)) !== null) {
      const table = match[1];
      const alias = match[2];
      if (alias && CLAUSE_KEYWORDS.includes(alias.toUpperCase())) {
        addRef(table, null);
      } else {
        addRef(table, alias);
      }
    }
    return refs;
  }

  function columnsForRefs(refs, schema) {
    const cols = new Set();
    Object.values(refs).forEach((table) => {
      (schema[table] || []).forEach((col) => cols.add(col));
    });
    return Array.from(cols);
  }

  function resolveTableRef(ref, refs, schema) {
    if (refs[ref]) {
      return refs[ref];
    }
    if (schema[ref]) {
      return ref;
    }
    return null;
  }

  function trailingPartial(text) {
    const match = text.match(/([A-Za-z_][A-Za-z0-9_]*)$/);
    return match ? match[1] : "";
  }

  function trailingQualified(text) {
    const match = text.match(/([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$/);
    if (!match) {
      return null;
    }
    return { tableRef: match[1], partial: match[2] };
  }

  function endsWithKeyword(text, keyword) {
    return new RegExp("\\b" + keyword + "\\s*$", "i").test(text);
  }

  const PREDICATE_KEYWORDS = ["IS", "NOT", "NULL", "LIKE", "IN", "BETWEEN"];
  const ALL_KEYWORDS = new Set([
    ...STATEMENT_KEYWORDS,
    ...CLAUSE_KEYWORDS,
    ...PREDICATE_KEYWORDS,
    "*",
    "=",
  ]);

  function buildSchemaIndex(schema) {
    const index = new Map();
    Object.keys(schema).forEach((table) => {
      index.set(table.toLowerCase(), table);
      (schema[table] || []).forEach((col) => {
        index.set(col.toLowerCase(), col);
      });
    });
    return index;
  }

  function isSqlKeyword(text) {
    return ALL_KEYWORDS.has(String(text).trim().toUpperCase());
  }

  function formatCompletion(text, schemaIndex) {
    const leading = (text.match(/^\s*/) || [""])[0];
    const core = text.trim();
    if (!core) {
      return text;
    }
    if (isSqlKeyword(core)) {
      return leading + core.toUpperCase();
    }
    const canonical = schemaIndex.get(core.toLowerCase());
    return leading + (canonical || core);
  }

  function keywordPrefixMatches(partial, keywords) {
    if (!partial) {
      return keywords.slice();
    }
    const lower = partial.toLowerCase();
    return keywords.filter(
      (kw) => kw.toLowerCase().startsWith(lower) && kw.length > partial.length
    );
  }

  const POST_TABLE_KEYWORDS = [
    "WHERE", "JOIN", "INNER", "LEFT", "RIGHT", "OUTER", "CROSS",
    "ORDER", "GROUP", "HAVING", "LIMIT", "UNION",
  ];

  function tailAfterFrom(statement) {
    const match = statement.match(/\bFROM\b([\s\S]*)$/i);
    return match ? match[1].trimStart() : "";
  }

  function afterFromClauseKeywordContext(statement) {
    if (!/\bSELECT\b/i.test(statement) || !/\bFROM\b/i.test(statement)) {
      return null;
    }

    const tail = tailAfterFrom(statement);
    if (!tail) {
      return null;
    }

    if (/\b(WHERE|JOIN|ORDER|GROUP|HAVING|LIMIT|UNION)\b/i.test(tail)) {
      return null;
    }

    if (/\bFROM\s+[A-Za-z_][A-Za-z0-9_]*(?:\s+(?:AS\s+)?[A-Za-z_][A-Za-z0-9_]*)?\s+$/i.test(statement)) {
      return { partial: "" };
    }

    const words = tail.split(/\s+/).filter(Boolean);
    const trailing = trailingPartial(statement);
    if (words.length < 2 || !trailing) {
      return null;
    }

    const lastWord = words[words.length - 1];
    if (lastWord.toLowerCase() !== trailing.toLowerCase()) {
      return null;
    }

    const beforeTrailing = words.slice(0, -1);
    if (beforeTrailing.length === 0 || beforeTrailing.length > 2) {
      return null;
    }

    if (beforeTrailing.length === 2 && isSqlKeyword(beforeTrailing[1])) {
      return null;
    }

    return { partial: trailing };
  }

  function columnPrefixMatches(partial, columns) {
    if (!partial) {
      return columns.slice();
    }
    const lower = partial.toLowerCase().replace(/\s+/g, "");
    return columns.filter((col) => {
      const colLower = col.toLowerCase();
      if (colLower.startsWith(lower) && colLower.length > lower.length) {
        return true;
      }
      const compact = colLower.replace(/_/g, "");
      if (compact.startsWith(lower) && compact.length > lower.length) {
        return true;
      }
      return colLower.split("_").some(
        (segment) => segment.startsWith(lower) && segment.length > lower.length
      );
    });
  }

  function isKnownColumn(name, columns) {
    const lower = String(name).toLowerCase();
    return columns.some((col) => col.toLowerCase() === lower);
  }

  function tailAfterPredicate(statement) {
    const match = statement.match(/\b(?:WHERE|AND|OR|HAVING|ON)\b([\s\S]*)$/i);
    return match ? match[1].trimStart() : null;
  }

  function whereClauseCompletions(statement, refs, schema) {
    if (!/\b(?:WHERE|AND|OR|HAVING|ON)\b/i.test(statement)) {
      return null;
    }

    const tail = tailAfterPredicate(statement);
    if (tail === null) {
      return null;
    }

    const partial = trailingPartial(statement);
    const cols = columnsForRefs(refs, schema);
    const afterColumnOps = ["IS", "LIKE", "IN", "NOT", "="];

    if (/\b(?:WHERE|AND|OR|HAVING|ON)\s+$/i.test(statement)) {
      return cols;
    }

    if (/\bIS\s+NOT\s*$/i.test(statement) && !partial) {
      return ["NULL"];
    }

    if (/\bIS\s+NOT\s+[A-Za-z_][A-Za-z0-9_]*$/i.test(statement)) {
      return keywordPrefixMatches(partial, ["NULL"]);
    }

    if (/\bIS\s+$/i.test(statement)) {
      return ["NOT", "NULL"];
    }

    if (/\bIS\s+[A-Za-z_][A-Za-z0-9_]*$/i.test(statement) && !/\bIS\s+NOT\b/i.test(statement)) {
      return keywordPrefixMatches(partial, ["NOT", "NULL"]);
    }

    const colThenWord = statement.match(/\b([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z_][A-Za-z0-9_]*)$/i);
    if (colThenWord && isKnownColumn(colThenWord[1], cols)) {
      return keywordPrefixMatches(partial, afterColumnOps);
    }

    if (/\b([A-Za-z_][A-Za-z0-9_]*)\s+$/i.test(statement)) {
      const colMatch = statement.match(/\b([A-Za-z_][A-Za-z0-9_]*)\s+$/i);
      if (colMatch && isKnownColumn(colMatch[1], cols)) {
        return afterColumnOps;
      }
    }

    if (/\bNULL\s*$/i.test(statement)) {
      return ["AND", "OR"];
    }

    if (/\b(?:WHERE|AND|OR|HAVING|ON)\s+[A-Za-z_][A-Za-z0-9_]*$/i.test(statement)) {
      const colMatches = columnPrefixMatches(partial, cols);
      if (colMatches.length) {
        return colMatches;
      }
      return keywordPrefixMatches(partial, PREDICATE_KEYWORDS);
    }

    return null;
  }

  function selectListHasStar(statement) {
    const list = statement.replace(/^\s*SELECT\s+(?:DISTINCT\s+)?/i, "");
    return /^\*/.test(list.trimStart()) || /,\s*\*/.test(list);
  }

  function getValidCompletions(textBeforeCursor, schema) {
    if (inStringLiteral(textBeforeCursor)) {
      return [];
    }

    const statement = lastStatement(textBeforeCursor).trimStart();
    const upper = statement.toUpperCase();
    const refs = parseTableRefs(statement, schema);
    const tables = Object.keys(schema);
    const qualified = trailingQualified(statement);
    const partial = trailingPartial(statement);

    if (qualified) {
      const table = resolveTableRef(qualified.tableRef, refs, schema);
      if (!table) {
        return [];
      }
      return keywordPrefixMatches(qualified.partial, schema[table] || []);
    }

    if (/^[A-Za-z_][A-Za-z0-9_]*$/i.test(statement)) {
      return keywordPrefixMatches(partial, STATEMENT_KEYWORDS);
    }

    if (/^\s*INSERT\s+[A-Za-z_][A-Za-z0-9_]*$/i.test(statement)) {
      return keywordPrefixMatches(partial, ["INTO"]);
    }

    if (/^\s*DELETE\s+[A-Za-z_][A-Za-z0-9_]*$/i.test(statement)) {
      return keywordPrefixMatches(partial, ["FROM"]);
    }

    if (/^\s*ORDER\s+[A-Za-z_][A-Za-z0-9_]*$/i.test(statement)) {
      return keywordPrefixMatches(partial, ["BY"]);
    }

    if (/^\s*GROUP\s+[A-Za-z_][A-Za-z0-9_]*$/i.test(statement)) {
      return keywordPrefixMatches(partial, ["BY"]);
    }

    if (/\bUPDATE\s+[A-Za-z_][A-Za-z0-9_]*\s+SET\s+[A-Za-z_][A-Za-z0-9_]*$/i.test(statement)) {
      const updateMatch = statement.match(/\bUPDATE\s+([A-Za-z_][A-Za-z0-9_]*)\s+SET/i);
      const table = updateMatch ? resolveTableRef(updateMatch[1], refs, schema) || updateMatch[1] : null;
      if (!table || !schema[table]) {
        return [];
      }
      return keywordPrefixMatches(partial, schema[table]);
    }

    if (endsWithKeyword(statement, "SET")) {
      return keywordPrefixMatches(partial, ["="]);
    }

    if (endsWithKeywordAndPartial(statement, "UPDATE")) {
      return keywordPrefixMatches(partial, tables);
    }

    if (endsWithKeywordAndPartial(statement, "INTO")) {
      return keywordPrefixMatches(partial, tables);
    }

    if (/\b(?:FROM|JOIN)\s+[A-Za-z_][A-Za-z0-9_]*$/i.test(statement)) {
      return keywordPrefixMatches(partial, tables);
    }

    const afterFromClause = afterFromClauseKeywordContext(statement);
    if (afterFromClause) {
      return keywordPrefixMatches(afterFromClause.partial, POST_TABLE_KEYWORDS);
    }

    if (endsWithKeyword(statement, "FROM") || endsWithKeyword(statement, "JOIN")) {
      return tables;
    }

    if (endsWithKeyword(statement, "INTO")) {
      return tables;
    }

    const whereClause = whereClauseCompletions(statement, refs, schema);
    if (whereClause) {
      return whereClause;
    }

    if (/^\s*SELECT\s*$/i.test(statement)) {
      return ["*", "DISTINCT"];
    }

    if (/^\s*SELECT\s+DISTINCT\s*$/i.test(statement)) {
      return ["*"];
    }

    if (/^\s*SELECT\s+/i.test(upper) && !/\bFROM\b/i.test(upper)) {
      if (selectListHasStar(statement)) {
        if (/\*\s*$/i.test(statement) && !partial) {
          return [" FROM"];
        }
        if (/\*\s+[A-Za-z_][A-Za-z0-9_]*$/i.test(statement)) {
          return keywordPrefixMatches(partial, ["FROM"]);
        }
        return [];
      }

      if (/^\s*SELECT\s+[A-Za-z_][A-Za-z0-9_]*$/i.test(statement) && partial) {
        const cols = columnsForRefs(refs, schema);
        if (cols.length) {
          return keywordPrefixMatches(partial, cols);
        }
        return [];
      }

      if (/^\s*SELECT\s+$/i.test(statement) || /^\s*SELECT\s+DISTINCT\s+$/i.test(statement)) {
        return ["*"];
      }

      return [];
    }

    if (endsWithKeyword(statement, "ON") ||
        endsWithKeyword(statement, "AND") || endsWithKeyword(statement, "OR") ||
        /\bORDER\s+BY\s*$/i.test(statement) || /\bGROUP\s+BY\s*$/i.test(statement)) {
      return columnsForRefs(refs, schema);
    }

    if (/^\s*INSERT\s*$/i.test(statement)) {
      return keywordPrefixMatches(partial, ["INTO"]);
    }

    if (/^\s*UPDATE\s*$/i.test(statement)) {
      return keywordPrefixMatches(partial, tables);
    }

    if (/^\s*DELETE\s*$/i.test(statement)) {
      return keywordPrefixMatches(partial, ["FROM"]);
    }

    return [];
  }

  function endsWithKeywordAndPartial(text, keyword) {
    return new RegExp("\\b" + keyword + "\\s+([A-Za-z_][A-Za-z0-9_]*)$", "i").exec(text);
  }

  function bestInlineCompletion(textBeforeCursor, schema) {
    const statement = lastStatement(textBeforeCursor).trimStart();
    const partial = trailingPartial(statement);
    const qualified = trailingQualified(statement);
    const activePartial = qualified ? qualified.partial : partial;
    const options = getValidCompletions(textBeforeCursor, schema);

    if (!options.length) {
      return null;
    }

    const lower = activePartial.toLowerCase();
    const matches = options.filter((option) => {
      const text = String(option);
      return text.toLowerCase().startsWith(lower) && text.length > activePartial.length;
    });

    function pack(fullText) {
      return {
        suffix: fullText.slice(activePartial.length),
        partialLength: activePartial.length,
        fullText,
      };
    }

    if (!matches.length) {
      if (!activePartial && options.length === 1) {
        return pack(options[0]);
      }
      return null;
    }

    if (matches.length === 1) {
      return pack(matches[0]);
    }

    let common = matches[0].slice(activePartial.length);
    for (let i = 1; i < matches.length; i += 1) {
      const suffix = matches[i].slice(activePartial.length);
      while (common && !suffix.startsWith(common)) {
        common = common.slice(0, -1);
      }
    }

    if (!common || common.length < 2) {
      return null;
    }

    return pack(activePartial + common);
  }

  function attachInlineCompletion(editor, schema) {
    const schemaIndex = buildSchemaIndex(schema);
    let ghostMarker = null;
    let ghostSuffix = "";
    let ghostCompletion = null;

    function clearGhost() {
      if (ghostMarker) {
        ghostMarker.clear();
        ghostMarker = null;
      }
      ghostSuffix = "";
      ghostCompletion = null;
    }

    function acceptGhost(cm) {
      if (!ghostCompletion) {
        return false;
      }
      const pos = cm.getCursor();
      const from = {
        line: pos.line,
        ch: pos.ch - ghostCompletion.partialLength,
      };
      const insertText = formatCompletion(ghostCompletion.fullText, schemaIndex);
      cm.replaceRange(insertText, from, pos);
      clearGhost();
      return true;
    }

    function refreshGhost() {
      clearGhost();
      const pos = editor.getCursor();
      const textBefore = editor.getRange({ line: 0, ch: 0 }, pos);
      const completion = bestInlineCompletion(textBefore, schema);
      if (!completion || !completion.suffix) {
        return;
      }

      ghostCompletion = completion;
      ghostSuffix = completion.suffix;
      const widget = document.createElement("span");
      widget.className = "sql-inline-ghost";
      widget.textContent = ghostSuffix;
      ghostMarker = editor.setBookmark(pos, { widget, insertLeft: false });
    }

    editor.on("cursorActivity", refreshGhost);
    editor.on("change", function () {
      window.requestAnimationFrame(refreshGhost);
    });

    return {
      acceptGhost,
      clearGhost,
    };
  }

  global.SqlInlineCompletion = {
    attachInlineCompletion,
    bestInlineCompletion,
    getValidCompletions,
  };
}(window));
