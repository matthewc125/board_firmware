#!/usr/bin/env python3
"""Set imported ES4 board firmware to 1.04.26 on the curated database."""
from __future__ import annotations

import sqlite3
from os.path import abspath, dirname, join

SCRIPT_DIR = dirname(abspath(__file__))
PROJECT_DIR = dirname(SCRIPT_DIR)
DEFAULT_DB = join(PROJECT_DIR, "board_firmware.db")
ES4_FIRMWARE = "1.04.26"


def firmware_event_date(board: sqlite3.Row) -> str | None:
    """Use the electronics tracking sheet date when available."""
    return board["source_updated_at"]


def main(db_path: str = DEFAULT_DB) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        boards = conn.execute(
            """
            SELECT board_id, board_name, serial, tool, data_source, source_updated_at
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
            event_date = firmware_event_date(board)
            if not event_date:
                print(
                    f"  skip board {board_id} {board['board_name']} {board['serial']} "
                    f"— no tracking sheet date"
                )
                continue

            rows = conn.execute(
                "SELECT event_id, firmware, event_date FROM firmware_history WHERE board_id = ?",
                (board_id,),
            ).fetchall()
            if not rows:
                conn.execute(
                    """
                    INSERT INTO firmware_history
                        (board_id, event_date, event_time, fpga, firmware, installer, result)
                    VALUES (?, ?, NULL, NULL, ?, 'manual update', 'PASS')
                    """,
                    (board_id, event_date, ES4_FIRMWARE),
                )
                print(
                    f"  insert board {board_id} {board['board_name']} "
                    f"{board['serial']} -> {ES4_FIRMWARE} ({event_date})"
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
                    (ES4_FIRMWARE, event_date, row["event_id"]),
                )
            print(
                f"  update board {board_id} {board['board_name']} {board['serial']} "
                f"({rows[0]['firmware']} -> {ES4_FIRMWARE}, date {event_date})"
            )

        conn.commit()
        print(f"\nUpdated {len(boards)} imported ES4 board(s) to {ES4_FIRMWARE}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
