"""
Block normalizer for PyMuPDF output.

Transforms PyMuPDF's raw dict blocks into a canonical structured
format suitable for downstream accessibility tagging.
"""

from __future__ import annotations

import os
import re
from typing import Any
import statistics

from src.utils import get_timestamp


# ── List-marker detection ─────────────────────────────────────────────────────
# Conservative: only split lines that CLEARLY begin a list item. When in doubt,
# a line is treated as normal text (better to miss a list than shred a paragraph).

BULLET_CHARS = "•·▪◦○●‣⁃∙◾◽■□*–—o"
SYMBOL_FONTS = ("symbol", "zapfdingbat", "wingding", "webding", "dingbat")

# Inline marker at the very start of a line: a bullet glyph, or a number/letter/
# roman token followed by '.' or ')', then whitespace and at least one more char.
_INLINE_MARKER_RE = re.compile(
    r"^\s*("
    r"[" + re.escape(BULLET_CHARS) + r"]"      # bullet glyph: • - * ▪ ...
    r"|\d{1,3}[.)]"                             # 1.  2)  12.
    r"|[ivxlcdmIVXLCDM]{1,4}[.)]"               # roman: i.  iv)  III.
    r"|[a-zA-Z][.)]"                            # a)  B.
    r")\s+\S"
)


def _line_text(line: dict) -> str:
    """Full concatenated text of a PyMuPDF line."""
    return "".join(s.get("text", "") for s in line.get("spans", []))


def _line_font(line: dict) -> tuple[float, str]:
    """Largest font size on a line and its font name (mirrors existing logic)."""
    max_size = 0.0
    name = ""
    for span in line.get("spans", []):
        if span.get("text", "").strip():
            size = span.get("size", 0.0)
            if size > max_size:
                max_size = size
                name = span.get("font", name)
    return max_size, name


def _primary_font(line: dict) -> str:
    """Font of the longest non-space span on a line."""
    best, best_len = "", -1
    for span in line.get("spans", []):
        t = span.get("text", "").strip()
        if len(t) > best_len:
            best_len, best = len(t), span.get("font", "")
    return best


def _is_symbol_font(font: str) -> bool:
    f = (font or "").lower()
    return any(k in f for k in SYMBOL_FONTS)


def _is_marker_only_line(line: dict) -> bool:
    """
    True if a line is JUST a list marker (its own line), e.g. a SymbolMT bullet.
    Two ways to qualify: the stripped text is only bullet characters, OR it is a
    short glyph (<=2 chars) drawn in a recognised symbol font.
    """
    text = _line_text(line).strip()
    if not text:
        return False
    if all(ch in BULLET_CHARS or ch.isspace() for ch in text):
        return True
    if len(text) <= 2 and _is_symbol_font(_primary_font(line)):
        return True
    return False


def _inline_marker_split(line: dict):
    """If the line starts with a clear inline marker, return (marker, body); else None."""
    text = _line_text(line)
    m = _INLINE_MARKER_RE.match(text)
    if not m:
        return None
    marker = m.group(1)
    body = text[m.end(1):].strip()
    return marker, body


def _union_bbox(a: list, b: list) -> list:
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def _make_list_item(page_idx, marker, body, item_bbox, marker_x0, body_x0, fs, fn) -> dict:
    """Build a single list_item block. Carries marker + indentation for STEP 2 nesting."""
    return {
        "block_id": -1,
        "page_idx": page_idx,
        "type": "list_item",
        "text": body,
        "bbox": [round(v, 2) for v in item_bbox],
        "metadata": {
            "list_marker": marker,
            "marker_x0": round(marker_x0, 2),
            "body_x0": round(body_x0, 2),
            "font_size": round(fs, 2),
            "font_name": fn,
        },
    }


def sort_blocks_reading_order(blocks: list[dict]) -> list[dict]:
    """
    Sort blocks by reading order using coordinates.
    """
    def sort_key(block: dict) -> tuple:
        page = block.get("page_idx", 0)
        bbox = block.get("bbox", [])
        if bbox and len(bbox) == 4:
            y_pos = bbox[1]
            x_pos = bbox[0]
        else:
            y_pos = 0.0
            x_pos = 0.0
        return (page, y_pos, x_pos)

    return sorted(blocks, key=sort_key)


def get_dominant_font_size(raw_blocks: list[dict]) -> float:
    """Calculate the most common font size (median) in the document to serve as baseline body text size."""
    font_sizes = []
    for block in raw_blocks:
        if block.get("type") == 0:  # text block
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("text", "").strip():
                        font_sizes.append(span.get("size", 0.0))
    if not font_sizes:
        return 12.0
    return statistics.median(font_sizes)


