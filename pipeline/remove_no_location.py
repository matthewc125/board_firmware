#!/usr/bin/env python3
"""Remove board 47 and boards with no tool (location) from the curated database."""
from __future__ import annotations

import sqlite3
import sys
from os.path import abspath, dirname, join

SCRIPT_DIR = dirname(abspath(__file__))
PROJECT_DIR = dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_DIR)

from db import delete_board

DEFAULT_DB = join(PROJECT_DIR, "board_firmware.db")
EXPLICIT_REMOVE = {47}


def boards_without_location(db_path: str) -> list[int]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT board_id FROM boards
            WHERE tool IS NULL OR TRIM(tool) = ''
            ORDER BY board_id
            """
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def main(db_path: str = DEFAULT_DB) -> None:
    to_remove = sorted(EXPLICIT_REMOVE | set(boards_without_location(db_path)))
    print(f"Removing {len(to_remove)} board(s): {to_remove}")
    for board_id in to_remove:
        if delete_board(board_id):
            print(f"  removed board {board_id}")
        else:
            print(f"  board {board_id} not found")

    conn = sqlite3.connect(db_path)
    try:
        remaining = conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0]
    finally:
        conn.close()
    print(f"\n{remaining} board(s) remaining.")


if __name__ == "__main__":
    main()
