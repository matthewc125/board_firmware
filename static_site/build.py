#!/usr/bin/env python3
"""Build a read-only static snapshot for GitHub Pages."""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
import db  # noqa: E402
import firmware_status_report  # noqa: E402

STATIC_SITE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = PROJECT_ROOT / "site"
PAGES = ("index", "status", "hardware", "board", "data")


def normalize_base_path(value: str) -> str:
    if not value or value == "/":
        return "/"
    return "/" + value.strip("/") + "/"


def export_csvs(output_data_dir: Path) -> list[dict]:
    output_data_dir.mkdir(parents=True, exist_ok=True)
    exports = []
    for table in db.list_tables():
        columns, rows, _ = db.fetch_table_rows(table["name"])
        path = output_data_dir / f"{table['name']}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, "") for col in columns})
        exports.append({"name": table["name"], "type": table["type"], "filename": path.name})
    return exports


def export_firmware_status(output_data_dir: Path) -> dict:
    output_data_dir.mkdir(parents=True, exist_ok=True)
    filename = "firmware_status.xlsx"
    path = output_data_dir / filename
    path.write_bytes(firmware_status_report.build_firmware_status_workbook())
    return {
        "name": "firmware_status",
        "type": "report",
        "filename": filename,
        "label": "Firmware status (Tool × board type)",
    }


def copy_assets(output_dir: Path) -> None:
    static_out = output_dir / "static"
    static_out.mkdir(parents=True, exist_ok=True)
    for name in ("style.css", "favicon.svg"):
        shutil.copy2(PROJECT_ROOT / "static" / name, static_out / name)
    shutil.copy2(STATIC_SITE_DIR / "site.js", static_out / "site.js")


def copy_database(output_dir: Path) -> None:
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config.DATABASE, data_dir / "board_firmware.db")


def render_pages(
    output_dir: Path,
    base_path: str,
    built_at: str,
    csv_exports: list[dict],
    firmware_status: dict | None = None,
    status_matrix: dict | None = None,
) -> None:
    env = Environment(
        loader=FileSystemLoader(STATIC_SITE_DIR / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )
    context = {
        "base_path": base_path,
        "built_at": built_at,
        "csv_exports": csv_exports,
        "firmware_status": firmware_status,
        "status_columns": status_matrix["columns"] if status_matrix else [],
        "status_sections": status_matrix["sections"] if status_matrix else [],
        "page": "",
    }
    for page in PAGES:
        page_context = {**context, "page": page}
        template = env.get_template(f"{page}.html")
        html = template.render(**page_context)
        (output_dir / f"{page}.html").write_text(html, encoding="utf-8")


def build(output_dir: Path, base_path: str) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    copy_assets(output_dir)
    copy_database(output_dir)
    data_dir = output_dir / "data"
    csv_exports = export_csvs(data_dir)
    firmware_status = export_firmware_status(data_dir)
    status_matrix = db.firmware_status_sections()
    render_pages(
        output_dir,
        base_path,
        built_at,
        csv_exports,
        firmware_status,
        status_matrix=status_matrix,
    )

    print(f"Built static site in {output_dir}")
    print(f"Base path: {base_path}")
    print(f"Database: {config.DATABASE}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the GitHub Pages static snapshot.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--base-path",
        default=os.environ.get("BASE_PATH", "/"),
        help="URL prefix for GitHub project pages (default: /)",
    )
    args = parser.parse_args()
    build(args.output.resolve(), normalize_base_path(args.base_path))


if __name__ == "__main__":
    main()
