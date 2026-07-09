#!/usr/bin/env python3
"""Restore deleted boards that still pass current exclusion rules."""
from __future__ import annotations

import sqlite3
import sys
from os.path import abspath, dirname, join

SCRIPT_DIR = dirname(abspath(__file__))
PROJECT_DIR = dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_DIR)

from db import restore_board
from inventory_normalize import (
    board_matches_strikethrough,
    is_scrapped_status,
    load_strikethrough_keys,
)

DEFAULT_DB = join(PROJECT_DIR, "board_firmware.db")
DEFAULT_ELECTRONICS = join(SCRIPT_DIR, "Electronics_Column_ Tracking.xlsx")
DEFAULT_PICO = join(SCRIPT_DIR, "2025-11-21_Picoammeter_Board_List.xlsx")


def should_restore(board: dict, fw_count: int, strike_keys: set[str]) -> bool:
    if board_matches_strikethrough(board.get("inventory_serial"), strike_keys):
        return False
    if is_scrapped_status(board.get("status")):
        return False
    if fw_count == 0:
        return False
    return True


def main():
    strike_keys = load_strikethrough_keys(DEFAULT_ELECTRONICS, DEFAULT_PICO)
    conn = sqlite3.connect(DEFAULT_DB)
    conn.row_factory = sqlite3.Row
    try:
        deleted = conn.execute("SELECT * FROM deleted_boards ORDER BY board_id").fetchall()
        existing = {
            row[0]
            for row in conn.execute("SELECT board_id FROM boards").fetchall()
        }
        restored = 0
        for board in deleted:
            board_id = board["board_id"]
            if board_id in existing:
                continue
            board_dict = dict(board)
            fw_count = conn.execute(
                "SELECT COUNT(*) FROM deleted_firmware_history WHERE board_id = ?",
                (board_id,),
            ).fetchone()[0]
            if not should_restore(board_dict, fw_count, strike_keys):
                continue
            if restore_board(board_id):
                restored += 1
                print(
                    f"Restored board {board_id} {board_dict.get('board_name')} "
                    f"{board_dict.get('serial')} (tool={board_dict.get('tool')!r})"
                )
    finally:
        conn.close()

    remaining = sqlite3.connect(DEFAULT_DB).execute("SELECT COUNT(*) FROM boards").fetchone()[0]
    print(f"\nRestored {restored} board(s); {remaining} total in database.")


if __name__ == "__main__":
    main()
