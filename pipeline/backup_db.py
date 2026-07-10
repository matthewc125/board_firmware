#!/usr/bin/env python3
"""Create a timestamped backup of board_firmware.db under backups/."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DEFAULT_DB = PROJECT_DIR / "board_firmware.db"
BACKUPS_DIR = PROJECT_DIR / "backups"


def git_head() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def db_stats(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        boards = conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0]
        deleted = conn.execute("SELECT COUNT(*) FROM deleted_boards").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM board_events").fetchone()[0]
        firmware = conn.execute("SELECT COUNT(*) FROM firmware_history").fetchone()[0]
        imports = conn.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0]
    finally:
        conn.close()
    return {
        "boards": boards,
        "deleted_boards": deleted,
        "board_events": events,
        "firmware_history": firmware,
        "import_runs": imports,
    }


def create_backup(db_path: Path, backups_dir: Path, label: str | None = None) -> Path:
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")

    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder_name = f"{stamp}-{label}" if label else stamp
    dest_dir = backups_dir / folder_name
    dest_dir.mkdir(parents=True, exist_ok=False)

    dest_db = dest_dir / "board_firmware.db"
    shutil.copy2(db_path, dest_db)

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_db": str(db_path),
        "backup_db": str(dest_db),
        "git_head": git_head(),
        "stats": db_stats(dest_db),
    }
    (dest_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return dest_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup board_firmware.db to backups/")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Database to back up")
    parser.add_argument("--backups-dir", default=str(BACKUPS_DIR), help="Backup root folder")
    parser.add_argument("--label", help="Optional suffix for the backup folder name")
    args = parser.parse_args()

    dest = create_backup(Path(args.db), Path(args.backups_dir), label=args.label)
    print(f"Backup created: {dest}")
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    print(f"  boards: {manifest['stats']['boards']}")
    print(f"  deleted_boards: {manifest['stats']['deleted_boards']}")
    print(f"  board_events: {manifest['stats']['board_events']}")


if __name__ == "__main__":
    main()
