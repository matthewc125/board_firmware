#!/usr/bin/env python3
"""
Import hardware tracking documents from pipeline/netapp_import into board_firmware.db.

Reads batch lists, tool assignments, repair logs, and inventory spreadsheets copied
from NetApp, then updates matching boards and appends board_events.

Usage:
  py -3 pipeline/import_netapp.py --dry-run
  py -3 pipeline/import_netapp.py
  py -3 pipeline/import_netapp.py --create-missing
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from os.path import abspath, dirname, join
from pathlib import Path

from inventory_normalize import MANUFACTURER, clean_text, normalize_inventory_serial, short_serial
from netapp_parsers import (
    BoardEvent,
    BoardPatch,
    inventory_serial_for,
    iter_import_files,
    normalize_doc_serial,
    parse_file,
    product_context,
    serial_match_keys,
)

SCRIPT_DIR = dirname(abspath(__file__))
PROJECT_DIR = dirname(SCRIPT_DIR)
DEFAULT_DB = join(PROJECT_DIR, "board_firmware.db")
DEFAULT_IMPORT_ROOT = join(SCRIPT_DIR, "netapp_import")

BOARD_COLS = [
    "board_id", "tool", "board_slot", "manufacturer", "board_name",
    "serial", "part_number", "revision", "file_id", "product_name", "ddr_fbga",
    "inventory_serial", "status", "role", "comment", "open_item", "po",
    "modified_by", "source_updated_at", "data_source",
    "dc_status", "ac_status", "gcal_status", "adc_status", "eeprom_status",
]


class NetappImportReport:
    def __init__(self):
        self.files_parsed = 0
        self.patches_applied = 0
        self.events_created = 0
        self.boards_created = 0
        self.unmatched_serials: list[str] = []
        self.warnings: list[str] = []
        self.skipped_files: list[str] = []

    def summary(self) -> dict:
        return {
            "files_parsed": self.files_parsed,
            "patches_applied": self.patches_applied,
            "events_created": self.events_created,
            "boards_created": self.boards_created,
            "unmatched_serials": len(self.unmatched_serials),
            "warnings": self.warnings,
            "skipped_files": self.skipped_files,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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


PRODUCT_MATCH = {
    "EM1": {"EM1"},
    "ADC": {"ADC"},
    "OBJ": {"OBJ"},
    "BAP": {"BAP"},
    "Column": {"Column"},
    "Pico": {"Pico"},
    "ES4": {"ES4"},
}


def build_serial_index(conn: sqlite3.Connection) -> tuple[dict[str, list[int]], dict[int, str], dict[str, int]]:
    index: dict[str, list[int]] = {}
    exact: dict[str, int] = {}
    products: dict[int, str] = {}
    for row in conn.execute(
        "SELECT board_id, product_name, serial, inventory_serial FROM boards"
    ):
        board_id = row["board_id"]
        products[board_id] = row["product_name"]
        for value in (row["inventory_serial"], row["serial"]):
            for variant in serial_lookup_variants(value):
                if variant not in exact:
                    exact[variant] = board_id
            exact_serial = normalize_doc_serial(value)
            if exact_serial and exact_serial not in exact:
                exact[exact_serial] = board_id
            if value and ":" in str(value):
                suffix = normalize_doc_serial(str(value).split(":", 1)[1])
                if suffix and suffix not in exact:
                    exact[suffix] = board_id
            for key in serial_match_keys(value):
                index.setdefault(key, [])
                if board_id not in index[key]:
                    index[key].append(board_id)
            inv = normalize_inventory_serial(value)
            if inv:
                index.setdefault(inv, [])
                if board_id not in index[inv]:
                    index[inv].append(board_id)
    return index, products, exact


def serial_lookup_variants(serial: str) -> list[str]:
    variants: list[str] = []
    doc = normalize_doc_serial(serial)
    if doc:
        variants.append(doc)
    if not doc or not doc.startswith("SN"):
        return variants
    body = doc[2:]
    m = re.match(r"^([A-Z]*)(\d+)([A-Z]*)$", body)
    if not m:
        return variants
    letters, number, suffix = m.group(1), int(m.group(2)), m.group(3)
    for width in (3, 4):
        variants.append(f"SN{letters}{number:0{width}d}{suffix}")
    return variants


def resolve_board_id(
    serial: str,
    index: dict[str, list[int]],
    products: dict[int, str],
    exact: dict[str, int],
    product_hint: str | None = None,
) -> int | None:
    for variant in serial_lookup_variants(serial):
        if variant in exact:
            return exact[variant]
    candidates: list[int] = []
    for key in serial_match_keys(serial):
        for board_id in index.get(key, []):
            if board_id not in candidates:
                candidates.append(board_id)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if product_hint:
        allowed = PRODUCT_MATCH.get(product_hint, {product_hint})
        filtered = [bid for bid in candidates if products.get(bid) in allowed]
        if len(filtered) == 1:
            return filtered[0]
        if filtered:
            candidates = filtered
    return candidates[0]


def merge_patch(existing: dict, patch: BoardPatch) -> dict:
    updates: dict = {}
    field_map = {
        "tool": patch.tool,
        "board_slot": patch.board_slot,
        "part_number": patch.part_number,
        "revision": patch.revision,
        "status": patch.status,
        "role": patch.role,
        "comment": patch.comment,
        "product_name": patch.product_name,
        "board_name": patch.board_name,
    }
    for field, value in field_map.items():
        if value is None:
            continue
        if field == "comment":
            existing_comment = clean_text(existing.get("comment"))
            if existing_comment and value not in existing_comment:
                updates["comment"] = f"{existing_comment} | {value}"
            elif not existing_comment:
                updates["comment"] = value
            continue
        if clean_text(existing.get(field)) != value:
            updates[field] = value
    return updates


def create_board_from_patch(
    conn: sqlite3.Connection,
    patch: BoardPatch,
    create_missing: bool,
    dry_run: bool = False,
    dry_run_id: int = 900_000,
) -> tuple[int | None, dict | None]:
    if not create_missing:
        return None, None
    product_name = patch.product_name or "Unknown"
    board_name = patch.board_name or product_name
    inv_serial = inventory_serial_for(product_name, patch.serial)
    existing = conn.execute(
        "SELECT board_id FROM boards WHERE inventory_serial = ?",
        (inv_serial,),
    ).fetchone()
    if existing:
        return existing["board_id"], None
    board_id = dry_run_id if dry_run else next_board_id(conn)
    data = {
        "board_id": board_id,
        "tool": patch.tool,
        "board_slot": patch.board_slot,
        "manufacturer": MANUFACTURER,
        "board_name": board_name,
        "serial": short_serial(patch.serial),
        "part_number": patch.part_number,
        "revision": patch.revision,
        "file_id": None,
        "product_name": product_name,
        "ddr_fbga": None,
        "inventory_serial": inv_serial,
        "status": patch.status,
        "role": patch.role,
        "comment": patch.comment,
        "open_item": None,
        "po": None,
        "modified_by": None,
        "source_updated_at": utc_now()[:10],
        "data_source": "netapp_import",
        "dc_status": None,
        "ac_status": None,
        "gcal_status": None,
        "adc_status": None,
        "eeprom_status": None,
    }
    if dry_run:
        return board_id, data
    cols = [c for c in BOARD_COLS if c in data]
    conn.execute(
        f"INSERT INTO boards ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [data[c] for c in cols],
    )
    return board_id, data


def event_exists(
    conn: sqlite3.Connection,
    board_id: int,
    description: str,
    source_ref: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM board_events
        WHERE board_id = ? AND source = 'netapp_import'
          AND (source_ref = ? OR description = ?)
        LIMIT 1
        """,
        (board_id, source_ref, description),
    ).fetchone()
    return row is not None


