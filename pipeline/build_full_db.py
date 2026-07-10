#!/usr/bin/env python3
"""
Build board_firmware_full.db — side archive only; never replaces board_firmware.db.

Copies the live curated database, restores every deleted board, re-imports
inventory spreadsheets without exclusion rules, and merges NetApp documents.
The main app always uses board_firmware.db (51 tracked boards).

Usage:
  py -3 pipeline/build_full_db.py
  py -3 pipeline/build_full_db.py --output path/to/archive.db
  py -3 pipeline/build_full_db.py --skip-netapp
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from os.path import abspath, dirname, join

SCRIPT_DIR = dirname(abspath(__file__))
PROJECT_DIR = dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_DIR)

from db import BOARD_COLUMNS, HISTORY_COLUMNS
from import_inventory import import_inventory
from import_netapp import import_netapp_tree

DEFAULT_SOURCE_DB = join(PROJECT_DIR, "board_firmware.db")
DEFAULT_OUTPUT_DB = join(PROJECT_DIR, "board_firmware_full.db")
DEFAULT_ELECTRONICS = join(SCRIPT_DIR, "sources", "Electronics_Column_ Tracking.xlsx")
DEFAULT_PICO = join(SCRIPT_DIR, "sources", "2025-11-21_Picoammeter_Board_List.xlsx")
DEFAULT_NETAPP_ROOT = join(SCRIPT_DIR, "netapp_import")


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


def restore_all_deleted_boards(db_path: str) -> tuple[int, int]:
    """Move every archived board back into boards with its firmware history.

    Returns (restored_count, reassigned_id_count).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        deleted = conn.execute(
            "SELECT * FROM deleted_boards ORDER BY board_id"
        ).fetchall()
        existing = {
            row[0] for row in conn.execute("SELECT board_id FROM boards").fetchall()
        }
        restored = 0
        reassigned = 0
        for board in deleted:
            board_id = board["board_id"]
            target_id = board_id
            if target_id in existing:
                target_id = next_board_id(conn)
                while target_id in existing:
                    target_id += 1
                reassigned += 1

            board_dict = dict(board)
            board_dict["board_id"] = target_id
            conn.execute(
                f"INSERT INTO boards ({', '.join(BOARD_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in BOARD_COLUMNS)})",
                [board_dict[col] for col in BOARD_COLUMNS],
            )
            for event in conn.execute(
                "SELECT * FROM deleted_firmware_history WHERE board_id = ?",
                (board_id,),
            ):
                event_dict = dict(event)
                event_dict["board_id"] = target_id
                conn.execute(
                    f"INSERT INTO firmware_history ({', '.join(HISTORY_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in HISTORY_COLUMNS)})",
                    [event_dict[col] for col in HISTORY_COLUMNS],
                )
            conn.execute(
                "DELETE FROM deleted_firmware_history WHERE board_id = ?",
                (board_id,),
            )
            conn.execute("DELETE FROM deleted_boards WHERE board_id = ?", (board_id,))
            existing.add(target_id)
            restored += 1
        conn.commit()
        return restored, reassigned
    finally:
        conn.close()


def print_db_summary(db_path: str, label: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        boards = conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0]
        deleted = conn.execute("SELECT COUNT(*) FROM deleted_boards").fetchone()[0]
        fw = conn.execute("SELECT COUNT(*) FROM firmware_history").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM board_events").fetchone()[0]
        by_source = conn.execute(
            """
            SELECT COALESCE(data_source, '(none)') AS src, COUNT(*) AS n
            FROM boards GROUP BY data_source ORDER BY n DESC
            """
        ).fetchall()
        print(f"\n{label}: {db_path}")
        print(f"  boards:          {boards}")
        print(f"  deleted_boards:  {deleted}")
        print(f"  firmware_history:{fw}")
        print(f"  board_events:    {events}")
        print("  boards by data_source:")
        for src, count in by_source:
            print(f"    {src}: {count}")
    finally:
        conn.close()


def build_full_db(
    source_db: str,
    output_db: str,
    electronics_path: str,
    pico_path: str,
    netapp_root: str,
    skip_netapp: bool = False,
) -> None:
    print(f"Copying {source_db}")
    print(f"     -> {output_db}")
    shutil.copy2(source_db, output_db)

    restored, reassigned = restore_all_deleted_boards(output_db)
    print(f"\nRestored {restored} deleted board(s)")
    if reassigned:
        print(f"  ({reassigned} reassigned new board_id due to ID conflicts)")
    print_db_summary(output_db, "After restore")

    print("\n--- Inventory import (include all rows) ---")
    import_inventory(
        output_db,
        electronics_path,
        pico_path,
        dry_run=False,
        include_all=True,
    )

    if skip_netapp:
        print("\nSkipping NetApp import (--skip-netapp)")
    else:
        print("\n--- NetApp import (create missing boards) ---")
        import_netapp_tree(
            output_db,
            netapp_root,
            dry_run=False,
            create_missing=True,
        )

    print_db_summary(output_db, "Final archive")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build full archive database from all sources including pruned boards",
    )
    parser.add_argument("--source-db", default=DEFAULT_SOURCE_DB, help="Live database to copy")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_DB, help="Archive database path")
    parser.add_argument("--electronics", default=DEFAULT_ELECTRONICS)
    parser.add_argument("--pico", default=DEFAULT_PICO)
    parser.add_argument("--import-root", default=DEFAULT_NETAPP_ROOT)
    parser.add_argument(
        "--skip-netapp",
        action="store_true",
        help="Skip NetApp document import",
    )
    args = parser.parse_args()

    source_db = abspath(args.source_db)
    output_db = abspath(args.output)
    if source_db == output_db:
        parser.error("Source and output database paths must differ")

    build_full_db(
        source_db,
        output_db,
        abspath(args.electronics),
        abspath(args.pico),
        abspath(args.import_root),
        skip_netapp=args.skip_netapp,
    )


if __name__ == "__main__":
    main()
