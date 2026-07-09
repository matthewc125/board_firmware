-- Board firmware log schema (fruread / getrev aligned)

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS boards (
    board_id      INTEGER PRIMARY KEY,
    tool          TEXT,
    board_slot    TEXT,
    manufacturer  TEXT NOT NULL DEFAULT 'PDF Solutions Inc.',
    board_name    TEXT NOT NULL,
    serial        TEXT NOT NULL,
    part_number   TEXT,
    revision      TEXT,
    file_id       TEXT,
    product_name  TEXT NOT NULL,
    ddr_fbga      TEXT
);

CREATE TABLE IF NOT EXISTS firmware_history (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id    INTEGER NOT NULL REFERENCES boards (board_id),
    event_date  TEXT NOT NULL,
    event_time  TEXT,
    fpga        TEXT,
    firmware    TEXT NOT NULL,
    installer   TEXT,
    result      TEXT
);

CREATE TABLE IF NOT EXISTS deleted_boards (
    board_id      INTEGER PRIMARY KEY,
    tool          TEXT,
    board_slot    TEXT,
    manufacturer  TEXT NOT NULL DEFAULT 'PDF Solutions Inc.',
    board_name    TEXT NOT NULL,
    serial        TEXT NOT NULL,
    part_number   TEXT,
    revision      TEXT,
    file_id       TEXT,
    product_name  TEXT NOT NULL,
    ddr_fbga      TEXT,
    deleted_at    TEXT NOT NULL
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

CREATE INDEX IF NOT EXISTS idx_firmware_history_board_date
    ON firmware_history (board_id, event_date, event_time);

CREATE VIEW IF NOT EXISTS current_firmware AS
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