def insert_event(
    conn: sqlite3.Connection,
    board_id: int,
    event: BoardEvent,
    report: NetappImportReport,
    dry_run: bool,
) -> None:
    if event_exists(conn, board_id, event.description, event.source_ref):
        return
    if dry_run:
        report.events_created += 1
        return
    conn.execute(
        """
        INSERT INTO board_events (
            board_id, event_date, event_time, event_type, description, tool, source, source_ref
        ) VALUES (?, ?, ?, ?, ?, ?, 'netapp_import', ?)
        """,
        (
            board_id,
            event.event_date,
            event.event_time,
            event.event_type,
            event.description,
            event.tool,
            event.source_ref,
        ),
    )
    report.events_created += 1


def apply_patch(
    conn: sqlite3.Connection,
    board_id: int,
    patch: BoardPatch,
    report: NetappImportReport,
    dry_run: bool,
) -> None:
    existing = dict(conn.execute("SELECT * FROM boards WHERE board_id = ?", (board_id,)).fetchone())
    updates = merge_patch(existing, patch)
    if not updates:
        return
    if existing.get("data_source") and existing.get("data_source") != "netapp_import":
        updates["data_source"] = "merged"
    else:
        updates["data_source"] = existing.get("data_source") or "netapp_import"
    if dry_run:
        report.patches_applied += 1
        return
    sets = ", ".join(f"{col} = ?" for col in updates)
    conn.execute(
        f"UPDATE boards SET {sets} WHERE board_id = ?",
        [*updates.values(), board_id],
    )
    report.patches_applied += 1


