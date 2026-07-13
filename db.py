import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config import DATABASE

ARCHIVE_TABLES = frozenset({"deleted_boards", "deleted_firmware_history"})

BOARD_COLUMNS = (
    "board_id", "tool", "board_slot", "manufacturer", "board_name",
    "serial", "part_number", "revision", "file_id", "product_name", "ddr_fbga",
    "inventory_serial", "status", "role", "comment", "open_item", "po",
    "modified_by", "source_updated_at", "data_source",
    "dc_status", "ac_status", "gcal_status", "adc_status", "eeprom_status",
)

HISTORY_COLUMNS = (
    "event_id", "board_id", "event_date", "event_time", "fpga",
    "firmware", "installer", "result",
)

BOARD_EVENT_COLUMNS = (
    "event_id", "board_id", "event_date", "event_time", "event_type",
    "description", "tool", "source", "source_ref",
)

BOARD_INVENTORY_COLUMNS = BOARD_COLUMNS[11:]

# Boards verified despite merged/imported data_source (original log + known-good BAP).
VERIFIED_BOARD_IDS = frozenset({1, 8})


def firmware_verified_sql(board_alias="b"):
    """SQL expression for firmware verification status."""
    ids = ", ".join(str(i) for i in sorted(VERIFIED_BOARD_IDS))
    return f"""
    CASE
        WHEN {board_alias}.board_id IN ({ids}) THEN 'verified'
        WHEN {board_alias}.data_source = 'firmware_log' THEN 'verified'
        WHEN {board_alias}.tool = 'Tool13' AND {board_alias}.product_name = 'BAP' THEN 'verified'
        ELSE 'unverified'
    END
    """


def is_firmware_verified(board) -> bool:
    if not board:
        return False
    if board.get("board_id") in VERIFIED_BOARD_IDS:
        return True
    if board.get("data_source") == "firmware_log":
        return True
    if board.get("tool") == "Tool13" and board.get("product_name") == "BAP":
        return True
    return False


def firmware_verified_label(board) -> str:
    return "verified" if is_firmware_verified(board) else "unverified"


def tool_sort_order_sql(tool_column="tool"):
    """Sort tools numerically (Tool2 before Tool12); Unassigned last."""
    return f"""
    CASE
        WHEN {tool_column} = 'Unassigned' THEN 3
        WHEN {tool_column} LIKE 'Tool%' THEN 1
        ELSE 2
    END,
    CASE
        WHEN {tool_column} LIKE 'Tool%' THEN CAST(SUBSTR({tool_column}, 5) AS INTEGER)
    END,
    {tool_column} ASC
    """

NEW_BOARD_COLUMNS_DDL = """
    inventory_serial  TEXT,
    status            TEXT,
    role              TEXT,
    comment           TEXT,
    open_item         TEXT,
    po                TEXT,
    modified_by       TEXT,
    source_updated_at TEXT,
    data_source       TEXT,
    dc_status         TEXT,
    ac_status         TEXT,
    gcal_status       TEXT,
    adc_status        TEXT,
    eeprom_status     TEXT
"""

CURRENT_FIRMWARE_VIEW = f"""
DROP VIEW IF EXISTS current_firmware;
CREATE VIEW current_firmware AS
SELECT
    b.board_id,
    b.tool,
    b.product_name,
    h.firmware,
    h.fpga,
    h.event_date,
    h.event_time,
    {firmware_verified_sql("b").strip()} AS firmware_verified
FROM boards b
JOIN firmware_history h ON h.board_id = b.board_id
WHERE h.event_id = (
    SELECT h2.event_id
    FROM firmware_history h2
    WHERE h2.board_id = b.board_id
    ORDER BY
        h2.event_date DESC,
        COALESCE(h2.event_time, '00:00:00') DESC,
        h2.event_id DESC
    LIMIT 1
);
"""

