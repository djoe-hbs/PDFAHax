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


def normalize_tags(tags_data):
    """
    Accept any of the tag-JSON shapes our pipeline produces:
      1. A flat list:                    [ {block_id, tag, ...}, ... ]
      2. The colleague-prompt object:    { "tagged_blocks": [...], "updated_tracking": {...} }
      3. A list of per-page chunks:      [ {"tagged_blocks": [...]}, {"tagged_blocks": [...]} ]
    Returns a flat list of tag dicts.
    """
    if isinstance(tags_data, dict):
        return tags_data.get("tagged_blocks", [])
    if isinstance(tags_data, list) and tags_data and isinstance(tags_data[0], dict) \
            and "tagged_blocks" in tags_data[0]:
        merged = []
        for chunk in tags_data:
            merged.extend(chunk.get("tagged_blocks", []))
        return merged
    return tags_data


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
    Wrap each block's text-drawing operations in BDC/EMC marked content, walking
    the content stream IN ORDER.

    A block whose operations are interleaved with other blocks' operations in the
    stream (common around figures / multi-column flow) is emitted as MULTIPLE
    marked-content runs, each with its own MCID — never one giant [min,max] range
    that swallows the blocks drawn in between (which produced improperly nested
    MCIDs before). Runs are also closed at ET so a sequence never crosses a text
    object boundary.

    Returns:
        new_instructions,
        block_to_mcids : dict block_id -> [mcid, ...] (one entry per run)
    """
    # Map each op index -> block_id (excluding artifact-tagged blocks, which get
    # no MCID and are handled by the Artifact sweep instead).
    op_to_block = {}
    for block_id, op_indices in block_ops.items():
        tag_info = tags_map.get(block_id)
        if not tag_info or tag_info.get("tag", "").upper() == "ARTIFACT":
            continue
        for idx in op_indices:
            op_to_block[idx] = block_id

    new_instructions = []
    block_to_mcids = defaultdict(list)
    mcid = mcid_start
    open_block = None

    def close_run():
        nonlocal open_block
        if open_block is not None:
            new_instructions.append((pikepdf._core._ObjectList([]), pikepdf.Operator("EMC")))
            open_block = None

    for idx, (operands, operator) in enumerate(instructions):
        target = op_to_block.get(idx)

        if target is not None:
            if open_block != target:
                close_run()
                m = mcid
                mcid += 1
                pdf_tag = tag_name_to_pdf_name(tags_map[target].get("tag", "P"))
                bdc_operands = pikepdf._core._ObjectList([
                    Name(f"/{pdf_tag}"),
                    Dictionary({"/MCID": m})
                ])
                new_instructions.append((bdc_operands, pikepdf.Operator("BDC")))
                block_to_mcids[target].append(m)
                open_block = target
            new_instructions.append((operands, operator))
        else:
            # A marked-content sequence may not cross a text-object boundary, so
            # close any open run before ET.
            if str(operator) == "ET":
                close_run()
            new_instructions.append((operands, operator))

    close_run()
    return new_instructions, dict(block_to_mcids)


# Content-producing operators that PDF/UA (7.1 test 3) requires to be marked as
# real content or as an Artifact. Positioning/state operators are excluded — they
# draw nothing, so veraPDF does not flag them.
_CONTENT_PAINT_OPS = {
    "Tj", "TJ", "'", '"',                       # text showing
    "S", "s", "f", "F", "f*", "B", "B*", "b", "b*",  # path painting
    "Do",                                        # XObjects (images / forms)
    "sh",                                        # shadings
}


def wrap_remaining_as_artifact(instructions):
    """
    Overlap-aware Artifact sweep.

    Wrap every content-producing operator that is NOT already inside a
    marked-content sequence in its own `/Artifact BMC ... EMC`. Depth is tracked
    across BDC/BMC/EMC so anything already tagged (or already artifacted) is left
    untouched — no double-marking, so this never creates nested-MCID problems.

    Each content op is wrapped individually; a single atomic operator can never
    straddle BT/ET or q/Q boundaries, so marker nesting stays valid.
    """
    out = []
    depth = 0
    wrapped = 0
    for operands, operator in instructions:
        op = str(operator)
        if op in ("BDC", "BMC"):
            depth += 1
            out.append((operands, operator))
            continue
        if op == "EMC":
            depth = max(0, depth - 1)
            out.append((operands, operator))
            continue

        if depth <= 0 and op in _CONTENT_PAINT_OPS:
            out.append((pikepdf._core._ObjectList([Name("/Artifact")]),
                        pikepdf.Operator("BMC")))
            out.append((operands, operator))
            out.append((pikepdf._core._ObjectList([]), pikepdf.Operator("EMC")))
            wrapped += 1
        else:
            out.append((operands, operator))

    return out, wrapped


# ── Structure Tree Builder (Multi-Page) ──────────────────────────────────────

def build_structure_tree(pdf, pages_data, tags_map, blocks_data):
    """
    Build the full PDF logical structure tree across all pages.

    pages_data: list of (page_obj, block_to_mcid) per page
    """
    # Index blocks by block_id so cell elems can read span/header info and
    # TH/TD grouping can read table_id and row.
    blocks_map = {b["block_id"]: b for b in blocks_data.get("blocks", [])}

    # Collect all struct elems across all pages
    all_elems = {}  # block_id -> struct_elem

    for page_idx, (page, block_to_mcids) in enumerate(pages_data):
        page_ref = page.obj
        for block_id, mcids in block_to_mcids.items():
            tag_info = tags_map.get(block_id)
            if not tag_info:
                continue
            tag = tag_name_to_pdf_name(tag_info.get("tag", "P"))
            alt_text = tag_info.get("alt_text")

            # One MCR per marked-content run. A block split across the stream has
            # several runs -> /K is an array of MCRs; a single run -> just the MCR.
            mcrs = [Dictionary({
                "/Type": Name("/MCR"),
                "/Pg": page_ref,
                "/MCID": m,
            }) for m in mcids]
            k_value = mcrs[0] if len(mcrs) == 1 else Array(mcrs)

            elem_dict = {
                "/Type": Name("/StructElem"),
                "/S": Name(f"/{tag}"),
                "/K": k_value,
            }
            if alt_text:
                elem_dict["/Alt"] = String(alt_text)

            # PDF/UA table attributes for TH/TD cells (ISO 14289-1 7.2 / 7.5):
            # ColSpan/RowSpan so rows resolve to equal column counts, and a
            # Scope on header cells so headers are determinable.
            if tag in ("TH", "TD"):
                binfo = blocks_map.get(block_id, {})
                col_span = int(binfo.get("col_span", 1) or 1)
                row_span = int(binfo.get("row_span", 1) or 1)
                attr = {"/O": Name("/Table")}
                if col_span > 1:
                    attr["/ColSpan"] = col_span
                if row_span > 1:
                    attr["/RowSpan"] = row_span
                if tag == "TH":
                    # Column header by default; a full-width single-cell row reads
                    # as a column header, narrow per-row headers as Row scope.
                    attr["/Scope"] = Name("/Column")
                elem_dict["/A"] = Dictionary(attr)

            struct_elem = pdf.make_indirect(Dictionary(elem_dict))
            all_elems[block_id] = struct_elem

    def build_table_subtree(t_id):
        """
        Build a complete Table -> TR -> TH/TD subtree for one table_id.

        Cells are gathered from ALL pages with this table_id and sorted by
        (row, col) so TR grouping and column order are correct regardless of
        the block_id (reading-order) sequence — important for merged cells
        whose bbox y-positions don't align cleanly within a row.
        """
        cells = []  # (row, col, block_id)
        for bid in all_elems:
            binfo = blocks_map.get(bid, {})
            if binfo.get("table_id", -2) != t_id:
                continue
            tinfo = tags_map.get(bid)
            if not tinfo or tinfo.get("tag", "").upper() not in ("TH", "TD"):
                continue
            cells.append((binfo.get("row", 0), binfo.get("col", 0), bid))

        cells.sort(key=lambda c: (c[0], c[1]))

        table_elem = pdf.make_indirect(Dictionary({
            "/Type": Name("/StructElem"),
            "/S": Name("/Table"),
            "/K": Array([]),
        }))

        current_tr = None
        current_tr_row = None
        for row, col, bid in cells:
            if current_tr is None or row != current_tr_row:
                current_tr = pdf.make_indirect(Dictionary({
                    "/Type": Name("/StructElem"),
                    "/S": Name("/TR"),
                    "/K": Array([]),
                }))
                table_elem["/K"].append(current_tr)
                current_tr["/P"] = table_elem
                current_tr_row = row
            cell_elem = all_elems[bid]
            current_tr["/K"].append(cell_elem)
            cell_elem["/P"] = current_tr

        return table_elem

    # Build document hierarchy respecting parent_tag grouping
    doc_kids = []
    current_list = None
    current_toc = None
    processed_tables = set()

    for block_id in sorted(all_elems.keys()):
        tag_info = tags_map.get(block_id)
        if not tag_info:
            continue
        tag = tag_info.get("tag", "P").upper()
        parent_tag = tag_info.get("parent_tag")
        elem = all_elems[block_id]

        # Handle TOC grouping
        if parent_tag and parent_tag.upper() == "TOC":
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
            current_toc = None
            current_list = None
            doc_kids.append(elem)
        # Handle Table grouping: build the whole Table -> TR -> TH/TD subtree once,
        # at the position of its first cell, with cells sorted by (row, col).
        elif tag in ("TH", "TD"):
            current_list = None
            current_toc = None
            t_id = blocks_map.get(block_id, {}).get("table_id", -1)
            if t_id in processed_tables:
                continue  # remaining cells of this table already placed
            processed_tables.add(t_id)
            table_elem = build_table_subtree(t_id)
            doc_kids.append(table_elem)
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

    for page_idx, (page, block_to_mcids) in enumerate(pages_data):
        if not block_to_mcids:
            continue

        # Map every MCID on this page (a block may own several) to its struct elem.
        # MCIDs are per-page and 0-based, so the array is indexed directly by MCID.
        mcid_to_elem = {}
        for block_id, mcids in block_to_mcids.items():
            if block_id in all_elems:
                for m in mcids:
                    mcid_to_elem[m] = all_elems[block_id]

        max_mcid = max(mcid_to_elem) if mcid_to_elem else -1
        mcid_refs = Array([])
        for mcid_val in range(0, max_mcid + 1):
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

    # PDF/UA 7.1 test 10: ViewerPreferences must set DisplayDocTitle true so the
    # window title shows the document title (from metadata) not the file name.
    vp = pdf.Root.get("/ViewerPreferences")
    if isinstance(vp, Dictionary):
        vp["/DisplayDocTitle"] = True
    else:
        pdf.Root["/ViewerPreferences"] = Dictionary({"/DisplayDocTitle": True})

    return struct_tree_root


# ── Main ─────────────────────────────────────────────────────────────────────

def inject_tags(pdf_path, blocks_json_path, tags_json_path, output_path):
    """Main multi-page injection pipeline."""

    # 1. Load data
    with open(blocks_json_path, 'r', encoding='utf-8') as f:
        blocks_data = json.load(f)
    with open(tags_json_path, 'r', encoding='utf-8') as f:
        tags_data = normalize_tags(json.load(f))

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
    total_matched = 0
    total_injected = 0

    total_artifacted = 0

    for page_idx in range(len(pdf.pages)):
        page = pdf.pages[page_idx]
        page_blocks = blocks_by_page.get(page_idx, [])

        # Analyze content stream (needed on EVERY page so the Artifact sweep can
        # run even where nothing matched — otherwise that content stays untagged).
        instructions, text_positions = analyze_content_stream(page)

        block_to_mcid = {}
        if page_blocks:
            mediabox = page.obj.get("/MediaBox")
            page_height = float(mediabox[3]) if mediabox else 792.0

            block_ops = match_operations_to_blocks(text_positions, page_blocks, page_height)
            tagged_block_ops = {bid: ops for bid, ops in block_ops.items() if bid in tags_map}

            matched = len(tagged_block_ops)
            total_matched += matched

            if matched > 0:
                # MCIDs are numbered PER PAGE (start at 0 each page). MCID values
                # only need to be unique within a page; the ParentTree keys content
                # by (StructParents=page_idx, MCID). Per-page numbering keeps each
                # page's ParentTree array indexable directly by MCID — global
                # numbering misaligned every page after page 0.
                instructions, block_to_mcid = inject_marked_content(
                    instructions, tagged_block_ops, tags_map, mcid_start=0
                )
                injected = len(block_to_mcid)
                total_injected += injected
        else:
            matched = 0

        # Artifact sweep: wrap any remaining unmarked content on this page.
        instructions, artifacted = wrap_remaining_as_artifact(instructions)
        total_artifacted += artifacted

        # Replace content stream (every page is rewritten now).
        page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(instructions))

        pages_data.append((page, block_to_mcid))
        print(f"  Page {page_idx}: {matched} matched, "
              f"{len(block_to_mcid)} tagged, {artifacted} artifacted")

    # 4. Build structure tree across all pages
    print(f"\n  Building StructTreeRoot across {len(pdf.pages)} pages...")
    build_structure_tree(pdf, pages_data, tags_map, blocks_data)

    # 4b. PDF/UA identification (ISO 14289-1 clause 5): the XMP metadata must
    # declare pdfuaid:part = 1.
    with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
        meta["pdfuaid:part"] = "1"

    # 5. Save
    pdf.save(output_path)
    pdf.close()

    print(f"\n  [OK] Tagged PDF saved to: {os.path.abspath(output_path)}")
    print(f"  Total blocks matched: {total_matched}")
    print(f"  Total MCIDs injected: {total_injected}")
    print(f"  Total content ops artifacted: {total_artifacted}")
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