def ensure_board_id(
    conn: sqlite3.Connection,
    serial: str,
    patch: BoardPatch | None,
    index: dict[str, list[int]],
    products: dict[int, str],
    exact: dict[str, int],
    create_missing: bool,
    report: NetappImportReport,
    dry_run: bool,
    dry_run_id: int,
) -> tuple[int | None, int]:
    product_hint = patch.product_name if patch else None
    board_id = resolve_board_id(serial, index, products, exact, product_hint=product_hint)
    if board_id is not None:
        return board_id, dry_run_id
    if patch is None:
        report.unmatched_serials.append(serial)
        return None, dry_run_id
    board_id, data = create_board_from_patch(
        conn, patch, create_missing=create_missing, dry_run=dry_run, dry_run_id=dry_run_id
    )
    if board_id is None:
        report.unmatched_serials.append(serial)
        return None, dry_run_id
    if dry_run and board_id >= 900_000:
        dry_run_id = board_id + 1
    for key in serial_match_keys(serial):
        index.setdefault(key, [])
        if board_id not in index[key]:
            index[key].append(board_id)
    if data and data.get("inventory_serial"):
        inv = normalize_inventory_serial(data["inventory_serial"])
        if inv:
            index.setdefault(inv, [])
            if board_id not in index[inv]:
                index[inv].append(board_id)
    report.boards_created += 1
    return board_id, dry_run_id


def import_netapp_tree(
    db_path: str,
    import_root: str,
    dry_run: bool = False,
    create_missing: bool = False,
) -> NetappImportReport:
    report = NetappImportReport()
    root = Path(import_root)
    if not root.is_dir():
        raise FileNotFoundError(f"Import folder not found: {root}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    dry_run_id = 900_000
    try:
        index, products, exact = build_serial_index(conn)
        files = iter_import_files(root)
        for path in files:
            parsed = parse_file(path)
            report.files_parsed += 1
            report.warnings.extend(parsed.warnings)
            if parsed.warnings and not parsed.patches and not parsed.events:
                report.skipped_files.append(str(path.relative_to(root)))

            patch_by_serial: dict[str, BoardPatch] = {}
            for patch in parsed.patches:
                patch_by_serial[patch.serial] = patch

            for patch in parsed.patches:
                board_id, dry_run_id = ensure_board_id(
                    conn,
                    patch.serial,
                    patch,
                    index,
                    products,
                    exact,
                    create_missing,
                    report,
                    dry_run,
                    dry_run_id,
                )
                if board_id is None:
                    continue
                if board_id < 900_000:
                    apply_patch(conn, board_id, patch, report, dry_run)
                elif dry_run:
                    report.patches_applied += 1

            for event in parsed.events:
                patch = patch_by_serial.get(event.serial)
                if patch is None:
                    product_name, board_name, part_number = product_context(Path(parsed.path))
                    patch = BoardPatch(
                        serial=event.serial,
                        product_name=product_name,
                        board_name=board_name,
                        part_number=part_number,
                        source_ref=parsed.path,
                    )
                board_id, dry_run_id = ensure_board_id(
                    conn,
                    event.serial,
                    patch,
                    index,
                    products,
                    exact,
                    create_missing,
                    report,
                    dry_run,
                    dry_run_id,
                )
                if board_id is None:
                    continue
                insert_event(conn, board_id, event, report, dry_run)

        if not dry_run:
            conn.execute(
                """
                INSERT INTO import_runs (
                    source_name, imported_at, file_path, rows_read,
                    boards_created, boards_updated, boards_merged, events_created, warnings
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "netapp_import",
                    utc_now(),
                    str(root),
                    report.files_parsed,
                    report.boards_created,
                    report.patches_applied,
                    0,
                    report.events_created,
                    json.dumps(report.warnings + report.unmatched_serials[:50]),
                ),
            )
            conn.commit()
    finally:
        conn.close()

    return report


def print_report(report: NetappImportReport) -> None:
    print("NetApp import complete")
    for key, value in report.summary().items():
        if key not in ("warnings", "skipped_files", "unmatched_serials"):
            print(f"  {key}: {value}")
    if report.unmatched_serials:
        unique = sorted(set(report.unmatched_serials))
        print(f"\nUnmatched serials ({len(unique)}):")
        for serial in unique[:30]:
            print(f"  - {serial}")
        if len(unique) > 30:
            print(f"  ... and {len(unique) - 30} more")
        print("  Tip: re-run with --create-missing to add inventory-only boards.")
    if report.skipped_files:
        print(f"\nSkipped files ({len(report.skipped_files)}):")
        for path in report.skipped_files[:10]:
            print(f"  - {path}")
    if report.warnings:
        print(f"\nWarnings ({len(report.warnings)}):")
        for warning in report.warnings[:15]:
            print(f"  - {warning}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import NetApp hardware tracking docs into SQLite")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument(
        "--import-root",
        default=DEFAULT_IMPORT_ROOT,
        help="Folder containing copied NetApp documents",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument(
        "--create-missing",
        action="store_true",
        help="Create boards for serials not already in the database",
    )
    args = parser.parse_args()

    report = import_netapp_tree(
        abspath(args.db),
        abspath(args.import_root),
        dry_run=args.dry_run,
        create_missing=args.create_missing,
    )
    print_report(report)


if __name__ == "__main__":
    main()
