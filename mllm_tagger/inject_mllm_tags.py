"""
Inject MLLM-detected tags into the original PDF.

This is the bridge between the MLLM vision tagger and the existing inject_tags.py
engine. It takes the merged MLLM output (bboxes + tags from the vision model) and
converts it into the blocks_json + tags_json format that inject_tags.py expects,
then calls the existing injection pipeline.

The key insight: inject_tags.py matches content-stream operations (text drawing ops)
to blocks by checking if each op's (x,y) falls inside a block's bbox. The MLLM
provides bounding boxes directly — so we synthesize "blocks" from the MLLM elements
and "tags" from the MLLM's tag assignments, then let the existing injection engine
do its coordinate-matching magic unchanged.

Usage:
    python inject_mllm_tags.py                                  # Use defaults
    python inject_mllm_tags.py --merged merged_tags.json --pdf Test_Input.pdf --output tagged.pdf
"""

import argparse
import json
import os
import sys
from pathlib import Path
from collections import defaultdict

# Add parent dir to path so we can import the existing inject_tags module
sys.path.insert(0, str(Path(__file__).parent.parent))
from inject_tags import inject_tags as _raw_inject_tags

SCRIPT_DIR = Path(__file__).parent
DEFAULT_PDF = SCRIPT_DIR / "Test_Input.pdf"
DEFAULT_MERGED = SCRIPT_DIR / "merged_tags.json"
DEFAULT_OUTPUT = SCRIPT_DIR / "tagged_output.pdf"


def mllm_to_blocks_json(merged: dict) -> dict:
    """Convert MLLM merged output to the blocks_json format inject_tags expects.

    inject_tags expects:
        {
            "document": {"total_pages": N},
            "blocks": [
                {
                    "block_id": int,
                    "page_idx": int,
                    "type": str,        # "text", "heading", "image", "list_item", "table_cell"
                    "text": str,
                    "bbox": [x0, y0, x1, y1],   # PDF page coords, top-left origin
                    "metadata": {"font_size": ..., "font_name": ...},
                    # + optional table_cell fields: is_header, row, col, table_id
                    # + optional list_item fields: metadata.list_marker
                },
                ...
            ]
        }
    """
    blocks = []

    for elem in merged.get("elements", []):
        elem_id = elem.get("id", 0)
        tag = str(elem.get("tag", "P")).upper()
        page_idx = elem.get("page_idx", 0)
        bbox = elem.get("bbox", [0, 0, 0, 0])
        text = elem.get("text", "")

        # Map MLLM tags to the block "type" field that inject_tags uses
        # for matching logic (especially list containers and table containers)
        if tag in ("H1", "H2", "H3", "H4", "H5", "H6"):
            block_type = "heading"
        elif tag == "FIGURE":
            block_type = "image"
        elif tag == "LI":
            block_type = "list_item"
        elif tag in ("TH", "TD"):
            block_type = "table_cell"
        elif tag == "TABLE":
            # Include table container — if TH/TD cells exist, inject_tags
            # builds the proper structure. If not, at least the Table block
            # appears in the tag tree with its text.
            block_type = "text"
        elif tag == "ARTIFACT":
            block_type = "text"  # Will be tagged as Artifact by tags_json
        else:
            block_type = "text"

        block = {
            "block_id": elem_id,
            "page_idx": page_idx,
            "type": block_type,
            "text": text,
            "bbox": [
                max(0, bbox[0] - 5),
                max(0, bbox[1] - 5),
                bbox[2] + 5,
                bbox[3] + 5,
            ],
            "metadata": {
                "font_size": 10.0,  # Default — MLLM doesn't extract this
                "font_name": "",
            },
        }

        # Table cell fields
        if block_type == "table_cell":
            table_info = elem.get("table_info") or {}
            block["is_header"] = tag == "TH"
            block["row"] = table_info.get("row", 0)
            block["col"] = table_info.get("col", 0)
            block["table_id"] = table_info.get("table_id", 0)
            block["row_span"] = table_info.get("row_span", 1)
            block["col_span"] = table_info.get("col_span", 1)

        # List item fields
        if block_type == "list_item":
            marker = elem.get("list_marker", "•")
            block["metadata"]["list_marker"] = marker
            block["metadata"]["marker_x0"] = bbox[0]
            block["metadata"]["body_x0"] = bbox[0] + 15  # Approximate offset

        blocks.append(block)

    total_pages = merged.get("total_pages", 0)
    if not total_pages and blocks:
        total_pages = max(b["page_idx"] for b in blocks) + 1

    return {
        "document": {"total_pages": total_pages},
        "blocks": blocks,
    }


