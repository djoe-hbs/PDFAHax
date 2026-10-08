#!/usr/bin/env python3
"""
build_tagged_pdf.py — Direct tagged PDF builder for scanned documents.

Replaces the fragile 3-step pipeline (inject_invisible_text → inject_mllm_tags
→ inject_tags) with a single-pass approach that:

1. Wraps existing page content (the scanned image) as /Artifact
2. For each MLLM element, injects invisible text DIRECTLY into the content
   stream already wrapped in BMC/EMC with the correct MCID and tag
3. Builds a proper StructTreeRoot pointing to those MCIDs

No coordinate matching needed — positions come directly from merged_tags.json.
The invisible text is spread across each element's bounding box so that
Acrobat highlights the correct visual area when a tag is clicked.

Usage:
    python build_tagged_pdf.py --pdf Test_Input.pdf --merged merged_tags.json --output tagged.pdf
"""

import argparse
import json
import math
import sys
import os

import pikepdf
from pikepdf import Dictionary, Name, Array, String


# ── Helpers ──────────────────────────────────────────────────────────────────

def tag_name_to_pdf(tag: str) -> str:
    """Convert MLLM tag name to PDF standard structure type name."""
    mapping = {
        "H1": "H1", "H2": "H2", "H3": "H3", "H4": "H4", "H5": "H5", "H6": "H6",
        "P": "P",
        "L": "L", "LI": "LI",
        "TABLE": "Table", "TR": "TR", "TH": "TH", "TD": "TD",
        "FIGURE": "Figure", "CAPTION": "Caption",
        "ARTIFACT": "Artifact",
        "SPAN": "Span", "TOC": "TOC", "TOCI": "TOCI",
        "NOTE": "Note", "REFERENCE": "Reference",
        "FORMULA": "Formula", "FORM": "Form",
    }
    return mapping.get(tag.upper(), tag)


def escape_pdf_string(s: str) -> str:
    """Escape backslashes and parentheses for PDF string literals."""
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

