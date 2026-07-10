#!/usr/bin/env python3
"""Restore tracking-sheet dates on manually updated firmware history rows."""
from __future__ import annotations

import sqlite3
from os.path import abspath, dirname, join, exists

SCRIPT_DIR = dirname(abspath(__file__))
PROJECT_DIR = dirname(SCRIPT_DIR)
DEFAULT_DB = join(PROJECT_DIR, "board_firmware.db")
REFERENCE_BACKUP = join(PROJECT_DIR, "backups", "20260710-082014", "board_firmware.db")


def bap_dates_from_reference() -> dict[str, str]:
    if not exists(REFERENCE_BACKUP):
        return {}
    ref = sqlite3.connect(REFERENCE_BACKUP)
    ref.row_factory = sqlite3.Row
    try:
        rows = ref.execute(
            """
            SELECT b.serial, h.event_date
            FROM boards b
            JOIN firmware_history h ON h.board_id = b.board_id
            WHERE (b.product_name = 'BAP' OR b.board_name IN ('BAP', 'BAP2'))
              AND h.installer != 'manual update'
            ORDER BY b.serial, h.event_date DESC, h.event_id DESC
            """
        ).fetchall()
    finally:
        ref.close()

    dates: dict[str, str] = {}
    for row in rows:
        dates.setdefault(row["serial"], row["event_date"])
    return dates


def main(db_path: str = DEFAULT_DB) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        bap_dates = bap_dates_from_reference()

        es4_rows = conn.execute(
            """
            SELECT h.event_id, h.board_id, b.serial, b.source_updated_at, h.event_date
            FROM firmware_history h
            JOIN boards b ON b.board_id = h.board_id
            WHERE h.installer = 'manual update'
              AND b.product_name = 'ES4'
              AND b.data_source != 'firmware_log'
            ORDER BY h.board_id
            """
        ).fetchall()

        fixed = 0
        for row in es4_rows:
            event_date = row["source_updated_at"]
            if not event_date or event_date == row["event_date"]:
                continue
            conn.execute(
                "UPDATE firmware_history SET event_date = ? WHERE event_id = ?",
                (event_date, row["event_id"]),
            )
            print(
                f"  ES4 board {row['board_id']} SN{row['serial']}: "
                f"{row['event_date']} -> {event_date}"
            )
            fixed += 1

        bap_rows = conn.execute(
            """
            SELECT h.event_id, h.board_id, b.serial, h.event_date
            FROM firmware_history h
            JOIN boards b ON b.board_id = h.board_id
            WHERE h.installer = 'manual update'
              AND (b.product_name = 'BAP' OR b.board_name IN ('BAP', 'BAP2'))
            ORDER BY h.board_id
            """
        ).fetchall()

        for row in bap_rows:
            event_date = bap_dates.get(row["serial"])
            if not event_date or event_date == row["event_date"]:
                if not event_date:
                    print(
                        f"  skip BAP board {row['board_id']} SN{row['serial']} "
                        f"— no reference tracking date"
                    )
                continue
            conn.execute(
                "UPDATE firmware_history SET event_date = ? WHERE event_id = ?",
                (event_date, row["event_id"]),
            )
            print(
                f"  BAP board {row['board_id']} SN{row['serial']}: "
                f"{row['event_date']} -> {event_date}"
            )
            fixed += 1

        conn.commit()
        print(f"\nFixed {fixed} firmware history date(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