def mllm_to_tags_json(merged: dict) -> list[dict]:
    """Convert MLLM merged output to the tags_json format inject_tags expects.

    inject_tags expects a flat list:
        [
            {
                "block_id": int,
                "tag": str,            # H1, P, Artifact, LI, TH, TD, Figure, ...
                "alt_text": str|null,
                "reading_order": int|null,
                "parent_tag": str|null,  # L for LI, TR for TH/TD, TOC for TOCI
                "role": str,
            },
            ...
        ]
    """
    tags = []

    for elem in merged.get("elements", []):
        tag = str(elem.get("tag", "P"))

        # Skip Table containers — inject_tags builds these itself
        if tag.upper() == "TABLE":
            continue

        tag_entry = {
            "block_id": elem.get("id", 0),
            "tag": tag,
            "alt_text": elem.get("alt_text"),
            "reading_order": elem.get("reading_order"),
            "parent_tag": elem.get("parent_tag"),
            "role": elem.get("role", "paragraph"),
            "notes": elem.get("notes", ""),
        }

        # Footnote info
        fn = elem.get("footnote_info")
        if fn and isinstance(fn, dict):
            tag_entry["marker_number"] = fn.get("marker_number")
            tag_entry["pairs_with_block_id"] = fn.get("pairs_with_id")

        tags.append(tag_entry)

    return tags


def inject_mllm_tags(pdf_path: str, merged_path: str, output_path: str):
    """Main function: convert MLLM output and inject tags into the PDF."""

    print(f"  Loading MLLM output: {merged_path}")
    with open(merged_path, "r", encoding="utf-8") as f:
        merged = json.load(f)

    total_elements = merged.get("total_elements", len(merged.get("elements", [])))
    print(f"  Elements from MLLM: {total_elements}")

    # Convert to inject_tags format
    print(f"  Converting to inject_tags format...")
    blocks_data = mllm_to_blocks_json(merged)
    tags_data = mllm_to_tags_json(merged)

    print(f"  Synthesized blocks: {len(blocks_data['blocks'])}")
    print(f"  Synthesized tags:   {len(tags_data)}")

    # Write temporary files for inject_tags
    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="mllm_inject_")
    blocks_path = os.path.join(tmp_dir, "blocks.json")
    tags_path = os.path.join(tmp_dir, "tags.json")

    with open(blocks_path, "w", encoding="utf-8") as f:
        json.dump(blocks_data, f)
    with open(tags_path, "w", encoding="utf-8") as f:
        json.dump(tags_data, f)

    # Call the existing injection engine
    print(f"\n  Running inject_tags on {pdf_path}...")
    print(f"  Output: {output_path}\n")
    _raw_inject_tags(pdf_path, blocks_path, tags_path, output_path)

    # Cleanup temp files
    os.remove(blocks_path)
    os.remove(tags_path)
    os.rmdir(tmp_dir)


def main():
    parser = argparse.ArgumentParser(
        description="Inject MLLM vision tags into the original PDF"
    )
    parser.add_argument("--pdf", default=str(DEFAULT_PDF),
                        help=f"Original PDF file (default: {DEFAULT_PDF.name})")
    parser.add_argument("--merged", default=str(DEFAULT_MERGED),
                        help=f"Merged MLLM tags JSON (default: {DEFAULT_MERGED.name})")
    parser.add_argument("--output", "-o", default=str(DEFAULT_OUTPUT),
                        help=f"Output tagged PDF (default: {DEFAULT_OUTPUT.name})")
    args = parser.parse_args()

    if not os.path.isfile(args.pdf):
        print(f"Error: PDF not found: {args.pdf}")
        return 1
    if not os.path.isfile(args.merged):
        print(f"Error: Merged tags not found: {args.merged}")
        print(f"  Run merge_outputs.py first to combine per-page MLLM outputs.")
        return 1

    inject_mllm_tags(args.pdf, args.merged, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
