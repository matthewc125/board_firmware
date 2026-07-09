#!/usr/bin/env python3
"""
Merge inventory spreadsheets into board_firmware.db.

Usage:
  py -3 pipeline/import_inventory.py
  py -3 pipeline/import_inventory.py --dry-run
  py -3 pipeline/import_inventory.py --electronics path/to/tracking.xlsx --pico path/to/pico.xlsx
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from os.path import abspath, dirname, join

import pandas as pd

from inventory_normalize import (
    MANUFACTURER,
    clean_text,
    etr_row_excluded,
    format_revision,
    load_strikethrough_keys,
    map_etr_type,
    map_pico_board,
    normalize_inventory_serial,
    normalize_tool,
    parse_part_revision,
    parse_pico_status_events,
    pico_match_key,
    pico_row_excluded,
    short_serial,
    split_datetime,
    split_tool_history,
    strong_match_key,
)

SCRIPT_DIR = dirname(abspath(__file__))
PROJECT_DIR = dirname(SCRIPT_DIR)
DEFAULT_DB = join(PROJECT_DIR, "board_firmware.db")
DEFAULT_ELECTRONICS = join(SCRIPT_DIR, "Electronics_Column_ Tracking.xlsx")
DEFAULT_PICO = join(SCRIPT_DIR, "2025-11-21_Picoammeter_Board_List.xlsx")

BOARD_COLS = [
    "board_id", "tool", "board_slot", "manufacturer", "board_name",
    "serial", "part_number", "revision", "file_id", "product_name", "ddr_fbga",
    "inventory_serial", "status", "role", "comment", "open_item", "po",
    "modified_by", "source_updated_at", "data_source",
    "dc_status", "ac_status", "gcal_status", "adc_status", "eeprom_status",
]


class ImportReport:
    def __init__(self):
        self.created: list[str] = []
        self.updated: list[str] = []
        self.merged: list[str] = []
        self.skipped: list[str] = []
        self.ambiguous: list[str] = []
        self.events_created = 0
        self.firmware_created = 0
        self.warnings: list[str] = []

    def summary(self) -> dict:
        return {
            "created": len(self.created),
            "updated": len(self.updated),
            "merged": len(self.merged),
            "skipped": len(self.skipped),
            "ambiguous": len(self.ambiguous),
            "events_created": self.events_created,
            "firmware_created": self.firmware_created,
            "warnings": self.warnings,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load_electronics(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=0)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def load_pico(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=0)
    df.columns = [
        "Board", "PN", "SN", "Status", "Tool", "DC", "AC", "GCAL", "ADC", "EEPROM", "Notes",
    ]
    df["Board"] = df["Board"].ffill()
    df["PN"] = df["PN"].ffill()
    return df[df["SN"].notna()].reset_index(drop=True)


def next_board_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(board_id), 0) + 1 AS next_id
        FROM (
            SELECT board_id FROM boards
            UNION ALL
            SELECT board_id FROM deleted_boards
        )
        """
    ).fetchone()
    return row[0]


def load_board_indexes(conn: sqlite3.Connection) -> tuple[dict, dict, dict]:
  by_inventory = {}
  by_strong = {}
  by_pico = {}
  for row in conn.execute("SELECT * FROM boards").fetchall():
      board = dict(row)
      bid = board["board_id"]
      inv = normalize_inventory_serial(board.get("inventory_serial"))
      if inv:
          by_inventory[inv] = bid
      sk = strong_match_key(board.get("part_number"), board.get("inventory_serial"))
      if sk:
          by_strong[sk] = bid
      pk = pico_match_key(board.get("part_number"), board.get("serial"))
      if pk:
          by_pico.setdefault(pk, []).append(bid)
  return by_inventory, by_strong, by_pico


