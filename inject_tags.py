"""
PDF Tag Injector — Multi-Page Version

Takes the original PDF, the structured blocks JSON (from PyMuPDF extraction),
and the AI's tag JSON, then injects real PDF/UA structure tags directly into
the PDF's binary structure using pikepdf.

The result is a tagged PDF that Adobe Acrobat will recognize in its Tags panel.

Architecture (per page):
  1. Parse content stream to find all text-drawing operations and their positions
  2. Match each operation to a block (by coordinate overlap)
  3. Wrap matched operations with BDC/EMC markers carrying MCIDs
  4. Build the StructTreeRoot, StructElems, and ParentTree across ALL pages
  5. Save — zero visual changes, only invisible logical structure added
"""

import argparse
import json
import os
from decimal import Decimal
from collections import defaultdict

import pikepdf
from pikepdf import Name, Dictionary, Array, String


# ── Helpers ──────────────────────────────────────────────────────────────────

def pdf_y_to_page_y(pdf_y, page_height):
    """Convert PDF coordinate system Y (origin bottom-left) to page Y (origin top-left)."""
    return page_height - pdf_y


def tag_name_to_pdf_name(tag: str) -> str:
    """Convert our tag names (H1, P, LI) to PDF standard structure type names."""
    mapping = {
        "H1": "H1", "H2": "H2", "H3": "H3", "H4": "H4", "H5": "H5", "H6": "H6",
        "P": "P",
        "L": "L",
        "LI": "LI",
        "LBODY": "LBody",
        "LBL": "Lbl",
        "TABLE": "Table", "TR": "TR", "TH": "TH", "TD": "TD",
        "FIGURE": "Figure",
        "CAPTION": "Caption",
        "ARTIFACT": "Artifact",
        "SPAN": "Span",
        "TOC": "TOC",
        "TOCI": "TOCI",
        "REFERENCE": "Reference",
        "LINK": "Link",
        "NOTE": "Note",
    }
    return mapping.get(tag.upper(), tag)


# ── Content Stream Analysis ──────────────────────────────────────────────────

def analyze_content_stream(page):
    """
    Walk the content stream and extract the position of every text-drawing
    operation. Returns instructions list and text_positions list.
    """
    instructions = pikepdf.parse_content_stream(page)
    text_positions = []

    in_text_block = False
    current_x = 0.0
    current_y = 0.0
    current_font_size = 12.0
    line_x = 0.0
    line_y = 0.0

    for idx, (operands, operator) in enumerate(instructions):
        op = str(operator)

        if op == "BT":
            in_text_block = True
            current_x = 0.0
            current_y = 0.0
            line_x = 0.0
            line_y = 0.0
        elif op == "ET":
            in_text_block = False
        elif not in_text_block:
            continue

        if op == "Tm":
            if len(operands) >= 6:
                current_font_size = float(operands[0])
                current_x = float(operands[4])
                current_y = float(operands[5])
                line_x = current_x
                line_y = current_y
        elif op == "Td" or op == "TD":
            if len(operands) >= 2:
                tx = float(operands[0]) * current_font_size
                ty = float(operands[1]) * current_font_size
                current_x = line_x + tx
                current_y = line_y + ty
                line_x = current_x
                line_y = current_y
        elif op == "T*":
            pass
        elif op in ("Tj", "TJ", "'", '"'):
            text_positions.append({
                "idx": idx,
                "x": current_x,
                "y": current_y,
                "font_size": current_font_size,
            })

    return instructions, text_positions


def match_operations_to_blocks(text_positions, page_blocks, page_height):
    """
    For each text-drawing operation, find which block it belongs to
    by checking if the operation's (x, y) falls inside the block's bbox.

    Returns a dict: block_id -> list of instruction indices
    """
    block_ops = defaultdict(list)

    for pos in text_positions:
        x = pos["x"]
        y_top_down = pdf_y_to_page_y(pos["y"], page_height)

        for block in page_blocks:
            bbox = block.get("bbox")
            if not bbox or len(bbox) != 4:
                continue

            if (bbox[0] - 5 <= x <= bbox[2] + 5 and
                bbox[1] - 5 <= y_top_down <= bbox[3] + 5):
                block_ops[block["block_id"]].append(pos["idx"])
                break

    return block_ops


# ── Content Stream Injection ─────────────────────────────────────────────────

