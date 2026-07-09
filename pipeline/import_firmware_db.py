#!/usr/bin/env python3
"""
Build board_firmware.db from firmware_database.xlsm.

Usage:
  py -3 pipeline/import_firmware_db.py
  py -3 pipeline/import_firmware_db.py --xlsm path/to/firmware_database.xlsm
  py -3 pipeline/import_firmware_db.py --db path/to/board_firmware.db
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import date, datetime
from os.path import abspath, dirname, join

import pandas as pd

SCRIPT_DIR = dirname(abspath(__file__))
PROJECT_DIR = dirname(SCRIPT_DIR)
DEFAULT_XLSM = join(SCRIPT_DIR, "firmware_database.xlsm")
DEFAULT_DB = join(PROJECT_DIR, "board_firmware.db")
DEFAULT_SCHEMA = join(SCRIPT_DIR, "schema.sql")

NA_VALUES = {"NOT_APPLICABLE", "UNKNOWN", "N/A", ""}
MANUFACTURER = "PDF Solutions Inc."

# Fallback when xlsm BoardRevision is blank/N/A (matches updated Boards sheet)
BOARD_REVISION_FALLBACK = {
    1: "N",
    2: "A",
    3: "C",
    4: "A",
    5: "AB2",
    6: "D",
    7: "B",
}

# Product EEPROM file_id (from fruread product block) — only boards that have one
PRODUCT_FILE_ID_BY_BOARD = {
    7: "CCB local-IO EEPROM 0x50",
}

BOARD_SLOT_BY_NAME = {
    "USF": "5",
    "LSF": "4",
    "Blanker": "6",
}


def clean_text(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if text.upper() in NA_VALUES:
        return None
    return text


def format_revision(value, board_id: int) -> str | None:
    text = clean_text(value)
    if text is None:
        text = BOARD_REVISION_FALLBACK.get(board_id)
        if text is None:
            return None
    if text.lower().startswith("rev "):
        return text
    return f"Rev {text}"


def map_board_name(board: str) -> str:
    board = board.strip()
    mapping = {
        "EM1": "LSCC",
        "Objective": "LSCC",
        "ES4-USF": "USF",
        "ES4-LSF": "LSF",
        "ES4-Blanker": "Blanker",
        "ES4": "ES4",
    }
    return mapping.get(board, board)


def map_product_name(board: str) -> str:
    board = board.strip()
    if board == "Objective":
        return "OBJ"
    if board.startswith("ES4"):
        return "ES4"
    return board


def map_board_slot(board_name: str) -> str:
    return BOARD_SLOT_BY_NAME.get(board_name, "Bench")


def split_event_date(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
    parsed = pd.to_datetime(value)
    if pd.isna(parsed):
        raise ValueError(f"invalid date: {value!r}")
    return parsed.date().isoformat()


def board_row_from_xlsm(row: pd.Series) -> dict:
    board_id = int(row["BoardID"])
    serial = clean_text(row["SerialNumber"])
    part_number = clean_text(row["PartNumber"])
    revision = format_revision(row["BoardRevision"], board_id)
    board_label = str(row["Board"]).strip()
    board_name = map_board_name(board_label)

    return {
        "board_id": board_id,
        "tool": clean_text(row["Tool"]),
        "board_slot": map_board_slot(board_name),
        "manufacturer": MANUFACTURER,
        "board_name": board_name,
        "serial": serial,
        "part_number": part_number,
        "revision": revision,
        "file_id": PRODUCT_FILE_ID_BY_BOARD.get(board_id),
        "product_name": map_product_name(board_label),
        "ddr_fbga": clean_text(row["DDR_FBGA"]),
    }


def history_row_from_xlsm(row: pd.Series) -> dict:
    return {
        "board_id": int(row["BoardID"]),
        "event_date": split_event_date(row["Date"]),
        "event_time": None,
        "fpga": None,
        "firmware": clean_text(row["Firmware"]),
        "installer": clean_text(row["Installer"]),
        "result": clean_text(row["Result"]),
    }


def rebuild_schema(conn: sqlite3.Connection, schema_path: str) -> None:
    conn.executescript(
        "DROP VIEW IF EXISTS current_firmware;\n"
        "DROP TABLE IF EXISTS firmware_history;\n"
        "DROP TABLE IF EXISTS boards;\n"
    )
    with open(schema_path, encoding="utf-8") as f:
        conn.executescript(f.read())


def import_xlsm(xlsm_path: str, db_path: str, schema_path: str) -> None:
    boards_df = pd.read_excel(xlsm_path, sheet_name="Boards")
    history_df = pd.read_excel(xlsm_path, sheet_name="FirmwareHistory")

    conn = sqlite3.connect(db_path)
    try:
        rebuild_schema(conn, schema_path)

        board_cols = [
            "board_id", "tool", "board_slot", "manufacturer", "board_name",
            "serial", "part_number", "revision", "file_id", "product_name", "ddr_fbga",
        ]
        for _, row in boards_df.iterrows():
            record = board_row_from_xlsm(row)
            conn.execute(
                f"INSERT INTO boards ({', '.join(board_cols)}) "
                f"VALUES ({', '.join('?' for _ in board_cols)})",
                [record[col] for col in board_cols],
            )

        history_cols = [
            "board_id", "event_date", "event_time", "fpga",
            "firmware", "installer", "result",
        ]
        for _, row in history_df.iterrows():
            record = history_row_from_xlsm(row)
            if record["firmware"] is None:
                continue
            conn.execute(
                f"INSERT INTO firmware_history ({', '.join(history_cols)}) "
                f"VALUES ({', '.join('?' for _ in history_cols)})",
                [record[col] for col in history_cols],
            )

        conn.commit()
    finally:
        conn.close()


def print_summary(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        boards = conn.execute(
            """
            SELECT board_id, tool, board_slot, manufacturer, board_name,
                   product_name, serial, revision, file_id, ddr_fbga
            FROM boards ORDER BY board_id
            """
        ).fetchall()
        current = conn.execute("SELECT * FROM current_firmware ORDER BY board_id").fetchall()
        history_count = conn.execute("SELECT COUNT(*) FROM firmware_history").fetchone()[0]

        print(f"Created {db_path}")
        print(f"  boards: {len(boards)}")
        print(f"  firmware_history: {history_count}")
        print("\nBoards:")
        for row in boards:
            print(
                f"  {row['board_id']:>2}  tool={row['tool']:<6}  slot={row['board_slot']:<6}  "
                f"{row['board_name']:<8}  {row['product_name']:<4}  "
                f"serial={row['serial']}  rev={row['revision']}  "
                f"file_id={row['file_id']}  ddr={row['ddr_fbga']}"
            )
        print("\nCurrent firmware:")
        for row in current:
            print(
                f"  {row['board_id']:>2}  tool={row['tool']:<6}  {row['product_name']:<4}  "
                f"fw={row['firmware']:<24}  fpga={row['fpga']}  date={row['event_date']}"
            )
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Import firmware_database.xlsm into SQLite")
    parser.add_argument("--xlsm", default=DEFAULT_XLSM, help="Source .xlsm path")
    parser.add_argument("--db", default=DEFAULT_DB, help="Output SQLite database path")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, help="schema.sql path")
    args = parser.parse_args()

    import_xlsm(abspath(args.xlsm), abspath(args.db), abspath(args.schema))
    print_summary(abspath(args.db))


if __name__ == "__main__":
    main()