ARCHIVE_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS deleted_boards (
    board_id          INTEGER PRIMARY KEY,
    tool              TEXT,
    board_slot        TEXT,
    manufacturer      TEXT NOT NULL DEFAULT 'PDF Solutions Inc.',
    board_name        TEXT NOT NULL,
    serial            TEXT NOT NULL,
    part_number       TEXT,
    revision          TEXT,
    file_id           TEXT,
    product_name      TEXT NOT NULL,
    ddr_fbga          TEXT,
    inventory_serial  TEXT,
    status            TEXT,
    role              TEXT,
    comment           TEXT,
    open_item         TEXT,
    po                TEXT,
    modified_by       TEXT,
    source_updated_at TEXT,
    data_source       TEXT,
    dc_status         TEXT,
    ac_status         TEXT,
    gcal_status       TEXT,
    adc_status        TEXT,
    eeprom_status     TEXT,
    deleted_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deleted_firmware_history (
    event_id    INTEGER PRIMARY KEY,
    board_id    INTEGER NOT NULL,
    event_date  TEXT NOT NULL,
    event_time  TEXT,
    fpga        TEXT,
    firmware    TEXT NOT NULL,
    installer   TEXT,
    result      TEXT,
    deleted_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS board_events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id      INTEGER NOT NULL REFERENCES boards (board_id),
    event_date    TEXT NOT NULL,
    event_time    TEXT,
    event_type    TEXT NOT NULL,
    description   TEXT NOT NULL,
    tool          TEXT,
    source        TEXT NOT NULL,
    source_ref    TEXT
);

CREATE TABLE IF NOT EXISTS import_runs (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name    TEXT NOT NULL,
    imported_at    TEXT NOT NULL,
    file_path      TEXT,
    rows_read      INTEGER,
    boards_created INTEGER,
    boards_updated INTEGER,
    boards_merged  INTEGER,
    events_created INTEGER,
    warnings       TEXT
);
"""


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


PRODUCT_FILE_ID_BY_BOARD = {
    7: "CCB local-IO EEPROM 0x50",
}


def migrate_revision_file_id(conn):
    """Move mis-imported Rev values from file_id into revision."""
    for row in conn.execute("SELECT board_id, revision, file_id FROM boards"):
        board_id = row[0]
        revision = row[1]
        file_id = row[2]
        if revision is None and file_id and str(file_id).startswith("Rev"):
            conn.execute(
                "UPDATE boards SET revision = ?, file_id = NULL WHERE board_id = ?",
                (file_id, board_id),
            )
    for board_id, product_file_id in PRODUCT_FILE_ID_BY_BOARD.items():
        conn.execute(
            "UPDATE boards SET file_id = ? WHERE board_id = ?",
            (product_file_id, board_id),
        )


def _table_columns(conn, table_name):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")]


def _rebuild_table_without_column(conn, table_name, keep_columns, create_sql):
    cols_csv = ", ".join(keep_columns)
    conn.execute(create_sql)
    conn.execute(
        f"INSERT INTO {table_name}_new ({cols_csv}) SELECT {cols_csv} FROM {table_name}"
    )
    conn.execute(f"DROP TABLE {table_name}")
    conn.execute(f"ALTER TABLE {table_name}_new RENAME TO {table_name}")


def _migrate_soft_deleted_rows(conn):
    """Move legacy deleted_at rows into archive tables and drop deleted_at columns."""
    conn.execute("DROP VIEW IF EXISTS current_firmware")
    conn.executescript(ARCHIVE_TABLES_DDL)

    board_cols = _table_columns(conn, "boards")
    history_cols = _table_columns(conn, "firmware_history")
    if "deleted_at" not in board_cols and "deleted_at" not in history_cols:
        return

    if "deleted_at" in board_cols:
        for row in conn.execute(
            "SELECT * FROM boards WHERE deleted_at IS NOT NULL"
        ).fetchall():
            board = dict(row)
            deleted_at = board.pop("deleted_at")
            conn.execute(
                f"INSERT OR REPLACE INTO deleted_boards ({', '.join(BOARD_COLUMNS)}, deleted_at) "
                f"VALUES ({', '.join('?' for _ in BOARD_COLUMNS)}, ?)",
                [board.get(col) for col in BOARD_COLUMNS] + [deleted_at],
            )
            for hist in conn.execute(
                "SELECT * FROM firmware_history WHERE board_id = ?",
                (board["board_id"],),
            ).fetchall():
                event = dict(hist)
                event_deleted_at = event.pop("deleted_at", None) or deleted_at
                conn.execute(
                    f"INSERT OR REPLACE INTO deleted_firmware_history "
                    f"({', '.join(HISTORY_COLUMNS)}, deleted_at) "
                    f"VALUES ({', '.join('?' for _ in HISTORY_COLUMNS)}, ?)",
                    [event.get(col) for col in HISTORY_COLUMNS] + [event_deleted_at],
                )
            conn.execute(
                "DELETE FROM firmware_history WHERE board_id = ?",
                (board["board_id"],),
            )
            conn.execute("DELETE FROM boards WHERE board_id = ?", (board["board_id"],))

    if "deleted_at" in history_cols:
        for row in conn.execute(
            "SELECT * FROM firmware_history WHERE deleted_at IS NOT NULL"
        ).fetchall():
            event = dict(row)
            deleted_at = event.pop("deleted_at")
            conn.execute(
                f"INSERT OR REPLACE INTO deleted_firmware_history "
                f"({', '.join(HISTORY_COLUMNS)}, deleted_at) "
                f"VALUES ({', '.join('?' for _ in HISTORY_COLUMNS)}, ?)",
                [event.get(col) for col in HISTORY_COLUMNS] + [deleted_at],
            )
            conn.execute(
                "DELETE FROM firmware_history WHERE event_id = ?",
                (event["event_id"],),
            )

    if "deleted_at" in _table_columns(conn, "boards"):
        conn.execute("PRAGMA foreign_keys = OFF")
        _rebuild_table_without_column(
            conn,
            "boards",
            BOARD_COLUMNS,
            """
            CREATE TABLE boards_new (
                board_id          INTEGER PRIMARY KEY,
                tool              TEXT,
                board_slot        TEXT,
                manufacturer      TEXT NOT NULL DEFAULT 'PDF Solutions Inc.',
                board_name        TEXT NOT NULL,
                serial            TEXT NOT NULL,
                part_number       TEXT,
                revision          TEXT,
                file_id           TEXT,
                product_name      TEXT NOT NULL,
                ddr_fbga          TEXT,
                inventory_serial  TEXT,
                status            TEXT,
                role              TEXT,
                comment           TEXT,
                open_item         TEXT,
                po                TEXT,
                modified_by       TEXT,
                source_updated_at TEXT,
                data_source       TEXT,
                dc_status         TEXT,
                ac_status         TEXT,
                gcal_status       TEXT,
                adc_status        TEXT,
                eeprom_status     TEXT
            );
            """,
        )

    if "deleted_at" in _table_columns(conn, "firmware_history"):
        _rebuild_table_without_column(
            conn,
            "firmware_history",
            HISTORY_COLUMNS,
            """
            CREATE TABLE firmware_history_new (
                event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                board_id    INTEGER NOT NULL REFERENCES boards (board_id),
                event_date  TEXT NOT NULL,
                event_time  TEXT,
                fpga        TEXT,
                firmware    TEXT NOT NULL,
                installer   TEXT,
                result      TEXT
            );
            """,
        )

    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_board_inventory_columns(conn):
    """Add inventory metadata columns to boards and deleted_boards."""
    for table in ("boards", "deleted_boards"):
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone():
            continue
        existing = set(_table_columns(conn, table))
        for col in BOARD_INVENTORY_COLUMNS:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")


def ensure_schema():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        board_cols = _table_columns(conn, "boards") if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='boards'"
        ).fetchone() else []
        if board_cols and "revision" not in board_cols:
            conn.execute("ALTER TABLE boards ADD COLUMN revision TEXT")
        _migrate_soft_deleted_rows(conn)
        conn.executescript(ARCHIVE_TABLES_DDL)
        _migrate_board_inventory_columns(conn)
        migrate_revision_file_id(conn)
        conn.executescript(CURRENT_FIRMWARE_VIEW)
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_boards_inventory_serial
            ON boards (inventory_serial)
            WHERE inventory_serial IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_board_events_board_date
            ON board_events (board_id, event_date)
            """
        )
        ensure_firmware_catalog(conn)
        conn.commit()
    finally:
        conn.close()


def dict_row(cursor, row):
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = dict_row
    try:
        yield conn
    finally:
        conn.close()


def fetch_all(query, params=()):
    with get_db() as conn:
        return conn.execute(query, params).fetchall()


def fetch_one(query, params=()):
    with get_db() as conn:
        return conn.execute(query, params).fetchone()


def execute(query, params=()):
    with get_db() as conn:
        cur = conn.execute(query, params)
        conn.commit()
        return cur.lastrowid


BOARD_SEARCH_COLUMNS = (
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
)


def _board_search_clause(search, alias="b", extra_columns=()):
    if not search:
        return None, []
    like = f"%{search}%"
    columns = [col.format(a=alias) for col in BOARD_SEARCH_COLUMNS] + list(extra_columns)
    clause = "(" + " OR ".join(f"{col} LIKE ?" for col in columns) + ")"
    return clause, [like] * len(columns)


def dashboard_stats():
    return fetch_one(
        """
        SELECT
            (SELECT COUNT(*) FROM boards) AS board_count,
            (SELECT COUNT(*) FROM firmware_history) AS history_count,
            (SELECT COUNT(DISTINCT firmware) FROM firmware_history) AS firmware_versions
        """
    )


def recent_installs(limit=10):
    return fetch_all(
        """
        SELECT
            h.event_id,
            h.event_date,
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
        ORDER BY h.event_date DESC, COALESCE(h.event_time, '00:00:00') DESC, h.event_id DESC
        LIMIT ?
        """,
        (limit,),
    )


