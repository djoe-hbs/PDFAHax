"""
Add a PDF document outline (bookmarks) to an already-tagged PDF.

Standalone pipeline step — runs AFTER inject_tags.py. Builds the outline from the
heading tags (H1..H6) the AI assigned, nested by heading level, with each
bookmark's destination set to its page and the heading's top-y (so clicking
scrolls to the heading, not just the page top).

Idempotent: any existing outline is cleared and rebuilt, so re-running is safe.

Usage:
    python add_bookmarks.py tagged_output.pdf \
        --blocks output/Final_Test_Input/Final_Test_Input_structured_blocks_merged.json \
        --tags tags_merged.json
    # (defaults to overwriting the input PDF in place)
"""

import argparse
import json
import os
import sys
import tempfile

import pikepdf
from pikepdf import OutlineItem, PageLocation

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inject_tags import normalize_tags

HEADING_LEVELS = {"H1": 1, "H2": 2, "H3": 3, "H4": 4, "H5": 5, "H6": 6}

# Map the source-font glyphs that PyMuPDF couldn't decode (and smart punctuation)
# to clean ASCII so the bookmark panel reads correctly.
_CLEAN = {
    "�": "'",   # replacement char — in this corpus it stands for an apostrophe
    "’": "'", "‘": "'",
    "“": '"', "”": '"',
    "–": "-", "—": "-",
}


def clean_title(text: str) -> str:
    text = " ".join(text.split())
    for bad, good in _CLEAN.items():
        text = text.replace(bad, good)
    return text


def collect_headings(blocks_path: str, tags_path: str):
    """Return heading dicts ordered for outlining: level, title, page, top_y(bbox)."""
    blocks = json.load(open(blocks_path, encoding="utf-8"))["blocks"]
    bmap = {b["block_id"]: b for b in blocks}
    tags = normalize_tags(json.load(open(tags_path, encoding="utf-8")))

    heads = []
    for t in tags:
        lvl = HEADING_LEVELS.get(str(t.get("tag", "")).upper())
        if not lvl:
            continue
        b = bmap.get(t["block_id"])
        if not b or not b.get("bbox"):
            continue
        heads.append({
            "level": lvl,
            "title": clean_title(b["text"]),
            "page": b["page_idx"],
            "top": b["bbox"][1],          # PyMuPDF top-left y (top edge)
            "order": t.get("reading_order") if t.get("reading_order") is not None else 1e9,
            "y_sort": b["bbox"][1],
        })

    heads.sort(key=lambda h: (h["order"], h["page"], h["y_sort"]))
    return heads


def add_bookmarks(pdf_path: str, blocks_path: str, tags_path: str, output_path: str) -> int:
    heads = collect_headings(blocks_path, tags_path)
    if not heads:
        print("  [add_bookmarks] No headings found — nothing to do.")
        return 0

    pdf = pikepdf.Pdf.open(pdf_path)
    n_pages = len(pdf.pages)

    # Page heights for top-left -> PDF (bottom-left) y conversion of the destination.
    page_heights = []
    for page in pdf.pages:
        mb = page.obj.get("/MediaBox")
        page_heights.append(float(mb[3]) if mb else 792.0)

    with pdf.open_outline() as outline:
        outline.root.clear()  # idempotent: drop any previous outline

        stack = []  # list of (level, OutlineItem)
        made = 0
        for h in heads:
            page_idx = h["page"]
            if page_idx >= n_pages:
                continue
            top_pdf = page_heights[page_idx] - h["top"]  # PDF-coord y of heading top
            item = OutlineItem(
                h["title"], page_idx,
                page_location=PageLocation.XYZ, top=top_pdf,
            )

            # Nest by level: child of the nearest shallower open item.
            while stack and stack[-1][0] >= h["level"]:
                stack.pop()
            if stack:
                stack[-1][1].children.append(item)
            else:
                outline.root.append(item)
            stack.append((h["level"], item))
            made += 1

    # Save (in place via temp swap if needed)
    in_place = os.path.abspath(output_path) == os.path.abspath(pdf_path)
    if in_place:
        fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(os.path.abspath(pdf_path)))
        os.close(fd)
        pdf.save(tmp)
        pdf.close()
        os.replace(tmp, pdf_path)
    else:
        pdf.save(output_path)
        pdf.close()

    print(f"  [add_bookmarks] {made} bookmark(s) written -> {os.path.abspath(output_path)}")
    return made


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Add heading-based bookmarks to a tagged PDF.")
    ap.add_argument("pdf_path", help="Tagged PDF (output of inject_tags.py)")
    ap.add_argument("--blocks", required=True, help="structured_blocks_merged.json")
    ap.add_argument("--tags", required=True, help="tags_merged.json")
    ap.add_argument("--output", default=None, help="Output PDF (default: overwrite input)")
    args = ap.parse_args()

    out = args.output or args.pdf_path
    add_bookmarks(args.pdf_path, args.blocks, args.tags, out)
