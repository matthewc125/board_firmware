"""Publish the local board database to GitHub using the machine's git credentials."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import config

DB_RELATIVE = Path(config.DATABASE).name
REMOTE_REF = "origin/main"
MAX_ITEMS = 40


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=config.BASE_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env={**os.environ},
    )


def _checkpoint_database() -> None:
    """Flush WAL pages into the main DB file before publishing."""
    conn = sqlite3.connect(config.DATABASE)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()


def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _fetch_boards(conn: sqlite3.Connection) -> dict[int, dict]:
    if not _table_exists(conn, "boards"):
        return {}
    rows = conn.execute(
        """
        SELECT board_id, tool, product_name, board_name, serial, inventory_serial,
               part_number, revision
        FROM boards
        """
    ).fetchall()
    return {int(row["board_id"]): dict(row) for row in rows}


def _fetch_current_firmware(conn: sqlite3.Connection) -> dict[int, dict]:
    if _table_exists(conn, "current_firmware"):
        rows = conn.execute(
            """
            SELECT board_id, firmware, fpga, event_date, event_time
            FROM current_firmware
            """
        ).fetchall()
        return {int(row["board_id"]): dict(row) for row in rows}

    if not _table_exists(conn, "firmware_history"):
        return {}
    rows = conn.execute(
        """
        SELECT h.board_id, h.firmware, h.fpga, h.event_date, h.event_time
        FROM firmware_history h
        WHERE h.event_id = (
            SELECT h2.event_id
            FROM firmware_history h2
            WHERE h2.board_id = h.board_id
            ORDER BY h2.event_date DESC,
                     COALESCE(h2.event_time, '00:00:00') DESC,
                     h2.event_id DESC
            LIMIT 1
        )
        """
    ).fetchall()
    return {int(row["board_id"]): dict(row) for row in rows}


def _fetch_catalog(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    if not _table_exists(conn, "firmware_catalog"):
        return {}
    cols = {row[1] for row in conn.execute("PRAGMA table_info(firmware_catalog)")}
    select_cols = [
        "family",
        "version",
        "is_field_deployed",
        "tools",
        "notes",
        "release_date",
    ]
    if "fpga" in cols:
        select_cols.insert(2, "fpga")
    if "in_status_ranking" in cols:
        select_cols.append("in_status_ranking")
    rows = conn.execute(
        f"SELECT {', '.join(select_cols)} FROM firmware_catalog"
    ).fetchall()
    return {(row["family"], row["version"]): dict(row) for row in rows}


def _history_ids(conn: sqlite3.Connection) -> set[int]:
    if not _table_exists(conn, "firmware_history"):
        return set()
    return {
        int(row[0])
        for row in conn.execute("SELECT event_id FROM firmware_history").fetchall()
    }


def _board_label(board: dict) -> str:
    return (
        f"#{board['board_id']} {board.get('product_name') or ''} "
        f"{board.get('board_name') or ''} {board.get('tool') or ''} "
        f"serial {board.get('serial') or '—'}"
    ).strip()


def _fmt(value) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


def _trim(items: list[str]) -> list[str]:
    if len(items) <= MAX_ITEMS:
        return items
    hidden = len(items) - MAX_ITEMS
    return items[:MAX_ITEMS] + [f"…and {hidden} more"]


def _changed_fields(before: dict, after: dict, fields: tuple[str, ...]) -> list[str]:
    changes = []
    for field in fields:
        old = before.get(field)
        new = after.get(field)
        if old != new:
            changes.append(f"{field}: {_fmt(old)} → {_fmt(new)}")
    return changes


def _load_remote_database(temp_path: Path) -> dict | None:
    """Write origin/main DB into temp_path. Returns error dict on failure, else None."""
    _run(["git", "fetch", "origin", "main"])
    probe = _run(["git", "cat-file", "-e", f"{REMOTE_REF}:{DB_RELATIVE}"])
    if probe.returncode != 0:
        return {
            "ok": False,
            "message": f"Could not find {DB_RELATIVE} on {REMOTE_REF}.",
            "detail": (probe.stderr or probe.stdout).strip(),
        }

    raw_bin = subprocess.run(
        ["git", "show", f"{REMOTE_REF}:{DB_RELATIVE}"],
        cwd=config.BASE_DIR,
        capture_output=True,
        check=False,
        env={**os.environ},
    )
    if raw_bin.returncode != 0:
        return {
            "ok": False,
            "message": "Could not read remote database.",
            "detail": (raw_bin.stderr or b"").decode("utf-8", errors="replace").strip(),
        }
    temp_path.write_bytes(raw_bin.stdout)
    return None


def preview_database_publish() -> dict:
    """Compare local DB against origin/main and summarize what a publish would change."""
    db_path = Path(config.DATABASE)
    if not db_path.is_file():
        return {"ok": False, "message": "Database file not found.", "detail": str(db_path)}

    git_check = _run(["git", "rev-parse", "--is-inside-work-tree"])
    if git_check.returncode != 0:
        return {
            "ok": False,
            "message": "Not a git repository.",
            "detail": (git_check.stderr or git_check.stdout).strip(),
        }

    try:
        _checkpoint_database()
    except sqlite3.Error as exc:
        return {
            "ok": False,
            "message": "Could not checkpoint the database.",
            "detail": str(exc),
        }

    status = _run(["git", "status", "--porcelain", "--", DB_RELATIVE])
    local_dirty = bool(status.stdout.strip()) if status.returncode == 0 else True

    with tempfile.TemporaryDirectory(prefix="fwdb-preview-") as tmp:
        remote_path = Path(tmp) / DB_RELATIVE
        err = _load_remote_database(remote_path)
        if err:
            return err

        local = _connect(db_path)
        remote = _connect(remote_path)
        try:
            local_boards = _fetch_boards(local)
            remote_boards = _fetch_boards(remote)
            local_fw = _fetch_current_firmware(local)
            remote_fw = _fetch_current_firmware(remote)
            local_cat = _fetch_catalog(local)
            remote_cat = _fetch_catalog(remote)
            local_hist = _history_ids(local)
            remote_hist = _history_ids(remote)
        finally:
            local.close()
            remote.close()

    boards_added = []
    boards_removed = []
    boards_changed = []
    for board_id in sorted(set(local_boards) - set(remote_boards)):
        boards_added.append(_board_label(local_boards[board_id]))
    for board_id in sorted(set(remote_boards) - set(local_boards)):
        boards_removed.append(_board_label(remote_boards[board_id]))
    for board_id in sorted(set(local_boards) & set(remote_boards)):
        changes = _changed_fields(
            remote_boards[board_id],
            local_boards[board_id],
            ("tool", "product_name", "board_name", "serial", "inventory_serial", "part_number", "revision"),
        )
        if changes:
            boards_changed.append(f"{_board_label(local_boards[board_id])} — " + "; ".join(changes))

    firmware_changed = []
    for board_id in sorted(set(local_boards) | set(remote_boards)):
        before = remote_fw.get(board_id)
        after = local_fw.get(board_id)
        if before == after:
            continue
        label = _board_label(
            local_boards.get(board_id) or remote_boards.get(board_id) or {"board_id": board_id}
        )
        before_fw = _fmt(before.get("firmware") if before else None)
        after_fw = _fmt(after.get("firmware") if after else None)
        before_fpga = _fmt(before.get("fpga") if before else None)
        after_fpga = _fmt(after.get("fpga") if after else None)
        before_date = _fmt(before.get("event_date") if before else None)
        after_date = _fmt(after.get("event_date") if after else None)
        parts = [f"firmware {before_fw} → {after_fw}"]
        if before_fpga != after_fpga:
            parts.append(f"fpga {before_fpga} → {after_fpga}")
        if before_date != after_date:
            parts.append(f"date {before_date} → {after_date}")
        firmware_changed.append(f"{label} — " + "; ".join(parts))

    catalog_added = []
    catalog_removed = []
    catalog_changed = []
    for key in sorted(set(local_cat) - set(remote_cat)):
        entry = local_cat[key]
        catalog_added.append(
            f"{entry['family']} {entry['version']}"
            + (" (field)" if entry.get("is_field_deployed") else "")
        )
    for key in sorted(set(remote_cat) - set(local_cat)):
        entry = remote_cat[key]
        catalog_removed.append(f"{entry['family']} {entry['version']}")
    for key in sorted(set(local_cat) & set(remote_cat)):
        changes = _changed_fields(
            remote_cat[key],
            local_cat[key],
            (
                "fpga",
                "is_field_deployed",
                "tools",
                "notes",
                "release_date",
                "in_status_ranking",
            ),
        )
        if changes:
            catalog_changed.append(f"{key[0]} {key[1]} — " + "; ".join(changes))

    history_added = len(local_hist - remote_hist)
    history_removed = len(remote_hist - local_hist)

    sections = []
    def add_section(title: str, items: list[str], count: int | None = None):
        if not items and not count:
            return
        sections.append(
            {
                "title": title,
                "count": count if count is not None else len(items),
                "items": _trim(items) if items else [],
            }
        )

    add_section("Boards added", boards_added)
    add_section("Boards removed", boards_removed)
    add_section("Boards changed", boards_changed)
    add_section("Current firmware changed", firmware_changed)
    add_section("Catalog releases added", catalog_added)
    add_section("Catalog releases removed", catalog_removed)
    add_section("Catalog releases changed", catalog_changed)
    if history_added or history_removed:
        hist_items = []
        if history_added:
            hist_items.append(f"+{history_added} firmware history row(s)")
        if history_removed:
            hist_items.append(f"−{history_removed} firmware history row(s)")
        add_section("Firmware history", hist_items)

    has_changes = bool(sections) or local_dirty
    summary = {
        "boards_added": len(boards_added),
        "boards_removed": len(boards_removed),
        "boards_changed": len(boards_changed),
        "firmware_changed": len(firmware_changed),
        "catalog_added": len(catalog_added),
        "catalog_removed": len(catalog_removed),
        "catalog_changed": len(catalog_changed),
        "history_added": history_added,
        "history_removed": history_removed,
        "local_db_modified": local_dirty,
    }

    if not has_changes:
        message = f"Local database matches {REMOTE_REF}."
    else:
        message = f"Changes versus {REMOTE_REF} (what GitHub Pages would get after publish)."

    return {
        "ok": True,
        "has_changes": has_changes,
        "message": message,
        "remote_ref": REMOTE_REF,
        "summary": summary,
        "sections": sections,
    }


def publish_database_to_github() -> dict:
    """
    Commit and push ``board_firmware.db`` only.

    Returns ``{"ok": bool, "message": str, "detail": str}``.
    """
    db_path = Path(config.DATABASE)
    if not db_path.is_file():
        return {"ok": False, "message": "Database file not found.", "detail": str(db_path)}

    git_check = _run(["git", "rev-parse", "--is-inside-work-tree"])
    if git_check.returncode != 0:
        return {
            "ok": False,
            "message": "Not a git repository.",
            "detail": (git_check.stderr or git_check.stdout).strip(),
        }

    try:
        _checkpoint_database()
    except sqlite3.Error as exc:
        return {
            "ok": False,
            "message": "Could not checkpoint the database before publish.",
            "detail": str(exc),
        }

    status = _run(["git", "status", "--porcelain", "--", DB_RELATIVE])
    if status.returncode != 0:
        return {
            "ok": False,
            "message": "Could not read git status.",
            "detail": (status.stderr or status.stdout).strip(),
        }

    dirty = bool(status.stdout.strip())
    detail_parts: list[str] = []

    if dirty:
        added = _run(["git", "add", "--", DB_RELATIVE])
        if added.returncode != 0:
            return {
                "ok": False,
                "message": "Could not stage the database.",
                "detail": (added.stderr or added.stdout).strip(),
            }

        commit = _run(
            [
                "git",
                "commit",
                "-m",
                "Publish local board database to GitHub Pages.",
            ]
        )
        if commit.returncode != 0:
            return {
                "ok": False,
                "message": "Could not create commit.",
                "detail": (commit.stderr or commit.stdout).strip(),
            }
        detail_parts.append((commit.stdout or "").strip())
    else:
        detail_parts.append("No local database changes to commit.")

    push = _run(["git", "push", "origin", "HEAD"])
    if push.returncode != 0:
        return {
            "ok": False,
            "message": "Commit ok, but push failed." if dirty else "Push failed.",
            "detail": (push.stderr or push.stdout).strip(),
        }

    detail_parts.append((push.stdout or push.stderr or "Push completed.").strip())
    if dirty:
        message = "Database committed and pushed to GitHub. Pages will rebuild shortly."
    else:
        message = "Database already up to date; pushed current branch to GitHub."

    return {
        "ok": True,
        "message": message,
        "detail": "\n".join(p for p in detail_parts if p),
    }
