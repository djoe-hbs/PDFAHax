"""
Block normalizer for PyMuPDF output.

Transforms PyMuPDF's raw dict blocks into a canonical structured
format suitable for downstream accessibility tagging.
"""

from __future__ import annotations

import os
from typing import Any
import statistics

from src.utils import get_timestamp


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
            # Text block
            current_sub_block = None
            
            for line in raw_block.get("lines", []):
                line_text = ""
                max_line_font_size = 0.0
                line_font_name = ""
                
                for span in line.get("spans", []):
                    span_text = span.get("text", "")
                    line_text += span_text
                    if span_text.strip():
                        size = span.get("size", 0.0)
                        if size > max_line_font_size:
                            max_line_font_size = size
                            line_font_name = span.get("font", line_font_name)
                
                if not line_text.strip():
                    continue
                    
                line_type = "heading" if max_line_font_size >= heading_threshold else "text"
                
                if (current_sub_block is None or current_sub_block["type"] != line_type):
                    if current_sub_block is not None:
                        current_sub_block["text"] = "\n".join(current_sub_block["text_lines"]).strip()
                        del current_sub_block["text_lines"]
                        normalized_blocks.append(current_sub_block)
                        
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

            if current_sub_block is not None:
                current_sub_block["text"] = "\n".join(current_sub_block["text_lines"]).strip()
                del current_sub_block["text_lines"]
                normalized_blocks.append(current_sub_block)
            
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
