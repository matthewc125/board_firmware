"""Publish the local board database to GitHub using the machine's git credentials."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import config

DB_RELATIVE = Path(config.DATABASE).name


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=config.BASE_DIR,
        capture_output=True,
        text=True,
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