def firmware_stats():
    return fetch_all(
        """
        SELECT firmware, COUNT(*) AS board_count
        FROM current_firmware
        GROUP BY firmware
        ORDER BY board_count DESC, firmware DESC
        LIMIT 10
        """
    )


def board_products():
    return fetch_all(
        """
        SELECT product_name, COUNT(*) AS board_count
        FROM boards
        GROUP BY product_name
        ORDER BY product_name
        """
    )


def firmware_for_product(product_name):
    rows = fetch_all(
        """
        SELECT DISTINCT cf.firmware
        FROM current_firmware cf
        JOIN boards b ON b.board_id = cf.board_id
        WHERE b.product_name = ? AND cf.firmware IS NOT NULL
        """,
        (product_name,),
    )
    return {row["firmware"] for row in rows}


def products_for_firmware(firmware):
    rows = fetch_all(
        """
        SELECT DISTINCT b.product_name
        FROM current_firmware cf
        JOIN boards b ON b.board_id = cf.board_id
        WHERE cf.firmware = ?
        """,
        (firmware,),
    )
    return {row["product_name"] for row in rows}


def tool_stats():
    return fetch_all(
        f"""
        SELECT COALESCE(tool, 'Unassigned') AS tool, COUNT(*) AS board_count
        FROM boards
        GROUP BY COALESCE(tool, 'Unassigned')
        ORDER BY {tool_sort_order_sql("tool")}
        """
    )


def products_for_tool(tool):
    if tool == "Unassigned":
        rows = fetch_all(
            "SELECT DISTINCT product_name FROM boards WHERE tool IS NULL"
        )
    else:
        rows = fetch_all(
            "SELECT DISTINCT product_name FROM boards WHERE tool = ?",
            (tool,),
        )
    return {row["product_name"] for row in rows}


def firmware_for_tool(tool):
    if tool == "Unassigned":
        rows = fetch_all(
            """
            SELECT DISTINCT cf.firmware
            FROM current_firmware cf
            JOIN boards b ON b.board_id = cf.board_id
            WHERE b.tool IS NULL AND cf.firmware IS NOT NULL
            """
        )
    else:
        rows = fetch_all(
            """
            SELECT DISTINCT cf.firmware
            FROM current_firmware cf
            JOIN boards b ON b.board_id = cf.board_id
            WHERE b.tool = ? AND cf.firmware IS NOT NULL
            """,
            (tool,),
        )
    return {row["firmware"] for row in rows}


def tools_for_product(product_name):
    rows = fetch_all(
        """
        SELECT DISTINCT COALESCE(tool, 'Unassigned') AS tool
        FROM boards
        WHERE product_name = ?
        """,
        (product_name,),
    )
    return {row["tool"] for row in rows}


def tools_for_firmware(firmware):
    rows = fetch_all(
        """
        SELECT DISTINCT COALESCE(b.tool, 'Unassigned') AS tool
        FROM current_firmware cf
        JOIN boards b ON b.board_id = cf.board_id
        WHERE cf.firmware = ?
        """,
        (firmware,),
    )
    return {row["tool"] for row in rows}


def board_types():
    return fetch_all(
        """
        SELECT
            product_name,
            board_name,
            COUNT(*) AS board_count
        FROM boards
        GROUP BY product_name, board_name
        ORDER BY product_name, board_name
        """
    )


def list_boards(product_name=None, board_name=None, firmware=None, tool=None, search=None, sort="board_id", order="asc"):
    allowed_sort = {
        "board_id": "b.board_id",
        "tool": "b.tool",
        "board_name": "b.board_name",
        "product_name": "b.product_name",
        "serial": "b.serial",
        "firmware": "cf.firmware",
        "event_date": "cf.event_date",
    }
    sort_col = allowed_sort.get(sort, "b.board_id")
    sort_dir = "DESC" if order == "desc" else "ASC"

    clauses = []
    params = []

    if product_name:
        clauses.append("b.product_name = ?")
        params.append(product_name)
    if board_name:
        clauses.append("b.board_name = ?")
        params.append(board_name)
    if firmware:
        clauses.append("cf.firmware = ?")
        params.append(firmware)
    if tool:
        if tool == "Unassigned":
            clauses.append("b.tool IS NULL")
        else:
            clauses.append("b.tool = ?")
            params.append(tool)
    search_clause, search_params = _board_search_clause(search, "b", ["cf.firmware"])
    if search_clause:
        clauses.append(search_clause)
        params.extend(search_params)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    return fetch_all(
        f"""
        SELECT
            b.*,
            cf.firmware AS current_firmware,
            cf.fpga AS current_fpga,
            COALESCE(cf.event_date, b.source_updated_at) AS last_update,
            {firmware_verified_sql("b")} AS firmware_verified
        FROM boards b
        LEFT JOIN current_firmware cf ON cf.board_id = b.board_id
        {where}
        ORDER BY {sort_col} {sort_dir}, b.board_id ASC
        """,
        params,
    )


def list_hardware(search=None, sort="board_id", order="asc"):
    allowed_sort = {
        "board_id": "board_id",
        "tool": "tool",
        "board_slot": "board_slot",
        "board_name": "board_name",
        "product_name": "product_name",
        "serial": "serial",
        "inventory_serial": "inventory_serial",
        "status": "status",
        "part_number": "part_number",
        "revision": "revision",
        "file_id": "file_id",
        "ddr_fbga": "ddr_fbga",
        "manufacturer": "manufacturer",
    }
    sort_col = allowed_sort.get(sort, "board_id")
    sort_dir = "DESC" if order == "desc" else "ASC"

    clauses = []
    params = []

    search_clause, search_params = _board_search_clause(search, "boards")
    if search_clause:
        clauses.append(search_clause)
        params.extend(search_params)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    return fetch_all(
        f"""
        SELECT *
        FROM boards
        {where}
        ORDER BY {sort_col} {sort_dir}, board_id ASC
        """,
        params,
    )


def get_board(board_id):
    return fetch_one("SELECT * FROM boards WHERE board_id = ?", (board_id,))


def get_deleted_board(board_id):
    return fetch_one("SELECT * FROM deleted_boards WHERE board_id = ?", (board_id,))


def board_history(board_id):
    return fetch_all(
        """
        SELECT *
        FROM firmware_history
        WHERE board_id = ?
        ORDER BY event_date DESC, COALESCE(event_time, '00:00:00') DESC, event_id DESC
        """,
        (board_id,),
    )


def board_events(board_id):
    return fetch_all(
        """
        SELECT *
        FROM board_events
        WHERE board_id = ?
        ORDER BY event_date DESC, COALESCE(event_time, '00:00:00') DESC, event_id DESC
        """,
        (board_id,),
    )


def insert_board_event(data):
    cols = [c for c in BOARD_EVENT_COLUMNS if c != "event_id"]
    placeholders = ", ".join("?" for _ in cols)
    values = [data.get(c) for c in cols]
    return execute(
        f"INSERT INTO board_events ({', '.join(cols)}) VALUES ({placeholders})",
        values,
    )


