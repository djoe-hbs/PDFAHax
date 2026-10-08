"""
Inject MLLM-extracted text into a scanned PDF as invisible OCR text.

This script takes the text and bounding boxes from merged_tags.json and injects
them as invisible, selectable text (render_mode=3) into the PDF. This allows 
inject_tags.py to successfully match and tag the document even if the original
PDF was purely scanned images.

Usage:
    python inject_invisible_text.py --merged merged_tags.json --pdf Test_Input.pdf --output Test_Input_ocr.pdf
"""

import argparse
import json
import os
import sys
import fitz

def inject_invisible_text(pdf_path: str, merged_path: str, output_path: str):
    print(f"  Loading PDF: {pdf_path}")
    doc = fitz.open(pdf_path)
    
    print(f"  Loading MLLM tags: {merged_path}")
    with open(merged_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    elements = data.get("elements", [])
    injected_count = 0
    
    for e in elements:
        text = (e.get("text") or "").strip()
        bbox = e.get("bbox")
        tag = e.get("tag", "").upper()
        
        # We don't inject text for FIGUREs or empty text
        if text and text != "None" and bbox and tag != "FIGURE":
            page_idx = e.get("page_idx", 0)
            if page_idx < len(doc):
                page = doc[page_idx]
                
                rect = fitz.Rect(bbox[0], bbox[1], bbox[2], bbox[3])
                
                # Strategy: use insert_textbox with tiny fontsize (1pt) so text
                # ALWAYS fits and spreads Tm ops across the full rect area.
                # This makes Acrobat highlight the correct visual region.
                # render_mode=3 = invisible but selectable/taggable.
                rc = page.insert_textbox(
                    rect,
                    text,
                    fontsize=1,
                    render_mode=3,
                    color=(0, 0, 0)
                )
                
                if rc < 0:
                    # Text fit — good, Tm ops are spread across the rect
                    injected_count += 1
                else:
                    # Fallback: insert_text at a point inside the rect
                    fontsize = max(1, min(rect.height * 0.7, 8))
                    page.insert_text(
                        rect.bl,
                        text,
                        fontsize=fontsize,
                        render_mode=3,
                        color=(0, 0, 0)
                    )
                    injected_count += 1
                
    doc.save(output_path)
    doc.close()
    
    print(f"  Injected {injected_count} invisible text blocks.")
    print(f"  [OK] Saved OCR-ready PDF to: {output_path}")

def main():
    parser = argparse.ArgumentParser(description="Inject invisible text into a scanned PDF.")
    parser.add_argument("--pdf", required=True, help="Original PDF file")
    parser.add_argument("--merged", required=True, help="Merged MLLM tags JSON")
    parser.add_argument("--output", "-o", required=True, help="Output PDF with invisible text")
    args = parser.parse_args()

    if not os.path.isfile(args.pdf):
        print(f"Error: PDF not found: {args.pdf}")
        return 1
    if not os.path.isfile(args.merged):
        print(f"Error: Merged tags not found: {args.merged}")
        return 1

    inject_invisible_text(args.pdf, args.merged, args.output)
    return 0

if __name__ == "__main__":
    sys.exit(main() or 0)
