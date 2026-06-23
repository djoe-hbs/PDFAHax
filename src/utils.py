"""
Utility functions for the AutomationAutoTag pipeline.
"""

import os
import json
from datetime import datetime, timezone


def ensure_dir(path: str) -> str:
    """Create directory if it doesn't exist. Returns the path."""
    os.makedirs(path, exist_ok=True)
    return path


def get_timestamp() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def print_extraction_summary(result: dict) -> None:
    """Pretty-print a summary of the extraction result."""
    doc = result.get("document", {})
    blocks = result.get("blocks", [])

    print("\n" + "=" * 60)
    print("  EXTRACTION SUMMARY")
    print("=" * 60)
    print(f"  Source file:  {doc.get('source_file', 'N/A')}")
    print(f"  Total pages:  {doc.get('total_pages', 'N/A')}")
    print(f"  Total blocks: {len(blocks)}")
    print(f"  Parse method: {doc.get('parse_method', 'N/A')}")
    print(f"  Backend:      {doc.get('backend', 'N/A')}")
    print(f"  Timestamp:    {doc.get('extraction_timestamp', 'N/A')}")

    # Count blocks by type
    type_counts: dict[str, int] = {}
    for block in blocks:
        block_type = block.get("type", "unknown")
        type_counts[block_type] = type_counts.get(block_type, 0) + 1

    if type_counts:
        print("\n  Blocks by type:")
        for btype, count in sorted(type_counts.items()):
            bar = "#" * min(count, 40)
            print(f"    {btype:<15} {count:>4}  {bar}")

    # Count blocks per page
    page_counts: dict[int, int] = {}
    for block in blocks:
        page = block.get("page_idx", 0)
        page_counts[page] = page_counts.get(page, 0) + 1

    if page_counts:
        print(f"\n  Blocks per page (showing first 10):")
        for page in sorted(page_counts.keys())[:10]:
            count = page_counts[page]
            bar = "#" * min(count, 40)
            print(f"    Page {page:<4} {count:>4}  {bar}")
        if len(page_counts) > 10:
            print(f"    ... and {len(page_counts) - 10} more pages")

    print("=" * 60 + "\n")


def save_json(data: dict, filepath: str, pretty: bool = True) -> str:
    """Save a dict as JSON to a file. Returns the filepath."""
    ensure_dir(os.path.dirname(filepath))
    indent = 2 if pretty else None
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    return filepath


def load_json(filepath: str) -> dict:
    """Load a JSON file and return its contents as a dict."""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)