def get_history_event(event_id):
    return fetch_one("SELECT * FROM firmware_history WHERE event_id = ?", (event_id,))


def get_deleted_history_event(event_id):
    return fetch_one(
        "SELECT * FROM deleted_firmware_history WHERE event_id = ?",
        (event_id,),
    )


def insert_board(data):
    placeholders = ", ".join("?" for _ in BOARD_COLUMNS)
    values = [data.get(c) for c in BOARD_COLUMNS]
    execute(
        f"INSERT INTO boards ({', '.join(BOARD_COLUMNS)}) VALUES ({placeholders})",
        values,
    )


def update_board(board_id, data):
    cols = [c for c in BOARD_COLUMNS if c != "board_id"]
    sets = ", ".join(f"{c} = ?" for c in cols)
    values = [data.get(c) for c in cols] + [board_id]
    execute(f"UPDATE boards SET {sets} WHERE board_id = ?", values)


def insert_history(data):
    cols = [c for c in HISTORY_COLUMNS if c != "event_id"]
    placeholders = ", ".join("?" for _ in cols)
    values = [data.get(c) for c in cols]
    return execute(
        f"INSERT INTO firmware_history ({', '.join(cols)}) VALUES ({placeholders})",
        values,
    )


def update_history(event_id, data):
    cols = [c for c in HISTORY_COLUMNS if c != "event_id"]
    sets = ", ".join(f"{c} = ?" for c in cols)
    values = [data.get(c) for c in cols] + [event_id]
    execute(f"UPDATE firmware_history SET {sets} WHERE event_id = ?", values)


