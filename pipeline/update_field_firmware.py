#!/usr/bin/env python3
"""Set unverified boards to field-deployed firmware versions."""
from __future__ import annotations

import sqlite3
import sys
from os.path import abspath, dirname, join

SCRIPT_DIR = dirname(abspath(__file__))
PROJECT_DIR = dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_DIR)

from db import firmware_verified_sql

DEFAULT_DB = join(PROJECT_DIR, "board_firmware.db")

# product_name -> (firmware, default_event_date)
FIELD_DEPLOYED = {
    "BAP": ("1.0.34", "2026-04-06"),
    "ES4": ("1.04.26", "2025-05-02"),
    "EM1": ("2.0.1.6", "2022-01-17"),
    "OBJ": ("2.0.1.6", "2022-01-17"),
}


def event_date(board: sqlite3.Row, default_date: str) -> str:
    return board["source_updated_at"] or default_date


def main(db_path: str = DEFAULT_DB) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        verified_expr = firmware_verified_sql("b").strip()
        boards = conn.execute(
            f"""
            SELECT b.board_id, b.product_name, b.board_name, b.serial,
                   b.source_updated_at, cf.firmware,
                   {verified_expr} AS firmware_verified
            FROM boards b
            LEFT JOIN current_firmware cf ON cf.board_id = b.board_id
            WHERE {verified_expr} = 'unverified'
            ORDER BY b.board_id
            """
        ).fetchall()

        updated = 0
        skipped = 0
        for board in boards:
            product = board["product_name"]
            target = FIELD_DEPLOYED.get(product)
            if not target:
                print(f"  skip board {board['board_id']} {product} — no field-deployed mapping")
                skipped += 1
                continue

            target_fw, default_date = target
            if board["firmware"] == target_fw:
                print(
                    f"  ok   board {board['board_id']} {product} {board['serial']} "
                    f"already {target_fw}"
                )
                skipped += 1
                continue

            date = event_date(board, default_date)
            rows = conn.execute(
                "SELECT event_id, firmware FROM firmware_history WHERE board_id = ?",
                (board["board_id"],),
            ).fetchall()
            if not rows:
                conn.execute(
                    """
                    INSERT INTO firmware_history
                        (board_id, event_date, event_time, fpga, firmware, installer, result)
                    VALUES (?, ?, NULL, NULL, ?, 'field deployed', 'PASS')
                    """,
                    (board["board_id"], date, target_fw),
                )
            else:
                for row in rows:
                    conn.execute(
                        """
                        UPDATE firmware_history
                        SET firmware = ?, event_date = ?, fpga = NULL,
                            installer = 'field deployed', result = 'PASS'
                        WHERE event_id = ?
                        """,
                        (target_fw, date, row["event_id"]),
                    )
            print(
                f"  fix  board {board['board_id']} {product} {board['serial']} "
                f"{board['firmware']} -> {target_fw} ({date})"
            )
            updated += 1

        conn.commit()
        print(f"\nUpdated {updated} board(s), {skipped} unchanged/skipped.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