def upsert_board(conn, board_id: int | None, data: dict, report: ImportReport, label: str):
    if board_id is None:
        board_id = next_board_id(conn)
        data["board_id"] = board_id
        cols = [c for c in BOARD_COLS if c in data]
        conn.execute(
            f"INSERT INTO boards ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [data[c] for c in cols],
        )
        report.created.append(f"{label} -> board_id {board_id}")
        return board_id

    existing = dict(conn.execute("SELECT * FROM boards WHERE board_id = ?", (board_id,)).fetchone())
    merged = {**existing, **{k: v for k, v in data.items() if v is not None}}
    merged["board_id"] = board_id
    if existing.get("data_source") and data.get("data_source") and existing["data_source"] != data["data_source"]:
        merged["data_source"] = "merged"
    cols = [c for c in BOARD_COLS if c != "board_id"]
    conn.execute(
        f"UPDATE boards SET {', '.join(f'{c}=?' for c in cols)} WHERE board_id=?",
        [merged.get(c) for c in cols] + [board_id],
    )
    if existing.get("data_source") == data.get("data_source"):
        report.updated.append(f"{label} -> board_id {board_id}")
    else:
        report.merged.append(f"{label} -> board_id {board_id}")
    return board_id


def firmware_exists(conn, board_id: int, firmware: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM firmware_history WHERE board_id=? AND firmware=? LIMIT 1",
        (board_id, firmware),
    ).fetchone()
    return row is not None


def insert_firmware_from_etr(conn, board_id: int, firmware: str, event_date: str | None, installer: str | None, report: ImportReport):
    if firmware_exists(conn, board_id, firmware):
        return
    conn.execute(
        """
        INSERT INTO firmware_history (board_id, event_date, event_time, fpga, firmware, installer, result)
        VALUES (?, ?, NULL, NULL, ?, ?, 'PASS')
        """,
        (board_id, event_date or utc_now()[:10], firmware, installer or "inventory"),
    )
    report.firmware_created += 1


def insert_board_events(conn, board_id: int, events: list[dict], report: ImportReport):
    for ev in events:
        exists = conn.execute(
            """
            SELECT 1 FROM board_events
            WHERE board_id=? AND event_date=? AND description=? AND source=?
            LIMIT 1
            """,
            (board_id, ev["event_date"], ev["description"], ev["source"]),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            INSERT INTO board_events (board_id, event_date, event_time, event_type, description, tool, source, source_ref)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                board_id,
                ev["event_date"],
                ev.get("event_time"),
                ev["event_type"],
                ev["description"],
                ev.get("tool"),
                ev["source"],
                ev.get("source_ref"),
            ),
        )
        report.events_created += 1


def etr_row_to_board(row: pd.Series, row_idx: int) -> dict:
    etr_type = clean_text(row.get("Type")) or "Unknown"
    role = clean_text(row.get("Role"))
    product_name, board_name, board_slot = map_etr_type(etr_type, role)
    raw_serial = clean_text(row.get("SN"))
    inventory_serial = f"{etr_type}:{raw_serial}" if raw_serial else None
    part_number = None
    revision = format_revision(row.get("Rev"))
    event_date, _ = split_datetime(row.get("Date Updated"))

    return {
        "tool": normalize_tool(row.get("ID(Tool)")),
        "board_slot": clean_text(row.get("SlotID")) or board_slot,
        "manufacturer": MANUFACTURER,
        "board_name": board_name,
        "serial": short_serial(raw_serial) or (raw_serial or f"etr-{row_idx}"),
        "part_number": part_number,
        "revision": revision,
        "file_id": None,
        "product_name": product_name,
        "ddr_fbga": None,
        "inventory_serial": inventory_serial,
        "status": clean_text(row.get("Status")),
        "role": role,
        "comment": clean_text(row.get("Comment")),
        "open_item": clean_text(row.get("Open Item")),
        "po": clean_text(row.get("PO")),
        "modified_by": clean_text(row.get("Modified By")),
        "source_updated_at": event_date,
        "data_source": "electronics_tracking",
        "dc_status": None,
        "ac_status": None,
        "gcal_status": None,
        "adc_status": None,
        "eeprom_status": None,
        "_firmware": clean_text(row.get("Firmware")),
        "_row_idx": row_idx,
    }


