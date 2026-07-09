#!/usr/bin/env python3
"""Remove boards by firmware version and renumber remaining boards sequentially."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from os.path import abspath, dirname, join

PROJECT_DIR = dirname(dirname(abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from db import delete_board, renumber_boards

DEFAULT_DB = join(PROJECT_DIR, "board_firmware.db")
DEFAULT_FIRMWARE = ("A191004A", "adc_board_v7", "20V")


def remove_boards_with_firmware(db_path: str, firmware_versions: tuple[str, ...]) -> list[int]:
    conn = sqlite3.connect(db_path)
    try:
        placeholders = ",".join("?" for _ in firmware_versions)
        board_ids = [
            row[0]
            for row in conn.execute(
                f"""
                SELECT DISTINCT board_id
                FROM firmware_history
                WHERE firmware IN ({placeholders})
                ORDER BY board_id
                """,
                firmware_versions,
            ).fetchall()
        ]
    finally:
        conn.close()

    removed: list[int] = []
    for board_id in board_ids:
        if delete_board(board_id):
            removed.append(board_id)
    return removed


def main():
    parser = argparse.ArgumentParser(description="Remove boards by firmware and renumber IDs")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument(
        "--firmware",
        nargs="+",
        default=list(DEFAULT_FIRMWARE),
        help="Firmware version(s) — boards with any match in history are removed",
    )
    args = parser.parse_args()

    removed = remove_boards_with_firmware(abspath(args.db), tuple(args.firmware))
    print(f"Removed {len(removed)} board(s) with firmware in {list(args.firmware)}")
    for board_id in removed:
        print(f"  - board {board_id}")

    result = renumber_boards()
    print(f"Renumbered {result['renumbered']} board(s) to sequential IDs 1–{result['renumbered']}")

    conn = sqlite3.connect(abspath(args.db))
    try:
        remaining = conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0]
        ids = [row[0] for row in conn.execute("SELECT board_id FROM boards ORDER BY board_id").fetchall()]
    finally:
        conn.close()
    print(f"{remaining} board(s) remaining: {ids[0]}–{ids[-1]}" if ids else "0 boards remaining")


if __name__ == "__main__":
    main()
