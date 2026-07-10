#!/usr/bin/env python3
"""Restore boards removed for missing location (IDs 36-46)."""
from __future__ import annotations

import sqlite3
import sys
from os.path import abspath, dirname, join

SCRIPT_DIR = dirname(abspath(__file__))
PROJECT_DIR = dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_DIR)

from db import restore_board
from update_es4_firmware import main as update_es4_firmware

DEFAULT_DB = join(PROJECT_DIR, "board_firmware.db")
RESTORE_BOARD_IDS = list(range(36, 47))  # 36-46, no location; excludes false-positive 47


def main(db_path: str = DEFAULT_DB) -> None:
    conn = sqlite3.connect(db_path)
    try:
        existing = {
            row[0] for row in conn.execute("SELECT board_id FROM boards").fetchall()
        }
    finally:
        conn.close()

    restored = 0
    for board_id in RESTORE_BOARD_IDS:
        if board_id in existing:
            print(f"  skip board {board_id} — already active")
            continue
        if restore_board(board_id):
            restored += 1
            print(f"  restored board {board_id}")
        else:
            print(f"  board {board_id} not found in deleted_boards")

    print(f"\nRestored {restored} board(s).")
    print("\nApplying ES4 imported firmware corrections...")
    update_es4_firmware()

    remaining = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM boards").fetchone()[0]
    print(f"\n{remaining} board(s) in database.")


if __name__ == "__main__":
    main()