def pico_row_to_board(row: pd.Series, row_idx: int) -> dict:
    board_family = clean_text(row.get("Board")) or "Unknown"
    product_name, board_name = map_pico_board(board_family)
    part_number, pn_rev = parse_part_revision(row.get("PN"))
    revision = pn_rev or None
    inv_serial = clean_text(row.get("SN"))
    tool_history, current_tool = split_tool_history(row.get("Tool"))

    return {
        "tool": current_tool,
        "board_slot": "Bench",
        "manufacturer": MANUFACTURER,
        "board_name": board_name,
        "serial": short_serial(inv_serial) or (inv_serial or f"pico-{row_idx}"),
        "part_number": part_number,
        "revision": revision,
        "file_id": None,
        "product_name": product_name,
        "ddr_fbga": None,
        "inventory_serial": inv_serial,
        "status": "Active" if current_tool else None,
        "role": None,
        "comment": clean_text(row.get("Notes")),
        "open_item": None,
        "po": None,
        "modified_by": None,
        "source_updated_at": None,
        "data_source": "pico_list",
        "dc_status": clean_text(row.get("DC")),
        "ac_status": clean_text(row.get("AC")),
        "gcal_status": clean_text(row.get("GCAL")),
        "adc_status": clean_text(row.get("ADC")),
        "eeprom_status": clean_text(row.get("EEPROM")),
        "_status_text": row.get("Status"),
        "_tool_history": tool_history,
        "_row_idx": row_idx,
        "_board_family": board_family,
    }


def find_etr_board_id(data: dict, by_inventory: dict, by_strong: dict) -> int | None:
    inv = normalize_inventory_serial(data.get("inventory_serial"))
    if inv and inv in by_inventory:
        return by_inventory[inv]
    sk = strong_match_key(data.get("part_number"), data.get("inventory_serial"))
    if sk and sk in by_strong:
        return by_strong[sk]
    return None


def find_pico_board_id(data: dict, by_inventory: dict, by_pico: dict) -> tuple[int | None, bool]:
    inv = normalize_inventory_serial(data.get("inventory_serial"))
    if inv and inv in by_inventory:
        return by_inventory[inv], False
    pk = pico_match_key(data.get("part_number"), data.get("inventory_serial"))
    if pk and pk in by_pico:
        matches = by_pico[pk]
        if len(matches) == 1:
            return matches[0], False
        return None, True
    return None, False


def board_has_firmware(conn, board_id: int | None) -> bool:
    if not board_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM firmware_history WHERE board_id = ? LIMIT 1",
        (board_id,),
    ).fetchone()
    return row is not None


def import_electronics(
    conn, path: str, report: ImportReport, dry_run: bool, strike_keys: set[str],
) -> None:
    df = load_electronics(path)
    by_inventory, by_strong, by_pico = load_board_indexes(conn)

    for idx, row in df.iterrows():
        data = etr_row_to_board(row, idx + 2)
        label = f"ETR row {idx + 2} {data['inventory_serial']}"
        skip = etr_row_excluded(row, strike_keys)
        if skip:
            report.skipped.append(f"{label}: {skip}")
            continue
        inv = normalize_inventory_serial(data["inventory_serial"])
        if not inv:
            report.skipped.append(f"{label}: missing serial")
            continue

        board_id = find_etr_board_id(data, by_inventory, by_strong)
        firmware = data.pop("_firmware")
        data.pop("_row_idx")

        if dry_run:
            action = "update" if board_id else "create"
            report.created.append(f"[dry-run {action}] {label}")
            continue

        board_id = upsert_board(conn, board_id, data, report, label)
        if inv:
            by_inventory[inv] = board_id
        sk = strong_match_key(data.get("part_number"), data.get("inventory_serial"))
        if sk:
            by_strong[sk] = board_id

        if firmware:
            insert_firmware_from_etr(
                conn,
                board_id,
                firmware,
                data.get("source_updated_at"),
                data.get("modified_by"),
                report,
            )


