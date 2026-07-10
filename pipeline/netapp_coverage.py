#!/usr/bin/env python3
"""Show which active boards match NetApp import data."""
import sqlite3
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from import_netapp import build_serial_index, resolve_board_id
from netapp_parsers import iter_import_files, parse_file, product_context, serial_match_keys

root = Path(__file__).resolve().parent / "netapp_import"
conn = sqlite3.connect(Path(__file__).resolve().parent.parent / "board_firmware.db")
conn.row_factory = sqlite3.Row

index, products, exact = build_serial_index(conn)
boards = list(conn.execute("SELECT * FROM boards ORDER BY board_id"))

# Collect all patches/events by resolved board_id
by_board: dict[int, dict] = {}
for path in iter_import_files(root):
    parsed = parse_file(path)
    rel = str(path.relative_to(root))
    for patch in parsed.patches:
        bid = resolve_board_id(patch.serial, index, products, exact, product_hint=patch.product_name)
        if bid is None:
            continue
        bucket = by_board.setdefault(bid, {"patches": 0, "events": 0, "files": set()})
        bucket["patches"] += 1
        bucket["files"].add(rel)
    for event in parsed.events:
        product_name, _, _ = product_context(path)
        bid = resolve_board_id(event.serial, index, products, exact, product_hint=product_name)
        if bid is None:
            continue
        bucket = by_board.setdefault(bid, {"patches": 0, "events": 0, "files": set()})
        bucket["events"] += 1
        bucket["files"].add(rel)

print(f"Active boards: {len(boards)}")
print(f"Boards with NetApp data available: {len(by_board)}")
print()
for row in boards:
    bid = row["board_id"]
    label = row["inventory_serial"] or row["serial"]
    hit = by_board.get(bid)
    if hit:
        print(
            f"  {bid:>3} {row['product_name']:<6} {label:<22} "
            f"patches={hit['patches']} events={hit['events']}"
        )
    else:
        print(f"  {bid:>3} {row['product_name']:<6} {label:<22}  (no netapp match)")

conn.close()
