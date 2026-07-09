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

CURRENT_FIRMWARE_VIEW = """
DROP VIEW IF EXISTS current_firmware;
CREATE VIEW current_firmware AS
SELECT
    b.board_id,
    b.tool,
    b.product_name,
    h.firmware,
    h.fpga,
    h.event_date,
    h.event_time
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
        """
        SELECT COALESCE(tool, '(Unassigned)') AS tool, COUNT(*) AS board_count
        FROM boards
        GROUP BY COALESCE(tool, '(Unassigned)')
        ORDER BY
            CASE WHEN tool = '(Unassigned)' THEN 1 ELSE 0 END,
            tool ASC
        """
    )


def products_for_tool(tool):
    if tool == "(Unassigned)":
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
    if tool == "(Unassigned)":
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
        SELECT DISTINCT COALESCE(tool, '(Unassigned)') AS tool
        FROM boards
        WHERE product_name = ?
        """,
        (product_name,),
    )
    return {row["tool"] for row in rows}


def tools_for_firmware(firmware):
    rows = fetch_all(
        """
        SELECT DISTINCT COALESCE(b.tool, '(Unassigned)') AS tool
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
        if tool == "(Unassigned)":
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
            COALESCE(cf.event_date, b.source_updated_at) AS last_update
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


def delete_board(board_id):
    ts = _utc_now()
    with get_db() as conn:
        board = conn.execute(
            "SELECT * FROM boards WHERE board_id = ?",
            (board_id,),
        ).fetchone()
        if not board:
            return 0
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
        if tool == "(Unassigned)":
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
            b.serial
        FROM firmware_history h
        JOIN boards b ON b.board_id = h.board_id
        {where}
        ORDER BY h.event_date DESC, COALESCE(h.event_time, '00:00:00') DESC, h.event_id DESC
    """
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return fetch_all(query, params)


def list_tables(include_archives=False):
    rows = fetch_all(
        """
        SELECT name, type
        FROM sqlite_master
        WHERE type IN ('table', 'view')
          AND name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    )
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
    for table in list_tables(include_archives=True):
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


ensure_schema()