def import_pico(
    conn, path: str, report: ImportReport, dry_run: bool, strike_keys: set[str],
) -> None:
    df = load_pico(path)
    by_inventory, by_strong, by_pico = load_board_indexes(conn)

    for idx, row in df.iterrows():
        data = pico_row_to_board(row, idx + 2)
        label = f"Pico row {idx + 2} {data['board_name']} {data['inventory_serial']}"
        skip = pico_row_excluded(row, strike_keys)
        if skip:
            report.skipped.append(f"{label}: {skip}")
            continue
        status_text = data.pop("_status_text")
        tool_history = data.pop("_tool_history")
        board_family = data.pop("_board_family")
        data.pop("_row_idx")

        board_id, ambiguous = find_pico_board_id(data, by_inventory, by_pico)
        if ambiguous:
            pk = pico_match_key(data.get("part_number"), data.get("inventory_serial"))
            report.ambiguous.append(f"{label}: multiple matches for key {pk}")
            continue
        if not board_has_firmware(conn, board_id):
            report.skipped.append(f"{label}: no_firmware")
            continue

        if dry_run:
            action = "update" if board_id else "create"
            report.created.append(f"[dry-run {action}] {label}")
            continue

        board_id = upsert_board(conn, board_id, data, report, label)
        inv = normalize_inventory_serial(data.get("inventory_serial"))
        if inv:
            by_inventory[inv] = board_id
        pk = pico_match_key(data.get("part_number"), data.get("inventory_serial"))
        if pk:
            by_pico.setdefault(pk, [])
            if board_id not in by_pico[pk]:
                by_pico[pk].append(board_id)

        events = parse_pico_status_events(status_text, board_family, data.get("inventory_serial") or "")
        for i, tool in enumerate(tool_history):
            events.append({
                "event_date": utc_now()[:10],
                "event_time": None,
                "event_type": "location",
                "description": f"Previously at {tool}",
                "tool": tool,
                "source": "pico_list",
                "source_ref": f"{board_family}:{data.get('inventory_serial')}:tool:{i}",
            })
        insert_board_events(conn, board_id, events, report)


def record_import_run(conn, source_name: str, file_path: str, rows_read: int, report: ImportReport):
    conn.execute(
        """
        INSERT INTO import_runs (
            source_name, imported_at, file_path, rows_read,
            boards_created, boards_updated, boards_merged, events_created, warnings
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_name,
            utc_now(),
            file_path,
            rows_read,
            len(report.created),
            len(report.updated),
            len(report.merged),
            report.events_created,
            json.dumps(report.warnings + report.ambiguous),
        ),
    )


def print_report(report: ImportReport, electronics_rows: int, pico_rows: int):
    summary = report.summary()
    print("Import complete")
    print(f"  Electronics rows read: {electronics_rows}")
    print(f"  Pico rows read:        {pico_rows}")
    for key, value in summary.items():
        if key != "warnings":
            print(f"  {key}: {value}")
    if report.skipped:
        print(f"\nSkipped ({len(report.skipped)}):")
        for line in report.skipped[:20]:
            print(f"  - {line}")
        if len(report.skipped) > 20:
            print(f"  ... and {len(report.skipped) - 20} more")
    if report.warnings:
        print("\nWarnings:")
        for w in report.warnings:
            print(f"  - {w}")
    if report.ambiguous:
        print("\nAmbiguous matches (manual review):")
        for a in report.ambiguous:
            print(f"  - {a}")
    if report.merged:
        print("\nMerged boards (sample):")
        for line in report.merged[:10]:
            print(f"  - {line}")
        if len(report.merged) > 10:
            print(f"  ... and {len(report.merged) - 10} more")


def import_inventory(
    db_path: str,
    electronics_path: str,
    pico_path: str,
    dry_run: bool = False,
) -> ImportReport:
    report = ImportReport()
    etr_df = load_electronics(electronics_path)
    pico_df = load_pico(pico_path)
    strike_keys = load_strikethrough_keys(electronics_path, pico_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        import_electronics(conn, electronics_path, report, dry_run, strike_keys)
        import_pico(conn, pico_path, report, dry_run, strike_keys)
        if not dry_run:
            record_import_run(conn, "electronics_tracking", electronics_path, len(etr_df), report)
            record_import_run(conn, "pico_list", pico_path, len(pico_df), report)
            conn.commit()
    finally:
        conn.close()

    print_report(report, len(etr_df), len(pico_df))
    return report


def main():
    parser = argparse.ArgumentParser(description="Import inventory spreadsheets into board_firmware.db")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--electronics", default=DEFAULT_ELECTRONICS, help="Electronics tracking xlsx")
    parser.add_argument("--pico", default=DEFAULT_PICO, help="Picoammeter board list xlsx")
    parser.add_argument("--dry-run", action="store_true", help="Report actions without writing")
    args = parser.parse_args()

    import_inventory(
        abspath(args.db),
        abspath(args.electronics),
        abspath(args.pico),
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
