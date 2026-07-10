#!/usr/bin/env python3
"""Set imported ES4 board firmware to 1.04.26 on the curated database."""
from __future__ import annotations

import sqlite3
from datetime import date
from os.path import abspath, dirname, join

SCRIPT_DIR = dirname(abspath(__file__))
PROJECT_DIR = dirname(SCRIPT_DIR)
DEFAULT_DB = join(PROJECT_DIR, "board_firmware.db")
ES4_FIRMWARE = "1.04.26"


def main(db_path: str = DEFAULT_DB) -> None:
    today = date.today().isoformat()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        boards = conn.execute(
            """
            SELECT board_id, board_name, serial, tool, data_source
            FROM boards
            WHERE product_name = 'ES4' AND data_source != 'firmware_log'
            ORDER BY board_id
            """
        ).fetchall()
        if not boards:
            print("No imported ES4 boards found.")
            return

        for board in boards:
            board_id = board["board_id"]
            rows = conn.execute(
                "SELECT event_id, firmware FROM firmware_history WHERE board_id = ?",
                (board_id,),
            ).fetchall()
            if not rows:
                conn.execute(
                    """
                    INSERT INTO firmware_history
                        (board_id, event_date, event_time, fpga, firmware, installer, result)
                    VALUES (?, ?, NULL, NULL, ?, 'manual update', 'PASS')
                    """,
                    (board_id, today, ES4_FIRMWARE),
                )
                print(
                    f"  insert board {board_id} {board['board_name']} "
                    f"{board['serial']} -> {ES4_FIRMWARE}"
                )
                continue

            for row in rows:
                conn.execute(
                    """
                    UPDATE firmware_history
                    SET firmware = ?, event_date = ?, fpga = NULL,
                        installer = 'manual update', result = 'PASS'
                    WHERE event_id = ?
                    """,
                    (ES4_FIRMWARE, today, row["event_id"]),
                )
            print(
                f"  update board {board_id} {board['board_name']} {board['serial']} "
                f"({rows[0]['firmware']} -> {ES4_FIRMWARE})"
            )

        conn.commit()
        print(f"\nUpdated {len(boards)} imported ES4 board(s) to {ES4_FIRMWARE}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