def normalize_text(text: str) -> str:
    """Normalize common unicode characters (smart quotes, bullets, etc.) to ASCII.
    This prevents mojibake (like 'SSAâs') when writing to standard PDF fonts that expect Latin-1/WinAnsi.
    """
    if not text:
        return text
    replacements = {
        '\u2018': "'", '\u2019': "'",   # smart single quotes
        '\u201c': '"', '\u201d': '"',   # smart double quotes
        '\u2013': '-', '\u2014': '-',   # en/em dashes
        '\u2022': '*',                  # bullet
        '\u2026': '...',                # ellipsis
        '\u00a0': ' ',                  # non-breaking space
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    # Strip any remaining non-ascii safely
    return text.encode('ascii', 'ignore').decode('ascii')


# ── Content Stream Generation ────────────────────────────────────────────────

FONT_NAME = "/MLLMHelv"


def generate_text_ops(text: str, bbox: list, page_height: float) -> bytes:
    """Generate invisible text ops (BT…ET) that FILL the bbox area.

    The text is broken into multiple lines spread vertically across the
    bounding box so that Acrobat's tag-highlight covers the full visual
    region — not just a single point.
    """
    x0, y0_tl, x1, y1_tl = bbox  # top-left origin coords from merged_tags.json

    # Convert to PDF coordinates (bottom-left origin, y increases upward)
    pdf_x0 = x0
    pdf_y_top = page_height - y0_tl
    pdf_y_bot = page_height - y1_tl
    bbox_width = x1 - x0
    bbox_height = pdf_y_top - pdf_y_bot

    if bbox_height <= 0 or bbox_width <= 0:
        return b""

    text = normalize_text(text)

    # Pick a fontsize that fills the box with multiple lines
    fontsize = min(max(4, bbox_height / 4), 10)
    line_height = fontsize * 1.25
    char_width = fontsize * 0.45  # conservative estimate for Helvetica
    chars_per_line = max(1, int(bbox_width / char_width))

    import textwrap
    # Split text into lines at word boundaries
    lines = textwrap.wrap(text, width=chars_per_line)

    parts = ["BT\n"]
    parts.append(f"  {FONT_NAME} {fontsize:.1f} Tf\n")
    parts.append("  3 Tr\n")  # render mode 3 = invisible

    # Trick to force Acrobat to highlight the ENTIRE bounding box:
    # Drop invisible spaces at the top-left and bottom-right corners.
    parts.append(f"  1 0 0 1 {pdf_x0:.2f} {pdf_y_top - 1:.2f} Tm\n")
    parts.append(f"  ( ) Tj\n")
    parts.append(f"  1 0 0 1 {x1 - 2:.2f} {pdf_y_bot + 1:.2f} Tm\n")
    parts.append(f"  ( ) Tj\n")

    # Now print the actual text
    y = pdf_y_top - fontsize
    for line in lines:
        if y < pdf_y_bot - 2:
            break
        escaped = escape_pdf_string(line)
        parts.append(f"  1 0 0 1 {pdf_x0:.2f} {y:.2f} Tm\n")
        parts.append(f"  ({escaped}) Tj\n")
        y -= line_height

    parts.append("ET\n")
    return "".join(parts).encode()


# ── Main Pipeline ────────────────────────────────────────────────────────────

def build_tagged_pdf(pdf_path: str, merged_path: str, output_path: str):
    """Build a tagged PDF directly from MLLM output — single pass."""

    print(f"  Loading PDF: {pdf_path}")
    print(f"  Loading MLLM tags: {merged_path}")

    with open(merged_path, "r", encoding="utf-8") as f:
        merged = json.load(f)

    elements = merged.get("elements", [])

    # Group elements by page
    pages_elements: dict[int, list] = {}
    for elem in elements:
        pi = elem.get("page_idx", 0)
        pages_elements.setdefault(pi, []).append(elem)

    pdf = pikepdf.Pdf.open(pdf_path)

    # Per-page MCID tracking for structure tree
    page_mcid_map: dict[int, list[tuple[int, dict]]] = {}

    for page_idx in range(len(pdf.pages)):
        page = pdf.pages[page_idx]
        page_elems = pages_elements.get(page_idx, [])
        if not page_elems:
            continue

        # Page dimensions
        mb = page.get("/MediaBox")
        page_height = float(mb[3]) - float(mb[1]) if mb else 792.0

        # Read existing content stream (the scan image)
        try:
            existing_ops = pikepdf.parse_content_stream(page)
            existing_bytes = pikepdf.unparse_content_stream(existing_ops)
        except Exception:
            existing_bytes = b""

        # Register font
        res = page.get("/Resources")
        if res is None:
            page["/Resources"] = Dictionary()
            res = page["/Resources"]
        fonts = res.get("/Font")
        if fonts is None:
            res["/Font"] = Dictionary()
            fonts = res["/Font"]
        if Name(FONT_NAME) not in fonts:
            fonts[Name(FONT_NAME)] = pdf.make_indirect(Dictionary({
                "/Type": Name("/Font"),
                "/Subtype": Name("/Type1"),
                "/BaseFont": Name("/Helvetica"),
            }))

        # ── Build new content stream ────────────────────────────────────
        parts: list[bytes] = []

        # 1. Wrap existing content (scan image) as Artifact inside q/Q
        parts.append(b"q\n")
        parts.append(b"/Artifact BMC\n")
        parts.append(existing_bytes)
        parts.append(b"\nEMC\n")
        parts.append(b"Q\n")

        # 2. Tagged text blocks
        mcid_list: list[tuple[int, dict]] = []
        mcid = 0

        for elem in page_elems:
            tag_raw = str(elem.get("tag", "P")).upper()
            text = (elem.get("text") or "").strip()
            bbox = elem.get("bbox", [0, 0, 0, 0])

            # Artifacts are already handled above
            if tag_raw == "ARTIFACT":
                continue

            # Skip elements with no usable bbox
            if not bbox or all(v == 0 for v in bbox):
                continue

            pdf_tag = tag_name_to_pdf(tag_raw)

            # ── BDC with MCID ──
            parts.append(f"/{pdf_tag} <</MCID {mcid}>> BDC\n".encode())

            if text:
                parts.append(generate_text_ops(text, bbox, page_height))
            else:
                # For elements without text (e.g. Figure): place a single
                # invisible space so the MCID has content Acrobat can anchor.
                pdf_y = page_height - bbox[3]
                parts.append(
                    f"BT {FONT_NAME} 1 Tf 3 Tr "
                    f"1 0 0 1 {bbox[0]:.2f} {pdf_y:.2f} Tm "
                    f"( ) Tj ET\n".encode()
                )

            parts.append(b"EMC\n")
            mcid_list.append((mcid, elem))
            mcid += 1

        # Replace content stream
        page["/Contents"] = pdf.make_stream(b"".join(parts))
        page_mcid_map[page_idx] = mcid_list

    # ── Structure Tree ──────────────────────────────────────────────────
    print("  Building structure tree...")
    _build_structure_tree(pdf, page_mcid_map)

    # ── Metadata flags ──────────────────────────────────────────────────
    pdf.Root["/MarkInfo"] = Dictionary({"/Marked": True})
    pdf.Root["/Lang"] = String("en-US")  # Required by PDF/UA

    vp = pdf.Root.get("/ViewerPreferences")
    if isinstance(vp, Dictionary):
        vp["/DisplayDocTitle"] = True
    else:
        pdf.Root["/ViewerPreferences"] = Dictionary({"/DisplayDocTitle": True})
        
    for p in pdf.pages:
        p["/Tabs"] = Name("/S")  # Required by PDF/UA for tag reading order

    # Save
    pdf.save(output_path)

    # Stats
    total = sum(len(v) for v in page_mcid_map.values())
    print(f"\n  [OK] Tagged PDF saved to: {output_path}")
    print(f"  Total elements tagged: {total}")
    for pi in sorted(page_mcid_map):
        tags = [tag_name_to_pdf(str(e.get("tag", "?"))) for _, e in page_mcid_map[pi]]
        print(f"  Page {pi}: {len(tags)} tags — {', '.join(tags)}")


def _build_structure_tree(pdf, page_mcid_map):
    """Build a flat Document → elements structure tree with ParentTree."""

    doc_kids = []
    nums_array = Array()
    sp_key = 0  # StructParents key

    for page_idx in sorted(page_mcid_map.keys()):
        mcid_list = page_mcid_map[page_idx]
        page_obj = pdf.pages[page_idx].obj

        # Every page with tagged content needs a StructParents entry
        page_obj["/StructParents"] = sp_key

        # ParentTree entry for this page: array mapping MCID → struct element
        parent_array = Array()

        for mcid, elem in mcid_list:
            pdf_tag = tag_name_to_pdf(str(elem.get("tag", "P")))

            # Marked-content reference
            mcr = Dictionary({
                "/Type": Name("/MCR"),
                "/MCID": mcid,
                "/Pg": page_obj,
            })

            # Struct element
            se = pdf.make_indirect(Dictionary({
                "/Type": Name("/StructElem"),
                "/S": Name(f"/{pdf_tag}"),
                "/K": mcr,
            }))

            # Alt text for figures
            alt = elem.get("alt_text")
            if alt and pdf_tag == "Figure":
                se["/Alt"] = String(alt)

            doc_kids.append(se)
            parent_array.append(se)

        nums_array.append(sp_key)
        nums_array.append(pdf.make_indirect(parent_array))
        sp_key += 1

    # Document element
    doc_elem = pdf.make_indirect(Dictionary({
        "/Type": Name("/StructElem"),
        "/S": Name("/Document"),
        "/K": Array(doc_kids),
    }))
    for kid in doc_kids:
        kid["/P"] = doc_elem

    # Parent tree
    parent_tree = pdf.make_indirect(Dictionary({
        "/Type": Name("/NumberTree"),
        "/Nums": nums_array,
    }))

    # StructTreeRoot
    root = pdf.make_indirect(Dictionary({
        "/Type": Name("/StructTreeRoot"),
        "/K": doc_elem,
        "/ParentTree": parent_tree,
        "/ParentTreeNextKey": sp_key,
    }))
    doc_elem["/P"] = root
    pdf.Root["/StructTreeRoot"] = root


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Build a tagged PDF directly from MLLM output (single pass)")
    parser.add_argument("--pdf", required=True, help="Original PDF (source scan)")
    parser.add_argument("--merged", required=True, help="Merged MLLM tags JSON")
    parser.add_argument("--output", "-o", required=True, help="Output tagged PDF")
    args = parser.parse_args()

    if not os.path.isfile(args.pdf):
        print(f"Error: PDF not found: {args.pdf}")
        return 1
    if not os.path.isfile(args.merged):
        print(f"Error: Tags not found: {args.merged}")
        return 1

    build_tagged_pdf(args.pdf, args.merged, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