def normalize_blocks(raw_blocks: list[dict], source_file: str) -> dict:
    """
    Transform PyMuPDF's raw dict blocks into our canonical structured format.
    """
    normalized_blocks = []
    
    dominant_size = get_dominant_font_size(raw_blocks)
    heading_threshold = dominant_size * 1.2  # 20% larger than body text
    
    for raw_block in raw_blocks:
        page_idx = raw_block.get("page_idx", 0)
        bbox = raw_block.get("bbox", [])
        
        if raw_block.get("type") == 1:
            # Image block
            normalized_blocks.append({
                "block_id": -1,
                "page_idx": page_idx,
                "type": "image",
                "text": "",
                "bbox": bbox,
                "metadata": {
                    "width": raw_block.get("width"),
                    "height": raw_block.get("height")
                }
            })
        elif raw_block.get("type") == 0:
            # Text block. Walk lines with index so list markers (which may sit on
            # their own line) can pull in the following body line(s). Non-list
            # lines flow through the ORIGINAL same-type merge logic unchanged.
            lines = raw_block.get("lines", [])
            current_sub_block = None  # holds a run of normal text/heading lines

            def flush_sub():
                nonlocal current_sub_block
                if current_sub_block is not None:
                    current_sub_block["text"] = "\n".join(current_sub_block["text_lines"]).strip()
                    del current_sub_block["text_lines"]
                    normalized_blocks.append(current_sub_block)
                    current_sub_block = None

            i, n = 0, len(lines)
            while i < n:
                line = lines[i]
                line_text = _line_text(line)

                if not line_text.strip():
                    i += 1
                    continue

                marker_only = _is_marker_only_line(line)
                inline = None if marker_only else _inline_marker_split(line)

                # ── Case B: marker on its own line, body on the following line ──
                if (marker_only and i + 1 < n
                        and _line_text(lines[i + 1]).strip()
                        and not _is_marker_only_line(lines[i + 1])):
                    flush_sub()
                    marker = line_text.strip()
                    marker_x0 = line["bbox"][0]
                    body_line = lines[i + 1]
                    body_text = _line_text(body_line).strip()
                    body_x0 = body_line["bbox"][0]
                    item_bbox = _union_bbox(line["bbox"], body_line["bbox"])
                    fs, fn = _line_font(body_line)
                    i += 2
                    # Pull in wrapped continuation lines (no marker, ~same body x0)
                    while i < n:
                        nxt = lines[i]
                        nt = _line_text(nxt).strip()
                        if not nt:
                            i += 1
                            continue
                        if _is_marker_only_line(nxt) or _inline_marker_split(nxt):
                            break
                        if abs(nxt["bbox"][0] - body_x0) <= 6:
                            body_text += " " + nt
                            item_bbox = _union_bbox(item_bbox, nxt["bbox"])
                            i += 1
                        else:
                            break
                    normalized_blocks.append(_make_list_item(
                        page_idx, marker, body_text, item_bbox, marker_x0, body_x0, fs, fn))
                    continue

                # ── Case A: inline marker at the start of the line ──
                if inline is not None:
                    flush_sub()
                    marker, body_text = inline
                    marker_x0 = line["bbox"][0]
                    # Calculate true body_x0 from spans
                    body_x0 = marker_x0
                    for span in line.get("spans", []):
                        span_text = span.get("text", "")
                        if marker in span_text:
                            continue # Skip the span with the marker
                        if span_text.strip():
                            body_x0 = span["bbox"][0]
                            break
                    
                    # Fallback if body_x0 wasn't found in a separate span
                    if body_x0 == marker_x0:
                        body_x0 = marker_x0 + 10 # heuristic fallback
                        
                    item_bbox = list(line["bbox"])
                    fs, fn = _line_font(line)
                    i += 1
                    while i < n:
                        nxt = lines[i]
                        nt = _line_text(nxt).strip()
                        if not nt:
                            i += 1
                            continue
                        if _is_marker_only_line(nxt) or _inline_marker_split(nxt):
                            break
                        if nxt["bbox"][0] >= marker_x0 + 2:  # indented under the marker
                            body_text += " " + nt
                            item_bbox = _union_bbox(item_bbox, nxt["bbox"])
                            i += 1
                        else:
                            break
                    normalized_blocks.append(_make_list_item(
                        page_idx, marker, body_text, item_bbox, marker_x0, body_x0, fs, fn))
                    continue

                # ── Normal line: ORIGINAL heading/text merge logic ──
                max_line_font_size, line_font_name = _line_font(line)
                line_type = "heading" if max_line_font_size >= heading_threshold else "text"

                if (current_sub_block is None or current_sub_block["type"] != line_type):
                    flush_sub()
                    current_sub_block = {
                        "block_id": -1,
                        "page_idx": page_idx,
                        "type": line_type,
                        "text_lines": [line_text],
                        "bbox": list(line.get("bbox", bbox)),
                        "metadata": {
                            "font_size": round(max_line_font_size, 2),
                            "font_name": line_font_name
                        }
                    }
                else:
                    current_sub_block["text_lines"].append(line_text)
                    line_bbox = line.get("bbox", [])
                    if len(line_bbox) == 4 and len(current_sub_block["bbox"]) == 4:
                        current_sub_block["bbox"][0] = min(current_sub_block["bbox"][0], line_bbox[0])
                        current_sub_block["bbox"][1] = min(current_sub_block["bbox"][1], line_bbox[1])
                        current_sub_block["bbox"][2] = max(current_sub_block["bbox"][2], line_bbox[2])
                        current_sub_block["bbox"][3] = max(current_sub_block["bbox"][3], line_bbox[3])

                    if max_line_font_size > current_sub_block["metadata"]["font_size"]:
                        current_sub_block["metadata"]["font_size"] = round(max_line_font_size, 2)
                        current_sub_block["metadata"]["font_name"] = line_font_name
                i += 1

            flush_sub()

    # Sort
    normalized_blocks = sort_blocks_reading_order(normalized_blocks)
    
    # Assign IDs
    for idx, block in enumerate(normalized_blocks):
        block["block_id"] = idx
        
    total_pages = max([b["page_idx"] for b in normalized_blocks]) + 1 if normalized_blocks else 0
    
    return {
        "document": {
            "source_file": os.path.basename(source_file),
            "total_pages": total_pages,
            "extraction_timestamp": get_timestamp(),
            "backend": "pymupdf"
        },
        "blocks": normalized_blocks
    }
