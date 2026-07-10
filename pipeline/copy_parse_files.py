#!/usr/bin/env python3
"""
Copy parseable documents from a large source tree to a local folder.

Read-only on the source: files are copied, never moved or modified.

Default extensions:
  - CSV: .csv
  - Word: .doc, .docx
  - Excel: .xlsx, .xlsm, .xls (with --include-xlsx)

Hardware-only mode (--hardware-only) keeps files whose names/paths look like
tracking docs: log, revision, version, history, notes, batchnumbers, status,
assignment, repair, modification, summary, report, envelope, acceptance,
warehouse, inventory, etc. Excludes fab/vendor duplicates and automated test CSVs.

Tracking-only mode (--tracking-only) narrows further to serial/part/inventory
sources: batch numbers, repair logs, board status, tool assignments, warehouse
inventory. Excludes bench tests, acceptance docs, unit reports, and summaries.

Usage:
  py -3 pipeline/copy_parse_files.py --source "\\\\netapp\\share\\folder"
  py -3 pipeline/copy_parse_files.py --from-index docs.txt --hardware-only --dry-run
  py -3 pipeline/copy_parse_files.py --from-index docs.txt --hardware-only
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DEFAULT_DEST = PROJECT_DIR / "pipeline" / "netapp_import"

CSV_EXTENSIONS = {".csv"}
WORD_EXTENSIONS = {".doc", ".docx"}
XLSX_EXTENSIONS = {".xlsx", ".xlsm", ".xls"}

HARDWARE_NAME_PATTERN = re.compile(
    r"(?i)(?:^|[^a-z])("
    r"log|revision|version|history|batchnumbers?|batch_|_batch|firstlevel|"
    r"inventory|status|assignment|board_assignments|repair|modification|"
    r"summary|report|envelope|acceptance|warehouse|repairs|repairlog|"
    r"drivelog|modification log|notes"
    r")(?:[^a-z]|$)|\.log\.|repairlog|drivelog|_log\.docx|_log\.xlsx",
)

HARDWARE_EXCLUDE_PATH = re.compile(
    r"(?i)(Output_Boards|R&D Backup|From_VectorFab|From_RushPCB|PCB_Stackup|"
    r"ManualFreqSweep|FreqSweep|single op-amp topology|Mechanical\\|"
    r"Project Outputs|Altium\\)",
)

HARDWARE_EXCLUDE_NAME = re.compile(
    r"(?i)(vectorfab-inventory|pdf-inventory|pcb fab|fabrication notes|dfm-report|"
    r"ejectors|faceplates|thermalcycle|\.log\.xlsx|noise_rank|"
    r"bom-wip|assembly notes|quotation|rushpcb|stackup|topology)",
)

# Serial/part/inventory tracking — not bench tests or performance reports.
TRACKING_INCLUDE_NAME = re.compile(
    r"(?i)(batchnumbers?|batch_|_batch|inventory|warehouse|"
    r"assignment|board_assignments|board.status|"
    r"repairlog|drivelog|repair[._ ]by[._ ]part|_repairs|"
    r"repair.and.modification|repairandmodification|modification.log)",
)

TRACKING_EXCLUDE_NAME = re.compile(
    r"(?i)(test|testing|acceptance|firstlevel|envelope|bandwidth|"
    r"noise|design.notes|failure.analysis|"
    r"test.report|test_summary|bench_|filter.notes|revision.summary|"
    r"acceptance.tests|_report(?:_|\.|$))",
)

TRACKING_EXCLUDE_PATH = re.compile(
    r"(?i)(LogFiles\\Reports\\|CurrentNoise\\|01_Test_|\\Summary\\)",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy CSV/Word/Excel documents from a source tree or file index.",
    )
    parser.add_argument(
        "--source",
        help="Root folder to scan (mapped drive or UNC path).",
    )
    parser.add_argument(
        "--from-index",
        help="Text file with one source path per line (from a prior scan).",
    )
    parser.add_argument(
        "--dest",
        default=str(DEFAULT_DEST),
        help=f"Local destination root (default: {DEFAULT_DEST}).",
    )
    parser.add_argument(
        "--include-xlsx",
        action="store_true",
        help="Also copy Excel workbooks (.xlsx, .xlsm, .xls).",
    )
    parser.add_argument(
        "--hardware-only",
        action="store_true",
        help="When using --from-index, keep only hardware tracking doc names.",
    )
    parser.add_argument(
        "--tracking-only",
        action="store_true",
        help="Serial/part/inventory docs only (excludes test reports and acceptance).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be copied without copying.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite destination files if they already exist.",
    )
    parser.add_argument(
        "--write-filtered-index",
        help="Write matched paths from --from-index to this file and exit.",
    )
    return parser.parse_args()


def is_hardware_doc(path: str) -> bool:
    name = Path(path).name
    if HARDWARE_EXCLUDE_PATH.search(path):
        return False
    if HARDWARE_EXCLUDE_NAME.search(name):
        return False
    return bool(HARDWARE_NAME_PATTERN.search(name))


def is_tracking_doc(path: str) -> bool:
    """Serial numbers, batch lists, repairs, assignments — not performance testing."""
    name = Path(path).name
    if HARDWARE_EXCLUDE_PATH.search(path):
        return False
    if HARDWARE_EXCLUDE_NAME.search(name):
        return False
    if TRACKING_EXCLUDE_PATH.search(path):
        return False
    if TRACKING_EXCLUDE_NAME.search(name):
        return False
    return bool(TRACKING_INCLUDE_NAME.search(name))


def load_index(
    index_path: Path,
    hardware_only: bool,
    tracking_only: bool,
) -> list[Path]:
    lines = index_path.read_text(encoding="utf-8", errors="replace").splitlines()
    paths: list[Path] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        path_str = line.split("\t")[0]
        if hardware_only and not is_hardware_doc(path_str):
            continue
        if tracking_only and not is_tracking_doc(path_str):
            continue
        paths.append(Path(path_str))
    return paths


def marker_relative(path: Path, marker: str = "Column_Electronics") -> Path:
    parts = path.parts
    for i, part in enumerate(parts):
        if part.lower() == marker.lower():
            return Path(*parts[i + 1 :])
    return Path(path.name)


def allowed_extensions(include_xlsx: bool) -> set[str]:
    exts = set(CSV_EXTENSIONS) | set(WORD_EXTENSIONS)
    if include_xlsx:
        exts |= XLSX_EXTENSIONS
    return exts


def unique_destination(dest_file: Path, overwrite: bool) -> Path:
    if overwrite or not dest_file.exists():
        return dest_file
    stem = dest_file.stem
    suffix = dest_file.suffix
    parent = dest_file.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}__{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def scan_and_copy(
    source: Path,
    dest_root: Path,
    extensions: set[str],
    dry_run: bool,
    overwrite: bool,
) -> dict:
    if not source.exists():
        raise FileNotFoundError(f"Source not found: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"Source is not a directory: {source}")

    copied: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []

    for path in source.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue

        rel = path.relative_to(source)
        dest_file = dest_root / rel

        record = {
            "source": str(path),
            "dest": str(dest_file),
            "size_bytes": path.stat().st_size,
        }

        try:
            if dry_run:
                copied.append(record)
                continue

            dest_file.parent.mkdir(parents=True, exist_ok=True)
            final_dest = unique_destination(dest_file, overwrite=overwrite)
            shutil.copy2(path, final_dest)
            record["dest"] = str(final_dest)
            copied.append(record)
        except OSError as exc:
            errors.append({**record, "error": str(exc)})

    return {
        "source": str(source),
        "dest": str(dest_root),
        "dry_run": dry_run,
        "extensions": sorted(extensions),
        "copied_count": len(copied),
        "error_count": len(errors),
        "copied": copied,
        "skipped": skipped,
        "errors": errors,
    }


def copy_from_paths(
    source_paths: list[Path],
    dest_root: Path,
    dry_run: bool,
    overwrite: bool,
) -> dict:
    copied: list[dict] = []
    errors: list[dict] = []

    for path in source_paths:
        if not path.is_file():
            continue
        rel = marker_relative(path)
        dest_file = dest_root / rel
        record = {
            "source": str(path),
            "dest": str(dest_file),
            "size_bytes": path.stat().st_size,
        }
        try:
            if dry_run:
                copied.append(record)
                continue
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            final_dest = unique_destination(dest_file, overwrite=overwrite)
            shutil.copy2(path, final_dest)
            record["dest"] = str(final_dest)
            copied.append(record)
        except OSError as exc:
            errors.append({**record, "error": str(exc)})

    return {
        "dest": str(dest_root),
        "dry_run": dry_run,
        "copied_count": len(copied),
        "error_count": len(errors),
        "copied": copied,
        "skipped": [],
        "errors": errors,
    }


def write_manifest(dest_root: Path, report: dict) -> Path:
    dest_root.mkdir(parents=True, exist_ok=True)
    manifest = dest_root / "_copy_manifest.json"
    manifest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    args = parse_args()
    dest_root = Path(args.dest).resolve()

    if args.from_index:
        index_path = Path(args.from_index).resolve()
        paths = load_index(
            index_path,
            hardware_only=args.hardware_only,
            tracking_only=args.tracking_only,
        )
        if args.write_filtered_index:
            out = Path(args.write_filtered_index)
            out.write_text("\n".join(str(p) for p in paths) + "\n", encoding="utf-8")
            print(f"Wrote {len(paths)} path(s) to {out}")
            return
        report = copy_from_paths(
            source_paths=paths,
            dest_root=dest_root,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
        )
        source_label = f"index:{index_path}"
    elif args.source:
        source = Path(args.source).resolve()
        extensions = allowed_extensions(args.include_xlsx)
        report = scan_and_copy(
            source=source,
            dest_root=dest_root,
            extensions=extensions,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
        )
        source_label = str(source)
    else:
        raise SystemExit("Provide --source or --from-index")

    report["copied_at"] = datetime.now(timezone.utc).isoformat()

    if not args.dry_run:
        manifest = write_manifest(dest_root, report)
        print(f"Manifest: {manifest}")

    total_bytes = sum(item["size_bytes"] for item in report["copied"])
    print(f"Source:  {source_label}")
    print(f"Dest:    {dest_root}")
    print(f"Mode:    {'dry-run' if args.dry_run else 'copy'}")
    print(f"Matched: {report['copied_count']} file(s), {total_bytes:,} bytes")
    if report["error_count"]:
        print(f"Errors:  {report['error_count']}")
        for item in report["errors"][:10]:
            print(f"  - {item['source']}: {item['error']}")

    if report["copied"]:
        print("\nSample files:")
        for item in report["copied"][:20]:
            print(f"  - {item['source']}")
        if report["copied_count"] > 20:
            print(f"  ... and {report['copied_count'] - 20} more")


if __name__ == "__main__":
    main()