def delete_history(event_id):
    ts = _utc_now()
    with get_db() as conn:
        event = conn.execute(
            "SELECT * FROM firmware_history WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if not event:
            return 0
        conn.execute(
            f"INSERT INTO deleted_firmware_history ({', '.join(HISTORY_COLUMNS)}, deleted_at) "
            f"VALUES ({', '.join('?' for _ in HISTORY_COLUMNS)}, ?)",
            [event[col] for col in HISTORY_COLUMNS] + [ts],
        )
        cur = conn.execute("DELETE FROM firmware_history WHERE event_id = ?", (event_id,))
        conn.commit()
        return cur.rowcount


def renumber_boards():
    """Renumber active boards to sequential IDs starting at 1."""
    temp_offset = 1_000_000
    child_tables = ("firmware_history", "board_events")
    with get_db() as conn:
        board_ids = [
            row["board_id"]
            for row in conn.execute("SELECT board_id FROM boards ORDER BY board_id").fetchall()
        ]
        if not board_ids:
            return {"renumbered": 0, "mapping": {}}

        mapping = {
            old_id: new_id
            for new_id, old_id in enumerate(board_ids, start=1)
        }
        if all(old_id == new_id for old_id, new_id in mapping.items()):
            return {"renumbered": 0, "mapping": mapping}

        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for old_id, new_id in mapping.items():
                temp_id = temp_offset + new_id
                for table in child_tables:
                    conn.execute(
                        f"UPDATE {table} SET board_id = ? WHERE board_id = ?",
                        (temp_id, old_id),
                    )
                conn.execute(
                    "UPDATE boards SET board_id = ? WHERE board_id = ?",
                    (temp_id, old_id),
                )

            for new_id in range(1, len(board_ids) + 1):
                temp_id = temp_offset + new_id
                for table in (*child_tables, "boards"):
                    conn.execute(
                        f"UPDATE {table} SET board_id = ? WHERE board_id = ?",
                        (new_id, temp_id),
                    )
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.commit()

        return {"renumbered": len(mapping), "mapping": mapping}


def delete_board(board_id):
    ts = _utc_now()
    with get_db() as conn:
        board = conn.execute(
            "SELECT * FROM boards WHERE board_id = ?",
            (board_id,),
        ).fetchone()
        if not board:
            return 0
        conn.execute("DELETE FROM deleted_firmware_history WHERE board_id = ?", (board_id,))
        conn.execute("DELETE FROM deleted_boards WHERE board_id = ?", (board_id,))
        conn.execute(
            f"INSERT INTO deleted_boards ({', '.join(BOARD_COLUMNS)}, deleted_at) "
            f"VALUES ({', '.join('?' for _ in BOARD_COLUMNS)}, ?)",
            [board[col] for col in BOARD_COLUMNS] + [ts],
        )
        for event in conn.execute(
            "SELECT * FROM firmware_history WHERE board_id = ?",
            (board_id,),
        ).fetchall():
            conn.execute(
                f"INSERT INTO deleted_firmware_history ({', '.join(HISTORY_COLUMNS)}, deleted_at) "
                f"VALUES ({', '.join('?' for _ in HISTORY_COLUMNS)}, ?)",
                [event[col] for col in HISTORY_COLUMNS] + [ts],
            )
        conn.execute("DELETE FROM firmware_history WHERE board_id = ?", (board_id,))
        conn.execute("DELETE FROM board_events WHERE board_id = ?", (board_id,))
        cur = conn.execute("DELETE FROM boards WHERE board_id = ?", (board_id,))
        conn.commit()
        return cur.rowcount


def restore_board(board_id):
    with get_db() as conn:
        board = conn.execute(
            "SELECT * FROM deleted_boards WHERE board_id = ?",
            (board_id,),
        ).fetchone()
        if not board:
            return False
        conn.execute(
            f"INSERT INTO boards ({', '.join(BOARD_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in BOARD_COLUMNS)})",
            [board[col] for col in BOARD_COLUMNS],
        )
        for event in conn.execute(
            "SELECT * FROM deleted_firmware_history WHERE board_id = ?",
            (board_id,),
        ).fetchall():
            conn.execute(
                f"INSERT INTO firmware_history ({', '.join(HISTORY_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in HISTORY_COLUMNS)})",
                [event[col] for col in HISTORY_COLUMNS],
            )
        conn.execute(
            "DELETE FROM deleted_firmware_history WHERE board_id = ?",
            (board_id,),
        )
        conn.execute("DELETE FROM deleted_boards WHERE board_id = ?", (board_id,))
        conn.commit()
        return True


def restore_history(event_id):
    with get_db() as conn:
        event = conn.execute(
            "SELECT * FROM deleted_firmware_history WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if not event:
            return False
        board = conn.execute(
            "SELECT 1 FROM boards WHERE board_id = ?",
            (event["board_id"],),
        ).fetchone()
        if not board:
            raise ValueError("Restore the board before restoring individual history records.")
        conn.execute(
            f"INSERT INTO firmware_history ({', '.join(HISTORY_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in HISTORY_COLUMNS)})",
            [event[col] for col in HISTORY_COLUMNS],
        )
        conn.execute(
            "DELETE FROM deleted_firmware_history WHERE event_id = ?",
            (event_id,),
        )
        conn.commit()
        return True


def list_deleted_boards():
    return fetch_all(
        """
        SELECT
            b.*,
            (SELECT COUNT(*) FROM deleted_firmware_history h WHERE h.board_id = b.board_id) AS history_count
        FROM deleted_boards b
        ORDER BY b.deleted_at DESC, b.board_id DESC
        """
    )


def list_deleted_history():
    return fetch_all(
        """
        SELECT
            h.*,
            b.product_name,
            b.board_name,
            b.tool,
            b.serial
        FROM deleted_firmware_history h
        JOIN boards b ON b.board_id = h.board_id
        ORDER BY h.deleted_at DESC, h.event_id DESC
        """
    )


def next_board_id():
    row = fetch_one(
        """
        SELECT COALESCE(MAX(board_id), 0) + 1 AS next_id
        FROM (
            SELECT board_id FROM boards
            UNION ALL
            SELECT board_id FROM deleted_boards
        )
        """
    )
    return row["next_id"]


def list_history(product_name=None, firmware=None, tool=None, search=None, limit=None):
    clauses = []
    params = []

    if product_name:
        clauses.append("b.product_name = ?")
        params.append(product_name)
    if firmware:
        clauses.append("h.firmware = ?")
        params.append(firmware)
    if tool:
        if tool == "Unassigned":
            clauses.append("b.tool IS NULL")
        else:
            clauses.append("b.tool = ?")
            params.append(tool)
    search_clause, search_params = _board_search_clause(search, "b", ["h.firmware"])
    if search_clause:
        clauses.append(search_clause)
        params.extend(search_params)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    query = f"""
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
            b.serial,
            {firmware_verified_sql("b")} AS firmware_verified
        FROM firmware_history h
        JOIN boards b ON b.board_id = h.board_id
        {where}
        ORDER BY h.event_date DESC, COALESCE(h.event_time, '00:00:00') DESC, h.event_id DESC
    """
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return fetch_all(query, params)


def list_tables(include_archives=False, include_internal=False):
    rows = fetch_all(
        """
        SELECT name, type
        FROM sqlite_master
        WHERE type IN ('table', 'view')
          AND name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    )
    if not include_internal:
        rows = [row for row in rows if row["name"] not in NON_EXPORT_TABLES]
    if include_archives:
        return rows
    return [row for row in rows if row["name"] not in ARCHIVE_TABLES]


def table_preview_query(table_name):
    if table_name == "deleted_boards":
        return "SELECT * FROM deleted_boards ORDER BY deleted_at DESC, board_id DESC"
    if table_name == "deleted_firmware_history":
        return (
            "SELECT * FROM deleted_firmware_history "
            "ORDER BY deleted_at DESC, event_id DESC"
        )
    return f"SELECT * FROM {table_name}"


def sql_autocomplete_schema():
    schema = {}
    for table in list_tables(include_archives=True, include_internal=True):
        name = table["name"]
        cols = fetch_all(f"PRAGMA table_info({name})")
        schema[name] = [col["name"] for col in cols]
    return schema


def _query_tokens(upper_sql):
    return set(re.findall(r"\b[A-Z_]+\b", upper_sql))


def run_readonly_query(sql, params=(), max_rows=5000):
    cleaned = sql.strip().rstrip(";")
    upper = cleaned.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise ValueError("Only SELECT queries are allowed.")
    forbidden = {
        "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
        "REPLACE", "ATTACH", "DETACH", "PRAGMA", "VACUUM",
    }
    tokens = _query_tokens(upper)
    blocked = tokens & forbidden
    if blocked:
        raise ValueError(f"Query may not contain {next(iter(blocked))}.")
    for table in ARCHIVE_TABLES:
        if table.upper() in upper:
            raise ValueError(f"Query may not access archive table {table}.")

    with get_db() as conn:
        conn.execute("PRAGMA query_only = ON")
        cur = conn.execute(cleaned, params)
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        if truncated:
            rows = rows[:max_rows]
        columns = [col[0] for col in cur.description] if cur.description else []
        return columns, rows, truncated


ADMIN_FORBIDDEN = ("DROP", "ALTER", "CREATE", "REPLACE", "ATTACH", "DETACH", "PRAGMA", "VACUUM")


def run_admin_query(sql, params=(), max_rows=5000):
    cleaned = sql.strip().rstrip(";")
    if not cleaned:
        raise ValueError("Query is empty.")
    upper = cleaned.upper()
    forbidden = set(ADMIN_FORBIDDEN)
    blocked = _query_tokens(upper) & forbidden
    if blocked:
        raise ValueError(f"Query may not contain {next(iter(blocked))}.")

    with get_db() as conn:
        cur = conn.execute(cleaned, params)
        if cur.description:
            rows = cur.fetchmany(max_rows + 1)
            truncated = len(rows) > max_rows
            if truncated:
                rows = rows[:max_rows]
            columns = [col[0] for col in cur.description]
            conn.commit()
            return {"kind": "rows", "columns": columns, "rows": rows, "truncated": truncated}

        conn.commit()
        return {"kind": "change", "rowcount": cur.rowcount}


def fetch_table_rows(table_name):
    allowed = {row["name"] for row in list_tables()}
    if table_name not in allowed:
        raise ValueError(f"Unknown table or view: {table_name}")
    return run_readonly_query(f"SELECT * FROM {table_name}")


def firmware_version_key(version):
    """Sort key for dotted firmware strings (numeric segments first)."""
    if version is None:
        return ()
    text = str(version).strip()
    if not text or text.lower() == "field deployed":
        return ()
    parts = []
    for segment in re.split(r"[.\s_\-]+", text):
        if not segment:
            continue
        if segment.isdigit():
            parts.append((0, int(segment), ""))
            continue
        digits = ""
        rest = segment
        while rest and rest[0].isdigit():
            digits += rest[0]
            rest = rest[1:]
        if digits:
            parts.append((0, int(digits), rest.lower()))
        else:
            parts.append((1, 0, segment.lower()))
    return tuple(parts)


def parse_tool_number(tool):
    if not tool:
        return None
    text = str(tool).strip()
    match = re.fullmatch(r"(?i)tool\s*(\d+)", text)
    if not match:
        return None
    return int(match.group(1))



# Fixed columns matching the field firmware-status spreadsheet.
FIRMWARE_STATUS_COLUMNS = (
    "ES4 USF",
    "ES4 LSF",
    "ES4 Blanker",
    "BAP",
    "EM1",
    "OBJ",
    "MFPGA",
    "PIB",
)

# Spreadsheet column -> catalog family used for field-deployed defaults.
STATUS_COLUMN_FAMILY = {
    "ES4 USF": "ES4",
    "ES4 LSF": "ES4",
    "ES4 Blanker": "ES4",
    "BAP": "BAP",
    "EM1": "EM1",
    "OBJ": "OBJ",
    "MFPGA": "MFPGA",
    "PIB": "PIB",
}

# Display / admin order for catalog families.
CATALOG_FAMILY_ORDER = ("BAP", "ES4", "EM1", "OBJ", "MFPGA", "PIB", "FF")

# Status report always shows the field-deployed version for these columns.
STATUS_FIXED_FIELD_COLUMNS = frozenset({"EM1", "OBJ"})


FIELD_DEPLOYED_LABEL = "field deployed"

# Not offered on the public Data export list (admin-managed / internal only).
NON_EXPORT_TABLES = frozenset({
    "firmware_catalog",
    "app_meta",
    "import_runs",
    "board_events",
})


FIRMWARE_CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS firmware_catalog (
    catalog_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    family             TEXT NOT NULL,
    version            TEXT NOT NULL,
    fpga               TEXT,
    release_date       TEXT,
    notes              TEXT,
    is_field_deployed  INTEGER NOT NULL DEFAULT 0,
    tools              TEXT,
    in_status_ranking  INTEGER NOT NULL DEFAULT 1,
    UNIQUE(family, version)
);
"""

# ES4 build suffix rule: last segment .00 vs .01 follows DDR FBGA (D9XSG -> .01).
ES4_DDR_SUFFIX_NOTE = "Build suffix .00/.01 follows DDR FBGA (D9XSG -> .01)."

# family, version, release_date, notes, is_field_deployed, tools, in_status_ranking
FIRMWARE_CATALOG_SEED = (
    ("BAP", "1.0.34", "2026-04-06",
     "BAP v1.0 functionality; some BAP v2.0 support", 1, None, 1),
    ("BAP", "2.02.77", "2026-05-27",
     "ADC & PA, gain/offset, FRU, auto-init", 0, None, 1),
    ("BAP", "2.03.81", "2026-06-03",
     "Units, HWCONST, FRU fix", 0, None, 1),
    ("ES4", "1.04.26", "2025-05-02",
     "Mostly deployed to field", 1, None, 1),
    ("ES4", "1.04.37.01", "2025-06-08",
     f"Glitch fix, larger DDR option, Aurora identification. {ES4_DDR_SUFFIX_NOTE}",
     0, "13", 1),
    ("ES4", "1.04.38.01", "2026-07-13",
     f"Under test - experimental AWG fix. {ES4_DDR_SUFFIX_NOTE}",
     0, None, 1),
    ("EM1", "2.0.1.6", "2022-01-17",
     "Mostly deployed to field", 1, None, 1),
    ("EM1", "3.1.0.21", "2026-06-10",
     "New API, gain/offset, AWG degauss, slew limit", 0, None, 1),
    ("OBJ", "2.0.1.6", "2022-01-17",
     "Mostly deployed to field", 1, None, 1),
    ("OBJ", "3.1.0.21", "2026-06-10",
     "New API, gain/offset, AWG degauss, slew limit", 0, None, 1),
    ("MFPGA", "5.11.1.921", "2025-08-24",
     "Jeff 2D never fail", 0, "1,3,4,5,7,8,12", 1),
    ("MFPGA", "5.13.5.1016", "2025-09-18",
     "Sarah metadata (needed for new SW)", 0, "2", 1),
    ("MFPGA", "6.7.20.1160", "2026-04-23",
     "New architecture, SMA blanker, FM, 1D reg sobel, BAP imaging, FPGA fast autofocus",
     0, "9", 1),
    ("MFPGA", "7.0.2.1163", "2026-05-03",
     "Dell 7960 support", 0, "6,10,11,13", 1),
    ("PIB", "12.37.23", None,
     "Deployed most places in field", 1, None, 1),
    ("PIB", "12.38.10", None,
     "Golden wafer glitch fix (Tool 9; next Tool 6)", 0, "9", 1),
    ("PIB", "12.38.11", None,
     "SOS feature for Scott (not in use)", 0, None, 0),
)

# Bump when seed content must replace an older auto-seeded catalog.
FIRMWARE_CATALOG_SEED_REV = 7


def _firmware_catalog_columns(conn):
    return {row[1] for row in conn.execute("PRAGMA table_info(firmware_catalog)")}


def _migrate_firmware_catalog_drop_aliases(conn):
    cols = _firmware_catalog_columns(conn)
    if not cols or "aliases" not in cols:
        return
    conn.executescript(
        """
        CREATE TABLE firmware_catalog__new (
            catalog_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            family             TEXT NOT NULL,
            version            TEXT NOT NULL,
            fpga               TEXT,
            release_date       TEXT,
            notes              TEXT,
            is_field_deployed  INTEGER NOT NULL DEFAULT 0,
            tools              TEXT,
            in_status_ranking  INTEGER NOT NULL DEFAULT 1,
            UNIQUE(family, version)
        );
        INSERT INTO firmware_catalog__new
            (catalog_id, family, version, release_date, notes, is_field_deployed, tools, in_status_ranking)
        SELECT catalog_id, family, version, release_date, notes, is_field_deployed, tools, 1
        FROM firmware_catalog;
        DROP TABLE firmware_catalog;
        ALTER TABLE firmware_catalog__new RENAME TO firmware_catalog;
        """
    )


def _migrate_firmware_catalog_ranking_flag(conn):
    cols = _firmware_catalog_columns(conn)
    if not cols or "in_status_ranking" in cols:
        return
    conn.execute(
        "ALTER TABLE firmware_catalog ADD COLUMN in_status_ranking INTEGER NOT NULL DEFAULT 1"
    )


def _migrate_firmware_catalog_fpga(conn):
    cols = _firmware_catalog_columns(conn)
    if not cols or "fpga" in cols:
        return
    conn.execute("ALTER TABLE firmware_catalog ADD COLUMN fpga TEXT")


def _catalog_seed_rev(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = 'firmware_catalog_seed_rev'"
    ).fetchone()
    return int(row["value"]) if row else 0


def _set_catalog_seed_rev(conn, rev):
    conn.execute(
        """
        INSERT INTO app_meta (key, value) VALUES ('firmware_catalog_seed_rev', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(rev),),
    )


def _insert_firmware_catalog_seed(conn):
    for family, version, release_date, notes, is_field, tools, in_rank in FIRMWARE_CATALOG_SEED:
        conn.execute(
            """
            INSERT INTO firmware_catalog
                (family, version, fpga, release_date, notes, is_field_deployed, tools, in_status_ranking)
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (family, version, release_date, notes, is_field, tools, in_rank),
        )


def ensure_firmware_catalog(conn):
    conn.executescript(FIRMWARE_CATALOG_DDL)
    _migrate_firmware_catalog_drop_aliases(conn)
    _migrate_firmware_catalog_ranking_flag(conn)
    _migrate_firmware_catalog_fpga(conn)
    count = conn.execute("SELECT COUNT(*) AS n FROM firmware_catalog").fetchone()["n"]
    rev = _catalog_seed_rev(conn)
    if count == 0:
        _insert_firmware_catalog_seed(conn)
        _set_catalog_seed_rev(conn, FIRMWARE_CATALOG_SEED_REV)
        return
    if rev < FIRMWARE_CATALOG_SEED_REV:
        conn.execute("DELETE FROM firmware_catalog")
        _insert_firmware_catalog_seed(conn)
        _set_catalog_seed_rev(conn, FIRMWARE_CATALOG_SEED_REV)


def _sort_catalog_rows(rows):
    """Newest versions first within each family; families in CATALOG_FAMILY_ORDER."""
    from collections import OrderedDict

    family_rank = {name: idx for idx, name in enumerate(CATALOG_FAMILY_ORDER)}
    by_family = OrderedDict()
    for row in rows:
        by_family.setdefault(row["family"], []).append(row)

    ordered_families = sorted(
        by_family.keys(),
        key=lambda family: (family_rank.get(family, len(CATALOG_FAMILY_ORDER)), family),
    )
    result = []
    for family in ordered_families:
        group = sorted(
            by_family[family],
            key=lambda row: (firmware_version_key(row["version"]), row["version"]),
            reverse=True,
        )
        result.extend(group)
    return result


def list_firmware_catalog(family=None):
    if family:
        rows = fetch_all(
            """
            SELECT * FROM firmware_catalog
            WHERE family = ?
            """,
            (family,),
        )
    else:
        rows = fetch_all("SELECT * FROM firmware_catalog")
    return _sort_catalog_rows(rows)


def list_firmware_families():
    present = {row["family"] for row in fetch_all("SELECT DISTINCT family FROM firmware_catalog")}
    families = [name for name in CATALOG_FAMILY_ORDER if name in present]
    for name in sorted(present):
        if name not in families:
            families.append(name)
    for name in CATALOG_FAMILY_ORDER:
        if name not in families:
            families.append(name)
    return families


def get_firmware_catalog_entry(catalog_id):
    return fetch_one(
        "SELECT * FROM firmware_catalog WHERE catalog_id = ?",
        (catalog_id,),
    )


def insert_firmware_catalog(data):
    with get_db() as conn:
        if data.get("is_field_deployed"):
            conn.execute(
                "UPDATE firmware_catalog SET is_field_deployed = 0 WHERE family = ?",
                (data["family"],),
            )
        cur = conn.execute(
            """
            INSERT INTO firmware_catalog
                (family, version, fpga, release_date, notes, is_field_deployed, tools, in_status_ranking)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["family"],
                data["version"],
                data.get("fpga"),
                data.get("release_date"),
                data.get("notes"),
                1 if data.get("is_field_deployed") else 0,
                data.get("tools"),
                1 if data.get("in_status_ranking", True) else 0,
            ),
        )
        conn.commit()
        return cur.lastrowid


def update_firmware_catalog(catalog_id, data):
    with get_db() as conn:
        if data.get("is_field_deployed"):
            conn.execute(
                """
                UPDATE firmware_catalog
                SET is_field_deployed = 0
                WHERE family = ? AND catalog_id != ?
                """,
                (data["family"], catalog_id),
            )
        conn.execute(
            """
            UPDATE firmware_catalog
            SET family = ?, version = ?, fpga = ?, release_date = ?, notes = ?,
                is_field_deployed = ?, tools = ?, in_status_ranking = ?
            WHERE catalog_id = ?
            """,
            (
                data["family"],
                data["version"],
                data.get("fpga"),
                data.get("release_date"),
                data.get("notes"),
                1 if data.get("is_field_deployed") else 0,
                data.get("tools"),
                1 if data.get("in_status_ranking", True) else 0,
                catalog_id,
            ),
        )
        conn.commit()


def delete_firmware_catalog(catalog_id):
    execute("DELETE FROM firmware_catalog WHERE catalog_id = ?", (catalog_id,))


def set_firmware_catalog_field_deployed(catalog_id):
    entry = get_firmware_catalog_entry(catalog_id)
    if not entry:
        raise ValueError("Catalog entry not found.")
    with get_db() as conn:
        conn.execute(
            "UPDATE firmware_catalog SET is_field_deployed = 0 WHERE family = ?",
            (entry["family"],),
        )
        conn.execute(
            "UPDATE firmware_catalog SET is_field_deployed = 1 WHERE catalog_id = ?",
            (catalog_id,),
        )
        conn.commit()


def _parse_tool_list(tools_text):
    if not tools_text:
        return set()
    numbers = set()
    for part in re.split(r"[,\s]+", str(tools_text).strip()):
        if not part:
            continue
        match = re.search(r"\d+", part)
        if match:
            numbers.add(int(match.group(0)))
    return numbers


def catalog_family_for_board(product_name, board_name=None):
    """Map a board's product/board name onto a firmware_catalog family."""
    product = (product_name or "").strip().upper()
    board = (board_name or "").strip().upper()

    if product == "ES4" or board in {"LSF", "USF", "BLANKER", "ES", "ES4"}:
        return "ES4"
    if product == "EM1" or board == "EM1":
        return "EM1"
    if product == "OBJ" or board == "OBJ":
        return "OBJ"
    if product == "BAP" or board in {"BAP", "BAP2"}:
        return "BAP"
    if product in {"MFPGA", "FPGA"} or board in {"MFPGA", "FPGA"}:
        return "MFPGA"
    if product in {"PIB", "PDB"} or board in {"PIB", "PDB"}:
        return "PIB"
    if product == "FF" or board == "FF":
        return "FF"
    return product or None


def firmware_catalog_by_family():
    """Return {family: [{version, fpga, ...}, ...]} for admin forms (newest first)."""
    from collections import OrderedDict

    grouped = OrderedDict()
    for entry in list_firmware_catalog():
        grouped.setdefault(entry["family"], []).append(
            {
                "version": entry["version"],
                "fpga": entry.get("fpga") or "",
                "is_field_deployed": bool(entry.get("is_field_deployed")),
                "notes": entry.get("notes") or "",
            }
        )
    ordered = OrderedDict()
    for family in list_firmware_families():
        if family in grouped:
            ordered[family] = grouped[family]
    for family, rows in grouped.items():
        if family not in ordered:
            ordered[family] = rows
    return ordered


def catalog_version_for_tool(family, tool_num):
    """Return catalog version assigned to a tool number, if any."""
    for entry in list_firmware_catalog(family):
        if tool_num in _parse_tool_list(entry.get("tools")):
            return entry["version"]
    return None


def catalog_field_deployed_version(family):
    row = fetch_one(
        """
        SELECT version FROM firmware_catalog
        WHERE family = ? AND is_field_deployed = 1
        ORDER BY catalog_id
        LIMIT 1
        """,
        (family,),
    )
    return row["version"] if row else None


def default_status_firmware(column_label, tool_num):
    """Fallback fill for a status-matrix cell with no live board reading."""
    family = STATUS_COLUMN_FAMILY.get(column_label)
    if not family:
        return ""
    tool_version = catalog_version_for_tool(family, tool_num)
    if tool_version:
        return tool_version
    return catalog_field_deployed_version(family) or ""


def _resolve_status_firmware(firmware, column_label, tool_num):
    """Use live firmware, or catalog field/tool defaults when missing or placeholder."""
    text = (firmware or "").strip()
    if text and text.lower() != FIELD_DEPLOYED_LABEL:
        return text
    return default_status_firmware(column_label, tool_num)


def map_board_to_status_column(product_name, board_name):
    """Map a live board row onto a spreadsheet column, or None if out of scope."""
    product = (product_name or "").strip().upper()
    board = (board_name or "").strip().upper()

    if product == "ES4" or board in {"LSF", "USF", "BLANKER", "ES", "ES4"}:
        if board == "LSF":
            return "ES4 LSF"
        if board == "USF":
            return "ES4 USF"
        if board == "BLANKER":
            return "ES4 Blanker"
        # Generic ES4 inventory row — caller expands to all ES4 status columns.
        return "ES4"

    if product == "BAP" or board in {"BAP", "BAP2"}:
        return "BAP"

    if product == "OBJ" or board == "OBJ":
        return "OBJ"
    if product == "EM1" or board == "EM1":
        return "EM1"

    aliases = {
        "FPGA": "MFPGA",
        "MFPGA": "MFPGA",
        "PDB": "PIB",
        "PIB": "PIB",
    }
    if board in aliases:
        return aliases[board]
    if product in aliases:
        return aliases[product]
    return None


def _catalog_versions_for_family(family):
    if not family:
        return set()
    return {
        entry["version"]
        for entry in list_firmware_catalog(family)
        if entry.get("version") and entry.get("in_status_ranking", 1)
    }


def _rank_versions(versions):
    ordered = sorted(
        {v for v in versions if v and v.lower() != FIELD_DEPLOYED_LABEL},
        key=firmware_version_key,
    )
    ranks = {}
    if not ordered:
        return ranks
    if len(ordered) == 1:
        ranks[ordered[0]] = "newest"
        return ranks
    ranks[ordered[-1]] = "newest"
    ranks[ordered[0]] = "oldest"
    for mid in ordered[1:-1]:
        ranks[mid] = "middle"
    return ranks


def _rank_versions_for_column(column_label, observed_versions, family_versions=None):
    """Rank a column using its shared product-family version universe.

    All ES4 columns (USF / LSF / Blanker) share one family, so ``1.04.37.01``
    is middle/outdated whenever ``1.04.38.01`` exists anywhere in ES4.
    EM1/OBJ include catalog versions so field ``2.0.1.6`` ranks oldest vs ``3.1.0.21``.
    """
    family = STATUS_COLUMN_FAMILY.get(column_label)
    if family and family_versions is not None and family in family_versions:
        return _rank_versions(family_versions[family])
    universe = set(observed_versions) | _catalog_versions_for_family(family)
    return _rank_versions(universe)


def firmware_status_matrix(min_tools=13):
    """
    Tool × board-type matrix of current firmware for numbered tools only.

    Columns match the field status spreadsheet. Only live ``boards`` /
    ``current_firmware`` rows are used (never deleted archives). Missing
    cells (or ``field deployed`` placeholders) use catalog tool assignments,
    then the family's field-deployed version from the firmware catalog.
    """
    columns = [{"key": label, "label": label} for label in FIRMWARE_STATUS_COLUMNS]

    rows = fetch_all(
        """
        SELECT
            b.tool,
            b.product_name,
            b.board_name,
            cf.firmware
        FROM boards b
        LEFT JOIN current_firmware cf ON cf.board_id = b.board_id
        WHERE b.tool IS NOT NULL
          AND LOWER(REPLACE(b.tool, ' ', '')) LIKE 'tool%'
        ORDER BY b.tool, b.product_name, b.board_name, b.board_id
        """
    )

    by_tool_col = {}
    max_tool = 0
    es4_status_columns = ("ES4 USF", "ES4 LSF", "ES4 Blanker")

    for row in rows:
        tool_num = parse_tool_number(row["tool"])
        if tool_num is None:
            continue
        max_tool = max(max_tool, tool_num)
        column = map_board_to_status_column(row["product_name"], row["board_name"])
        if column is None:
            continue
        if column in STATUS_FIXED_FIELD_COLUMNS:
            continue
        target_columns = es4_status_columns if column == "ES4" else (column,)
        for target in target_columns:
            firmware = _resolve_status_firmware(row["firmware"], target, tool_num)
            if firmware:
                by_tool_col.setdefault((tool_num, target), set()).add(firmware)

    tool_count = max(min_tools, max_tool) if (max_tool or columns) else 0

    for tool_num in range(1, tool_count + 1):
        for col in FIRMWARE_STATUS_COLUMNS:
            key = (tool_num, col)
            if col in STATUS_FIXED_FIELD_COLUMNS:
                fixed = catalog_field_deployed_version(
                    STATUS_COLUMN_FAMILY[col]
                ) or "2.0.1.6"
                by_tool_col[key] = {fixed}
                continue
            if key not in by_tool_col:
                filled = default_status_firmware(col, tool_num)
                by_tool_col[key] = {filled} if filled else set()

    versions_by_col = {label: set() for label in FIRMWARE_STATUS_COLUMNS}
    for (_tool_num, column), firmwares in by_tool_col.items():
        versions_by_col[column].update(
            fw for fw in firmwares if fw and fw.lower() != FIELD_DEPLOYED_LABEL
        )

    family_versions = {}
    for label, versions in versions_by_col.items():
        family = STATUS_COLUMN_FAMILY.get(label)
        if not family:
            continue
        bucket = family_versions.setdefault(family, set())
        bucket.update(versions)
        bucket.update(_catalog_versions_for_family(family))

    rank_by_col = {
        label: _rank_versions_for_column(label, versions, family_versions)
        for label, versions in versions_by_col.items()
    }

    matrix_rows = []
    for tool_num in range(1, tool_count + 1):
        cells = []
        for col in columns:
            firmwares = sorted(
                (
                    fw
                    for fw in by_tool_col.get((tool_num, col["key"]), set())
                    if fw and fw.lower() != FIELD_DEPLOYED_LABEL
                ),
                key=lambda value: (firmware_version_key(value), value),
            )
            display = " / ".join(firmwares)
            version_ranks = {rank_by_col[col["key"]].get(fw) for fw in firmwares}
            version_ranks.discard(None)
            if not version_ranks:
                rank = None
            elif len(firmwares) <= 1:
                rank = next(iter(version_ranks))
            elif "oldest" in version_ranks:
                rank = "oldest"
            elif version_ranks == {"newest"}:
                rank = "newest"
            else:
                rank = "middle"

            cells.append(
                {
                    "firmware": display,
                    "rank": rank,
                    "versions": firmwares,
                }
            )
        matrix_rows.append(
            {
                "tool": f"Tool {tool_num}",
                "tool_num": tool_num,
                "cells": cells,
            }
        )

    return {"columns": columns, "rows": matrix_rows}


ensure_schema()
