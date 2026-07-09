#!/usr/bin/env python3
"""Fix serial numbers and invalid tool values in board_firmware.db."""
from __future__ import annotations

import sqlite3
import sys
from os.path import abspath, dirname, join

SCRIPT_DIR = dirname(abspath(__file__))
PROJECT_DIR = dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from inventory_normalize import display_serial, normalize_tool

DEFAULT_DB = join(PROJECT_DIR, "board_firmware.db")

INVALID_TOOLS = {"?", "on desk", "on bench", "steve", "unknown", "n/a"}


def fix_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    serial_updates = 0
    tool_updates = 0

    for row in conn.execute("SELECT board_id, serial, inventory_serial, tool FROM boards").fetchall():
        board_id = row["board_id"]
        source = row["inventory_serial"] or row["serial"]
        new_serial = display_serial(source) or row["serial"]
        if new_serial and new_serial != row["serial"]:
            conn.execute("UPDATE boards SET serial = ? WHERE board_id = ?", (new_serial, board_id))
            serial_updates += 1

        tool = row["tool"]
        if tool is not None:
            cleaned = normalize_tool(tool)
            if cleaned != tool:
                conn.execute("UPDATE boards SET tool = ? WHERE board_id = ?", (cleaned, board_id))
                tool_updates += 1
            elif str(tool).strip().lower() in INVALID_TOOLS:
                conn.execute("UPDATE boards SET tool = NULL WHERE board_id = ?", (board_id,))
                tool_updates += 1

    conn.commit()
    conn.close()
    print(f"Updated {serial_updates} serial(s), {tool_updates} tool value(s) in {db_path}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    fix_db(abspath(path))