def inject_marked_content(instructions, block_ops, tags_map, mcid_start=0):
    """
    Insert BDC/EMC markers around groups of text operations.
    mcid_start allows continuing MCID numbering across pages.

    Returns: new_instructions, block_to_mcid dict
    """
    markers = {}

    for block_id, op_indices in block_ops.items():
        if not op_indices:
            continue
        tag_info = tags_map.get(block_id)
        if not tag_info:
            continue
        # Skip artifacts — they get no MCID
        if tag_info.get("tag", "").upper() == "ARTIFACT":
            continue

        first_idx = min(op_indices)
        last_idx = max(op_indices)

        if first_idx not in markers:
            markers[first_idx] = []
        markers[first_idx].insert(0, ("bdc", block_id))

        if last_idx not in markers:
            markers[last_idx] = []
        markers[last_idx].append(("emc", block_id))

    # Assign MCIDs
    block_to_mcid = {}
    mcid = mcid_start
    for block_id in sorted(block_ops.keys()):
        tag_info = tags_map.get(block_id)
        if tag_info and tag_info.get("tag", "").upper() != "ARTIFACT":
            block_to_mcid[block_id] = mcid
            mcid += 1

    # Build new instruction list
    new_instructions = []
    for idx, (operands, operator) in enumerate(instructions):
        if idx in markers:
            for action, block_id in markers[idx]:
                if action == "bdc" and block_id in block_to_mcid:
                    m = block_to_mcid[block_id]
                    tag = tags_map[block_id].get("tag", "P")
                    pdf_tag = tag_name_to_pdf_name(tag)
                    bdc_operands = pikepdf._core._ObjectList([
                        Name(f"/{pdf_tag}"),
                        Dictionary({"/MCID": m})
                    ])
                    new_instructions.append((bdc_operands, pikepdf.Operator("BDC")))

        new_instructions.append((operands, operator))

        if idx in markers:
            for action, block_id in markers[idx]:
                if action == "emc" and block_id in block_to_mcid:
                    emc_operands = pikepdf._core._ObjectList([])
                    new_instructions.append((emc_operands, pikepdf.Operator("EMC")))

    return new_instructions, block_to_mcid


# ── Structure Tree Builder (Multi-Page) ──────────────────────────────────────

def build_structure_tree(pdf, pages_data, tags_map, blocks_data):
    """
    Build the full PDF logical structure tree across all pages.

    pages_data: list of (page_obj, block_to_mcid) per page
    """
    # Collect all struct elems across all pages
    all_elems = {}  # block_id -> struct_elem

    for page_idx, (page, block_to_mcid) in enumerate(pages_data):
        page_ref = page.obj
        for block_id, mcid in block_to_mcid.items():
            tag_info = tags_map.get(block_id)
            if not tag_info:
                continue
            tag = tag_name_to_pdf_name(tag_info.get("tag", "P"))
            alt_text = tag_info.get("alt_text")

            mcr = Dictionary({
                "/Type": Name("/MCR"),
                "/Pg": page_ref,
                "/MCID": mcid
            })

            elem_dict = {
                "/Type": Name("/StructElem"),
                "/S": Name(f"/{tag}"),
                "/K": mcr,
            }
            if alt_text:
                elem_dict["/Alt"] = String(alt_text)

            struct_elem = pdf.make_indirect(Dictionary(elem_dict))
            all_elems[block_id] = struct_elem

    # Build document hierarchy respecting parent_tag grouping
    doc_kids = []
    current_list = None
    current_toc = None

    for block_id in sorted(all_elems.keys()):
        tag_info = tags_map.get(block_id)
        if not tag_info:
            continue
        tag = tag_info.get("tag", "P").upper()
        parent_tag = tag_info.get("parent_tag")
        elem = all_elems[block_id]

        # Handle TOC grouping
        if parent_tag and parent_tag.upper() == "TOC":
            if current_list is not None:
                current_list = None
            if current_toc is None:
                current_toc = pdf.make_indirect(Dictionary({
                    "/Type": Name("/StructElem"),
                    "/S": Name("/TOC"),
                    "/K": Array([]),
                }))
                doc_kids.append(current_toc)
            current_toc["/K"].append(elem)
            elem["/P"] = current_toc
        # Handle List grouping
        elif tag == "LI" or (parent_tag and parent_tag.upper() == "L"):
            if current_toc is not None:
                current_toc = None
            if current_list is None:
                current_list = pdf.make_indirect(Dictionary({
                    "/Type": Name("/StructElem"),
                    "/S": Name("/L"),
                    "/K": Array([]),
                }))
                doc_kids.append(current_list)
            current_list["/K"].append(elem)
            elem["/P"] = current_list
        elif tag == "L":
            # A block tagged as L itself (list container with merged items)
            if current_toc is not None:
                current_toc = None
            current_list = None
            doc_kids.append(elem)
        else:
            current_list = None
            current_toc = None
            doc_kids.append(elem)

    # Create Document element
    doc_elem = pdf.make_indirect(Dictionary({
        "/Type": Name("/StructElem"),
        "/S": Name("/Document"),
        "/K": Array(doc_kids),
    }))

    for kid in doc_kids:
        kid["/P"] = doc_elem

    # Build ParentTree (NumberTree) — one entry per page
    nums_array = Array([])

    for page_idx, (page, block_to_mcid) in enumerate(pages_data):
        if not block_to_mcid:
            continue
        max_mcid = max(block_to_mcid.values())
        min_mcid = min(block_to_mcid.values())

        # Build MCID->elem array for this page
        mcid_refs = Array([])
        # Create array indexed by local MCID (relative to this page's start)
        mcid_to_elem = {}
        for block_id, mcid in block_to_mcid.items():
            if block_id in all_elems:
                mcid_to_elem[mcid] = all_elems[block_id]

        for mcid_val in range(min_mcid, max_mcid + 1):
            if mcid_val in mcid_to_elem:
                mcid_refs.append(mcid_to_elem[mcid_val])
            else:
                mcid_refs.append(pikepdf.Null())

        nums_array.append(page_idx)
        nums_array.append(pdf.make_indirect(mcid_refs))

        # Set StructParents on the page
        page.obj["/StructParents"] = page_idx

    parent_tree = pdf.make_indirect(Dictionary({
        "/Type": Name("/NumberTree"),
        "/Nums": nums_array,
    }))

    # Create StructTreeRoot
    struct_tree_root = pdf.make_indirect(Dictionary({
        "/Type": Name("/StructTreeRoot"),
        "/K": doc_elem,
        "/ParentTree": parent_tree,
        "/ParentTreeNextKey": len(pages_data),
    }))

    doc_elem["/P"] = struct_tree_root

    # Wire into document catalog
    pdf.Root["/StructTreeRoot"] = struct_tree_root
    pdf.Root["/MarkInfo"] = Dictionary({"/Marked": True})

    return struct_tree_root


