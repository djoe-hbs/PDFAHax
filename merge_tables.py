"""
Merge Docling table regions into the PyMuPDF structured-blocks JSON.

Strategy (decided after confirming PyMuPDF does NOT segment tables into cells):
  For every detected table region, DELETE the PyMuPDF blocks that fall inside it
  (those blocks are either the whole table mashed into one, or fragments that cut
  across cells) and REPLACE them with one synthetic "table_cell" block per Docling
  cell. Blocks outside any table region are left completely untouched.

COORDINATE ALIGNMENT (important):
  Docling cell bboxes use CoordOrigin.TOPLEFT, and our comparison showed they
  match PyMuPDF's page coordinates 1:1 (same origin, same scale, sub-pixel
  agreement). PyMuPDF block bboxes are also top-left page coordinates.
  inject_tags.py converts content-stream draw positions (PDF bottom-left origin)
  into top-left page coords via pdf_y_to_page_y BEFORE comparing to block bboxes.
  Therefore the synthetic cell bboxes — taken verbatim from Docling, already
  top-left — are in exactly the coordinate system the injector's matcher expects.
  No transform is applied here; doing so would mis-align them.

Synthetic table_cell block schema:
  {
    "block_id": <reassigned>,
    "page_idx": int,
    "type": "table_cell",
    "text": str,
    "bbox": [x0, y0, x1, y1],   # top-left page coords (from Docling)
    "table_id": int,
    "row": int, "col": int,
    "row_span": int, "col_span": int,
    "is_header": bool,
    "metadata": {"source": "docling"}
  }
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.normalizer import sort_blocks_reading_order


def _center(bbox):
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _point_in_bbox(x, y, bbox, pad=2.0):
    return (bbox[0] - pad <= x <= bbox[2] + pad and
            bbox[1] - pad <= y <= bbox[3] + pad)


def merge_tables(blocks_json_path: str, regions_json_path: str, output_path: str) -> dict:
    with open(blocks_json_path, "r", encoding="utf-8") as f:
        blocks_data = json.load(f)
    with open(regions_json_path, "r", encoding="utf-8") as f:
        regions = json.load(f)

    blocks = blocks_data["blocks"]

    # Group table regions by page for quick lookup
    regions_by_page = {}
    for tbl in regions:
        regions_by_page.setdefault(tbl["page_idx"], []).append(tbl)

    # 1. Decide which existing blocks fall inside a table region -> delete them.
    kept_blocks = []
    deleted_per_table = {tbl["table_id"]: 0 for tbl in regions}

    for block in blocks:
        bbox = block.get("bbox")
        page = block.get("page_idx", 0)
        page_regions = regions_by_page.get(page, [])

        inside_table_id = None
        if bbox and len(bbox) == 4:
            cx, cy = _center(bbox)
            for tbl in page_regions:
                if _point_in_bbox(cx, cy, tbl["bbox"]):
                    inside_table_id = tbl["table_id"]
                    break

        if inside_table_id is None:
            kept_blocks.append(block)
        else:
            deleted_per_table[inside_table_id] += 1

    # 2. Build synthetic table_cell blocks, one per Docling cell.
    synthetic = []
    created_per_table = {}
    skipped_no_bbox = {}

    for tbl in regions:
        tid = tbl["table_id"]
        created = 0
        skipped = 0
        for cell in tbl["cells"]:
            cbbox = cell.get("bbox")
            if not cbbox or len(cbbox) != 4:
                skipped += 1
                continue
            synthetic.append({
                "block_id": -1,
                "page_idx": tbl["page_idx"],
                "type": "table_cell",
                "text": cell.get("text", ""),
                "bbox": list(cbbox),
                "table_id": tid,
                "row": cell.get("row", 0),
                "col": cell.get("col", 0),
                "row_span": cell.get("row_span", 1),
                "col_span": cell.get("col_span", 1),
                "is_header": bool(cell.get("is_header", False)),
                "metadata": {"source": "docling"},
            })
            created += 1
        created_per_table[tid] = created
        skipped_no_bbox[tid] = skipped

    # 3. Combine, re-sort into reading order, re-assign block_ids.
    merged = kept_blocks + synthetic
    merged = sort_blocks_reading_order(merged)
    for idx, block in enumerate(merged):
        block["block_id"] = idx

    blocks_data["blocks"] = merged
    if "document" in blocks_data:
        blocks_data["document"]["table_merge"] = {
            "tables": len(regions),
            "synthetic_cells": len(synthetic),
        }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(blocks_data, f, indent=2, ensure_ascii=False)

    # 4. Report counts so they can be checked against the real tables.
    print(f"[merge_tables] Merged {len(regions)} table(s) into blocks")
    print(f"  Input blocks:  {len(blocks)}")
    print(f"  Deleted (inside table regions): {sum(deleted_per_table.values())}")
    print(f"  Synthetic table cells created:  {len(synthetic)}")
    print(f"  Output blocks: {len(merged)}")
    print(f"  {'-'*52}")
    for tbl in regions:
        tid = tbl["table_id"]
        grid = tbl["num_rows"] * tbl["num_cols"]
        line = (f"  Table {tid} (page {tbl['page_idx']}): "
                f"{tbl['num_rows']}r x {tbl['num_cols']}c, grid={grid} | "
                f"cells created={created_per_table[tid]} | "
                f"PyMuPDF blocks deleted={deleted_per_table[tid]}")
        if skipped_no_bbox[tid]:
            line += f" | SKIPPED(no bbox)={skipped_no_bbox[tid]}"
        print(line)
    print(f"  {'-'*52}")
    print(f"  [OK] Written -> {os.path.abspath(output_path)}")
    print(f"\n  Note: grid = rows*cols is the UPPER bound; real cell count is")
    print(f"  lower when cells span multiple rows/cols (merged cells).")

    return blocks_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Merge Docling table regions into structured_blocks.json "
                    "by replacing in-table PyMuPDF blocks with per-cell blocks."
    )
    parser.add_argument("blocks_json", help="structured_blocks.json from extraction")
    parser.add_argument("regions_json", help="table_regions.json from detect_tables.py")
    parser.add_argument("--output", default=None,
                        help="Output path (default: <blocks_json> with _merged suffix)")
    args = parser.parse_args()

    out = args.output
    if out is None:
        base, ext = os.path.splitext(args.blocks_json)
        out = f"{base}_merged{ext}"

    merge_tables(args.blocks_json, args.regions_json, out)
