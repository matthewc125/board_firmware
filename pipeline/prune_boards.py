#!/usr/bin/env python3
"""
Remove boards that should not appear in the firmware log.

Excludes boards with any of:
  - strikethrough in source inventory spreadsheets
  - status Scrapped
  - no firmware version in firmware_history

Usage:
  py -3 pipeline/prune_boards.py
  py -3 pipeline/prune_boards.py --dry-run
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from os.path import abspath, dirname, join

SCRIPT_DIR = dirname(abspath(__file__))
PROJECT_DIR = dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_DIR)

from db import delete_board
from inventory_normalize import board_removal_reasons, load_strikethrough_keys

DEFAULT_DB = join(PROJECT_DIR, "board_firmware.db")
DEFAULT_ELECTRONICS = join(SCRIPT_DIR, "Electronics_Column_ Tracking.xlsx")
DEFAULT_PICO = join(SCRIPT_DIR, "2025-11-21_Picoammeter_Board_List.xlsx")


def prune_boards(
    db_path: str,
    electronics_path: str,
    pico_path: str,
    dry_run: bool = False,
) -> dict[str, int]:
    strike_keys = load_strikethrough_keys(electronics_path, pico_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        boards = conn.execute("SELECT * FROM boards ORDER BY board_id").fetchall()

        to_remove: list[tuple[int, str, list[str]]] = []
        reason_counts: dict[str, int] = {}

        for board in boards:
            board_dict = dict(board)
            reasons = board_removal_reasons(conn, board_dict, strike_keys)
            if not reasons:
                continue
            label = (
                f"board {board_dict['board_id']} "
                f"{board_dict.get('board_name')} {board_dict.get('serial')} "
                f"({board_dict.get('inventory_serial') or 'no inv serial'})"
            )
            to_remove.append((board_dict["board_id"], label, reasons))
            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
    finally:
        conn.close()

    kept = len(boards) - len(to_remove)
    print(f"Strikethrough keys loaded: {len(strike_keys)}")
    print(f"Boards before: {len(boards)}")
    print(f"Boards to remove: {len(to_remove)}")
    print(f"Boards to keep: {kept}")
    if reason_counts:
        print("Removal reasons (boards may match multiple):")
        for reason, count in sorted(reason_counts.items()):
            print(f"  {reason}: {count}")

    if dry_run:
        print("\nDry run — sample removals:")
        for _, label, reasons in to_remove[:30]:
            print(f"  - {label}: {', '.join(reasons)}")
        if len(to_remove) > 30:
            print(f"  ... and {len(to_remove) - 30} more")
        return {"removed": 0, "kept": kept, "candidates": len(to_remove)}

    removed = 0
    for board_id, label, reasons in to_remove:
        delete_board(board_id)
        removed += 1
        print(f"Removed {label}: {', '.join(reasons)}")

    print(f"\nRemoved {removed} board(s); {kept} remaining.")
    return {"removed": removed, "kept": kept, "candidates": len(to_remove)}


def main():
    parser = argparse.ArgumentParser(description="Remove excluded inventory boards from board_firmware.db")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--electronics", default=DEFAULT_ELECTRONICS, help="Electronics tracking xlsx")
    parser.add_argument("--pico", default=DEFAULT_PICO, help="Picoammeter board list xlsx")
    parser.add_argument("--dry-run", action="store_true", help="Report removals without writing")
    args = parser.parse_args()

    prune_boards(
        abspath(args.db),
        abspath(args.electronics),
        abspath(args.pico),
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