# ── Main ─────────────────────────────────────────────────────────────────────

def inject_tags(pdf_path, blocks_json_path, tags_json_path, output_path):
    """Main multi-page injection pipeline."""

    # 1. Load data
    with open(blocks_json_path, 'r', encoding='utf-8') as f:
        blocks_data = json.load(f)
    with open(tags_json_path, 'r', encoding='utf-8') as f:
        tags_data = json.load(f)

    tags_map = {item["block_id"]: item for item in tags_data}
    blocks = blocks_data["blocks"]
    total_pages = blocks_data["document"]["total_pages"]

    # Group blocks by page
    blocks_by_page = defaultdict(list)
    for block in blocks:
        blocks_by_page[block["page_idx"]].append(block)

    # 2. Open PDF
    pdf = pikepdf.Pdf.open(pdf_path)
    print(f"  PDF pages: {len(pdf.pages)}")
    print(f"  Total blocks: {len(blocks)}")
    print(f"  Total tags: {len(tags_data)}")

    # 3. Process each page
    pages_data = []  # list of (page_obj, block_to_mcid)
    global_mcid = 0
    total_matched = 0
    total_injected = 0

    for page_idx in range(len(pdf.pages)):
        page = pdf.pages[page_idx]
        page_blocks = blocks_by_page.get(page_idx, [])

        if not page_blocks:
            pages_data.append((page, {}))
            continue

        # Get page height
        mediabox = page.obj.get("/MediaBox")
        page_height = float(mediabox[3]) if mediabox else 792.0

        # Analyze content stream
        instructions, text_positions = analyze_content_stream(page)

        # Match operations to blocks
        block_ops = match_operations_to_blocks(text_positions, page_blocks, page_height)

        # Filter to only blocks that have tags (skip untagged or missing)
        tagged_block_ops = {}
        for bid, ops in block_ops.items():
            if bid in tags_map:
                tagged_block_ops[bid] = ops

        matched = len(tagged_block_ops)
        total_matched += matched

        if matched == 0:
            pages_data.append((page, {}))
            print(f"  Page {page_idx}: 0 matches (skipped)")
            continue

        # Inject BDC/EMC markers
        new_instructions, block_to_mcid = inject_marked_content(
            instructions, tagged_block_ops, tags_map, mcid_start=global_mcid
        )

        injected = len(block_to_mcid)
        total_injected += injected
        global_mcid += injected

        # Replace content stream
        page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(new_instructions))

        pages_data.append((page, block_to_mcid))
        print(f"  Page {page_idx}: {matched} matched, {injected} tagged (MCIDs {global_mcid - injected}-{global_mcid - 1})")

    # 4. Build structure tree across all pages
    print(f"\n  Building StructTreeRoot across {len(pdf.pages)} pages...")
    build_structure_tree(pdf, pages_data, tags_map, blocks_data)

    # 5. Save
    pdf.save(output_path)
    pdf.close()

    print(f"\n  [OK] Tagged PDF saved to: {os.path.abspath(output_path)}")
    print(f"  Total blocks matched: {total_matched}")
    print(f"  Total MCIDs injected: {total_injected}")
    print(f"  Open in Adobe Acrobat -> View -> Navigation Panels -> Tags")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Inject accessibility tags into a PDF using coordinate matching (multi-page)."
    )
    parser.add_argument("pdf_path", help="Original PDF file")
    parser.add_argument("blocks_json", help="structured_blocks.json from extraction")
    parser.add_argument("tags_json", help="AI-generated tags JSON")
    parser.add_argument("--output", default="tagged_output.pdf", help="Output PDF path")

    args = parser.parse_args()
    inject_tags(args.pdf_path, args.blocks_json, args.tags_json, args.output)
