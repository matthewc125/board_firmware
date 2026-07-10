#!/usr/bin/env python3
"""Compare NetApp import serials against deleted and active boards."""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from inventory_normalize import normalize_inventory_serial
from netapp_parsers import iter_import_files, parse_file, serial_match_keys

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DEFAULT_DB = PROJECT_DIR / "board_firmware.db"
DEFAULT_IMPORT = SCRIPT_DIR / "netapp_import"


def collect_netapp_serials(import_root: Path) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for path in iter_import_files(import_root):
        parsed = parse_file(path)
        rel = str(path.relative_to(import_root))
        for item in (*parsed.patches, *parsed.events):
            serial = getattr(item, "serial", None)
            if not serial:
                continue
            for key in serial_match_keys(serial):
                bucket = index.setdefault(key, {"serials": set(), "sources": set()})
                bucket["serials"].add(serial)
                bucket["sources"].add(rel)
    return index


def board_keys(row: sqlite3.Row) -> set[str]:
    keys: set[str] = set()
    for field in ("inventory_serial", "serial"):
        val = row[field]
        if not val:
            continue
        keys |= serial_match_keys(val)
        inv = normalize_inventory_serial(val)
        if inv:
            keys.add(inv)
    return keys


def main() -> None:
    netapp = collect_netapp_serials(DEFAULT_IMPORT)
    all_keys = set(netapp.keys())
    print(f"NetApp serial keys parsed: {len(all_keys)}")

    conn = sqlite3.connect(DEFAULT_DB)
    conn.row_factory = sqlite3.Row
    deleted = list(conn.execute("SELECT * FROM deleted_boards ORDER BY board_id"))
    active = list(conn.execute("SELECT * FROM boards ORDER BY board_id"))

    overlap_deleted = []
    for row in deleted:
        hit = board_keys(row) & all_keys
        if not hit:
            continue
        sources: set[str] = set()
        for key in hit:
            sources |= netapp[key]["sources"]
        fw = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT firmware FROM deleted_firmware_history WHERE board_id = ?",
                (row["board_id"],),
            )
        ]
        overlap_deleted.append(
            {
                "board_id": row["board_id"],
                "product": row["product_name"],
                "serial": row["serial"],
                "inventory_serial": row["inventory_serial"],
                "status": row["status"],
                "tool": row["tool"],
                "deleted_at": row["deleted_at"],
                "firmware": fw,
                "sources": sorted(sources),
            }
        )

    overlap_active = []
    for row in active:
        hit = board_keys(row) & all_keys
        if not hit:
            continue
        sources: set[str] = set()
        for key in hit:
            sources |= netapp[key]["sources"]
        overlap_active.append(
            {
                "board_id": row["board_id"],
                "product": row["product_name"],
                "serial": row["serial"],
                "inventory_serial": row["inventory_serial"],
                "tool": row["tool"],
                "sources": sorted(sources),
            }
        )

    print(f"Overlap with deleted_boards: {len(overlap_deleted)} / {len(deleted)}")
    print(f"Overlap with active boards:  {len(overlap_active)} / {len(active)}")
    print()

    by_product = defaultdict(list)
    for item in overlap_deleted:
        by_product[item["product"]].append(item)

    print("=== Removed boards that appear in NetApp docs ===")
    for product in sorted(by_product):
        items = by_product[product]
        print(f"\n{product} ({len(items)})")
        for item in items:
            label = item["inventory_serial"] or item["serial"]
            fw = f" firmware={item['firmware']}" if item["firmware"] else ""
            print(
                f"  board {item['board_id']:>3}  {label:<22}  "
                f"status={item['status'] or '-':<12} tool={item['tool'] or '-'}{fw}"
            )
            print(f"           docs: {', '.join(item['sources'][:3])}")

    print("\n=== Active boards also in NetApp docs ===")
    for item in overlap_active:
        label = item["inventory_serial"] or item["serial"]
        print(
            f"  board {item['board_id']:>3}  {item['product']:<6} {label:<22}  "
            f"tool={item['tool'] or '-'}"
        )
        print(f"           docs: {', '.join(item['sources'][:3])}")

    fw_overlap = [i for i in overlap_deleted if i["firmware"]]
    prune_overlap = [i for i in overlap_deleted if not i["firmware"]]
    print(f"\n=== By removal type (approximate) ===")
    print(f"  Had firmware history (firmware purge): {len(fw_overlap)}")
    print(f"  No firmware history (inventory prune):   {len(prune_overlap)}")

    deleted_em1 = [r for r in deleted if r["product_name"] == "EM1"]
    em1_hits = [i for i in overlap_deleted if i["product"] == "EM1"]
    print(f"\n=== EM1-specific ===")
    print(f"  Deleted EM1 boards total: {len(deleted_em1)}")
    print(f"  Deleted EM1 in NetApp docs: {len(em1_hits)}")

    snj_keys = sorted(
        k for k in all_keys
        if re.match(r"^(SN)?J\d+", k, re.IGNORECASE)
    )
    all_db_keys: set[str] = set()
    for row in (*deleted, *active):
        all_db_keys |= board_keys(row)
    snj_only_netapp = [k for k in snj_keys if k not in all_db_keys]
    print(f"  SNJ RevJ-style serials in NetApp docs: {len(snj_keys)}")
    print(f"  Of those, not in DB at all (active or deleted): {len(snj_only_netapp)}")

    conn.close()


if __name__ == "__main__":
    main()
