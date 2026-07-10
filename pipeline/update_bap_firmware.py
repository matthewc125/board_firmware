#!/usr/bin/env python3
"""Set BAP firmware to 1.0.34 on the curated 51-board database."""
from __future__ import annotations

import sqlite3
from os.path import abspath, dirname, join, exists

SCRIPT_DIR = dirname(abspath(__file__))
PROJECT_DIR = dirname(SCRIPT_DIR)
DEFAULT_DB = join(PROJECT_DIR, "board_firmware.db")

BAP_FIRMWARE = "1.0.34"
SKIP_BOARD_IDS = {8}  # BAP2 SN013 — known different firmware (2.00.02.77)
REFERENCE_BACKUP = join(PROJECT_DIR, "backups", "20260710-082014", "board_firmware.db")


def tracking_date_from_reference_backup(serial: str) -> str | None:
    if not exists(REFERENCE_BACKUP):
        return None
    ref = sqlite3.connect(REFERENCE_BACKUP)
    ref.row_factory = sqlite3.Row
    try:
        row = ref.execute(
            """
            SELECT h.event_date
            FROM boards b
            JOIN firmware_history h ON h.board_id = b.board_id
            WHERE b.serial = ?
              AND (b.product_name = 'BAP' OR b.board_name IN ('BAP', 'BAP2'))
              AND h.installer != 'manual update'
            ORDER BY h.event_date DESC, h.event_id DESC
            LIMIT 1
            """,
            (serial,),
        ).fetchone()
    finally:
        ref.close()
    return row["event_date"] if row else None


def prior_firmware_date(conn: sqlite3.Connection, board_id: int, serial: str) -> str | None:
    """Prefer the tracking-sheet date from existing or archived firmware history."""
    row = conn.execute(
        """
        SELECT event_date
        FROM firmware_history
        WHERE board_id = ? AND installer != 'manual update'
        ORDER BY event_date DESC, event_id DESC
        LIMIT 1
        """,
        (board_id,),
    ).fetchone()
    if row and row["event_date"]:
        return row["event_date"]

    row = conn.execute(
        """
        SELECT event_date
        FROM deleted_firmware_history
        WHERE board_id = ? AND installer != 'manual update'
        ORDER BY event_date DESC, event_id DESC
        LIMIT 1
        """,
        (board_id,),
    ).fetchone()
    if row and row["event_date"]:
        return row["event_date"]

    row = conn.execute(
        "SELECT source_updated_at FROM boards WHERE board_id = ?",
        (board_id,),
    ).fetchone()
    if row and row["source_updated_at"]:
        return row["source_updated_at"]
    return tracking_date_from_reference_backup(serial)


def main(db_path: str = DEFAULT_DB) -> None:
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

            event_date = prior_firmware_date(conn, board_id, board["serial"])
            if not event_date:
                print(
                    f"  skip board {board_id} {board['board_name']} SN{board['serial']} "
                    f"— no tracking sheet date"
                )
                skipped += 1
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
                    (board_id, event_date, BAP_FIRMWARE),
                )
                print(
                    f"  insert board {board_id} {board['board_name']} SN{board['serial']} "
                    f"-> {BAP_FIRMWARE} ({event_date})"
                )
            else:
                for row in rows:
                    conn.execute(
                        """
                        UPDATE firmware_history
                        SET firmware = ?, event_date = ?, fpga = NULL,
                            installer = 'manual update', result = 'PASS'
                        WHERE event_id = ?
                        """,
                        (BAP_FIRMWARE, event_date, row["event_id"]),
                    )
                print(
                    f"  update board {board_id} {board['board_name']} SN{board['serial']} "
                    f"({rows[0]['firmware']} -> {BAP_FIRMWARE}, date {event_date})"
                )
            updated += 1

        conn.commit()
        print(f"\nDone: {updated} updated, {skipped} skipped.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
