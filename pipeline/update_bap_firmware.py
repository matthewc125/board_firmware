#!/usr/bin/env python3
"""Set BAP firmware to 1.0.34 on the curated 51-board database."""
from __future__ import annotations

import sqlite3
from datetime import date
from os.path import abspath, dirname, join

SCRIPT_DIR = dirname(abspath(__file__))
PROJECT_DIR = dirname(SCRIPT_DIR)
DEFAULT_DB = join(PROJECT_DIR, "board_firmware.db")

BAP_FIRMWARE = "1.0.34"
SKIP_BOARD_IDS = {8}  # BAP2 SN013 — known different firmware (2.00.02.77)


def main(db_path: str = DEFAULT_DB) -> None:
    today = date.today().isoformat()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        bap_boards = conn.execute(
            """
            SELECT board_id, serial, board_name
            FROM boards
            WHERE product_name = 'BAP' OR board_name IN ('BAP', 'BAP2')
            ORDER BY board_id
            """
        ).fetchall()

        updated = 0
        skipped = 0
        for board in bap_boards:
            board_id = board["board_id"]
            if board_id in SKIP_BOARD_IDS:
                row = conn.execute(
                    "SELECT firmware FROM firmware_history WHERE board_id = ? "
                    "ORDER BY event_date DESC, event_id DESC LIMIT 1",
                    (board_id,),
                ).fetchone()
                print(
                    f"  skip board {board_id} {board['board_name']} "
                    f"SN{board['serial']} — keeping {row['firmware'] if row else 'no firmware'}"
                )
                skipped += 1
                continue

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
                    (board_id, today, BAP_FIRMWARE),
                )
                print(f"  insert board {board_id} {board['board_name']} SN{board['serial']} -> {BAP_FIRMWARE}")
            else:
                for row in rows:
                    conn.execute(
                        """
                        UPDATE firmware_history
                        SET firmware = ?, event_date = ?, fpga = NULL,
                            installer = 'manual update', result = 'PASS'
                        WHERE event_id = ?
                        """,
                        (BAP_FIRMWARE, today, row["event_id"]),
                    )
                print(
                    f"  update board {board_id} {board['board_name']} SN{board['serial']} "
                    f"({rows[0]['firmware']} -> {BAP_FIRMWARE})"
                )
            updated += 1

        conn.commit()
        print(f"\nDone: {updated} updated, {skipped} skipped.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
