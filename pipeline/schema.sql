-- Board firmware log schema (fruread / getrev aligned)

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS boards (
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
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name   TEXT NOT NULL,
    imported_at   TEXT NOT NULL,
    file_path     TEXT,
    rows_read     INTEGER,
    boards_created INTEGER,
    boards_updated INTEGER,
    boards_merged INTEGER,
    events_created INTEGER,
    warnings      TEXT
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_boards_inventory_serial
    ON boards (inventory_serial)
    WHERE inventory_serial IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_board_events_board_date
    ON board_events (board_id, event_date);

CREATE VIEW IF NOT EXISTS current_firmware AS
SELECT
    b.board_id,
    b.tool,
    b.product_name,
    h.firmware,
    h.fpga,
    h.event_date,
    h.event_time,
    CASE
        WHEN b.board_id IN (1, 8) THEN 'verified'
        WHEN b.data_source = 'firmware_log' THEN 'verified'
        WHEN b.tool = 'Tool13' AND b.product_name = 'BAP' THEN 'verified'
        ELSE 'unverified'
    END AS firmware_verified
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
