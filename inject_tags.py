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

def _mat_mul(m1, m2):
    """Concatenate two PDF matrices [a b c d e f] (m1 applied first, row-vector)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    ]


def _mat_apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def analyze_content_stream(page):
    """
    Walk the content stream and extract positions of every text-drawing op AND
    every image (Do XObject) paint.

    Returns: instructions, text_positions, image_positions
      text_positions : [{idx, x, y, font_size}]            (PDF coords)
      image_positions: [{idx, bbox=[x0,y0,x1,y1]}]         (TOP-LEFT page coords,
                        computed from the CTM so it can be matched to a block bbox)
    """
    instructions = pikepdf.parse_content_stream(page)
    text_positions = []
    image_positions = []

    mb = page.obj.get("/MediaBox")
    page_height = float(mb[3]) if mb else 792.0

    in_text_block = False
    current_x = 0.0
    current_y = 0.0
    current_font_size = 12.0
    line_x = 0.0
    line_y = 0.0

    # Graphics-state CTM stack (q/Q/cm) — needed to place images. q/Q/cm/Do are
    # not permitted inside BT/ET, so they are handled before the text-block gate.
    ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    ctm_stack = []

    for idx, (operands, operator) in enumerate(instructions):
        op = str(operator)

        if op == "q":
            ctm_stack.append(list(ctm))
            continue
        elif op == "Q":
            if ctm_stack:
                ctm = ctm_stack.pop()
            continue
        elif op == "cm":
            if len(operands) >= 6:
                m = [float(operands[i]) for i in range(6)]
                ctm = _mat_mul(m, ctm)  # cm applied first, then current CTM
            continue
        elif op == "Do":
            # Image is painted in the unit square [0,1]x[0,1] transformed by CTM.
            corners = [_mat_apply(ctm, ux, uy) for ux, uy in ((0, 0), (1, 0), (0, 1), (1, 1))]
            xs = [p[0] for p in corners]
            ys = [p[1] for p in corners]
            # Convert PDF (bottom-left origin) to top-left page coords for matching.
            image_positions.append({
                "idx": idx,
                "bbox": [min(xs), page_height - max(ys), max(xs), page_height - min(ys)],
            })
            continue

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

    return instructions, text_positions, image_positions


def match_operations_to_blocks(text_positions, page_blocks, page_height):
    """
    For each text-drawing operation, find which block it belongs to by checking
    if the operation's (x, y) falls inside the block's bbox.

    When several blocks contain the point (e.g. a small caption sitting inside a
    large paragraph's bbox), the SMALLEST-area containing block wins — the tighter
    block is the more specific owner. (Previously the first block in page order
    won, so big paragraphs stole captions' text.) Image-type blocks are never
    candidates for text ops.

    Returns a dict: block_id -> list of instruction indices
    """
    block_ops = defaultdict(list)

    for pos in text_positions:
        x = pos["x"]
        y_top_down = pdf_y_to_page_y(pos["y"], page_height)

        best_block = None
        best_area = None
        for block in page_blocks:
            if block.get("type") == "image":
                continue
            bbox = block.get("bbox")
            if not bbox or len(bbox) != 4:
                continue

            if (bbox[0] - 5 <= x <= bbox[2] + 5 and
                bbox[1] - 5 <= y_top_down <= bbox[3] + 5):
                area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
                if best_area is None or area < best_area:
                    best_area = area
                    best_block = block

        if best_block is not None:
            block_ops[best_block["block_id"]].append(pos["idx"])

    return block_ops


def match_images_to_blocks(image_positions, page_blocks):
    """
    Match each image (Do) operation to the image-type block it paints, by bbox
    overlap. Only image-type blocks are candidates, so a Do never gets attached
    to a text block. Returns dict: block_id -> [instruction indices].
    """
    block_ops = defaultdict(list)
    img_blocks = [b for b in page_blocks
                  if b.get("type") == "image" and b.get("bbox") and len(b["bbox"]) == 4]
    if not img_blocks:
        return block_ops

    for pos in image_positions:
        ib = pos["bbox"]
        best, best_ov = None, 0.0
        for blk in img_blocks:
            bb = blk["bbox"]
            ix = max(0.0, min(ib[2], bb[2]) - max(ib[0], bb[0]))
            iy = max(0.0, min(ib[3], bb[3]) - max(ib[1], bb[1]))
            ov = ix * iy
            if ov > best_ov:
                best_ov, best = ov, blk
        if best is not None:
            block_ops[best["block_id"]].append(pos["idx"])

    return block_ops


def collect_link_groups(page, text_positions, page_height):
    """
    For each existing /Link annotation on the page, find the visible text ops
    that fall under its Rect. Returns (link_groups, link_annots) where
    link_groups[i] is the list of op indices for link_annots[i].

    The op's start (x, y) must fall inside the Rect (x strictly left of the right
    edge so a glyph beginning exactly at the edge — the next word — is excluded).
    """
    annots = page.obj.get("/Annots")
    if not annots:
        return [], []

    link_groups = []
    link_annots = []
    for a in annots:
        if a.get("/Subtype") != Name("/Link"):
            continue
        rect = a.get("/Rect")
        if rect is None or len(rect) != 4:
            continue
        r = [float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])]
        # Rect to top-left page coords.
        left, right = r[0], r[2]
        top, bottom = page_height - r[3], page_height - r[1]

        ops = []
        for p in text_positions:
            x = p["x"]
            y = pdf_y_to_page_y(p["y"], page_height)
            if (left - 1 <= x <= right - 1) and (top - 1 <= y <= bottom + 1):
                ops.append(p["idx"])

        link_groups.append(ops)
        link_annots.append(a)

    return link_groups, link_annots


# ── Content Stream Injection ─────────────────────────────────────────────────

def inject_marked_content(instructions, block_ops, tags_map, blocks_map, pos_x,
                          link_groups=None, mcid_start=0):
    """
    Wrap each block's text-drawing operations in BDC/EMC marked content, walking
    the content stream IN ORDER.

    A block whose operations are interleaved with other blocks' operations in the
    stream (common around figures / multi-column flow) is emitted as MULTIPLE
    marked-content runs, each with its own MCID — never one giant [min,max] range
    that swallows the blocks drawn in between. Runs also close at BT/ET/q/Q so a
    sequence never crosses a text-object or graphics-state boundary.

    LIST LABEL SPLIT: for a list_item (tagged LI) whose bullet glyph sits to the
    LEFT of the body (marker_x0 < body_x0, the bullet being its own text op), the
    bullet ops and body ops are emitted as SEPARATE runs/MCIDs, tagged /Span and
    /LBody respectively, so build_structure_tree can form LI -> (Lbl[Span] + LBody).
    Inline markers that share a text op with the body are NOT split (fall back to
    a single LI run).

    Returns:
        new_instructions,
        block_to_runs : dict block_id -> [(role, mcid), ...]
                        role is None (normal), "lbl" (bullet) or "lbody" (body).
    """
    # Map each op index -> target key. Target is (role, block_id); role is None
    # for normal blocks, "lbl"/"lbody" for split list items. Artifact-tagged
    # blocks are excluded (handled by the Artifact sweep).
    op_to_target = {}
    for block_id, op_indices in block_ops.items():
        tag_info = tags_map.get(block_id)
        if not tag_info or tag_info.get("tag", "").upper() == "ARTIFACT":
            continue

        blk = blocks_map.get(block_id, {})
        meta = blk.get("metadata", {})
        marker_x0 = meta.get("marker_x0")
        body_x0 = meta.get("body_x0")
        splittable = (tag_info.get("tag", "").upper() == "LI"
                      and blk.get("type") == "list_item"
                      and marker_x0 is not None and body_x0 is not None
                      and (body_x0 - marker_x0) > 3)

        if splittable:
            threshold = body_x0 - 3
            for idx in op_indices:
                x = pos_x.get(idx)
                role = "lbl" if (x is not None and x < threshold) else "lbody"
                op_to_target[idx] = (role, block_id)
        else:
            for idx in op_indices:
                op_to_target[idx] = (None, block_id)

    # Link runs take priority over block assignment for their ops, so the link's
    # visible text is tagged /Link (not absorbed into the surrounding paragraph).
    if link_groups:
        for gi, ops in enumerate(link_groups):
            for idx in ops:
                op_to_target[idx] = ("link", gi)

    new_instructions = []
    block_to_runs = defaultdict(list)
    link_runs = defaultdict(list)  # link group index -> [mcid, ...]
    mcid = mcid_start
    open_target = None

    def close_run():
        nonlocal open_target
        if open_target is not None:
            new_instructions.append((pikepdf._core._ObjectList([]), pikepdf.Operator("EMC")))
            open_target = None

    for idx, (operands, operator) in enumerate(instructions):
        target = op_to_target.get(idx)

        if target is not None:
            if open_target != target:
                close_run()
                role, key = target
                m = mcid
                mcid += 1
                if role == "link":
                    pdf_tag = "Link"
                elif role == "lbl":
                    pdf_tag = "Span"
                elif role == "lbody":
                    pdf_tag = "LBody"
                else:
                    pdf_tag = tag_name_to_pdf_name(tags_map[key].get("tag", "P"))
                bdc_operands = pikepdf._core._ObjectList([
                    Name(f"/{pdf_tag}"),
                    Dictionary({"/MCID": m})
                ])
                new_instructions.append((bdc_operands, pikepdf.Operator("BDC")))
                if role == "link":
                    link_runs[key].append(m)
                else:
                    block_to_runs[key].append((role, m))
                open_target = target
            new_instructions.append((operands, operator))
        else:
            # A marked-content sequence may not cross a text-object (BT/ET) or
            # graphics-state (q/Q) boundary. Close any open run before those, so
            # e.g. an image's BDC..EMC stays within the q..Q it is painted in.
            if str(operator) in ("BT", "ET", "q", "Q"):
                close_run()
            new_instructions.append((operands, operator))

    close_run()
    return new_instructions, dict(block_to_runs), dict(link_runs)


# Bullet glyphs that should be announced via /ActualText rather than read raw.
# � is what PyMuPDF yields for the SymbolMT bullet in this document.
_BULLET_CHARS = set("•◦▪‣·–—-*●○") | {"�"}

# Default spoken text for a symbol bullet's Lbl/Span. Set to "" to make the
# screen reader skip the bullet entirely.
BULLET_ACTUAL_TEXT = "Bullet"

# Default /Contents (alternate description) written onto a Link annotation that
# lacks one. Required by PDF/UA 7.18.1 / 7.18.5. Override per-document if richer
# wording is available.
LINK_CONTENTS = "Internal link"


def actual_text_for_marker(marker: str) -> str:
    """Map a list marker to its /ActualText: symbol bullets -> configurable label;
    numbered/lettered markers -> the marker text itself (so it is read aloud)."""
    m = (marker or "").strip()
    if not m:
        return BULLET_ACTUAL_TEXT
    if all(ch in _BULLET_CHARS or ch.isspace() for ch in m):
        return BULLET_ACTUAL_TEXT
    return m


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

    # Collect all struct elems across all pages, plus a per-page map from MCID to
    # the DEEPEST struct elem holding it (the element whose /K is that MCR). For a
    # split list item that is the Span (bullet) or LBody (body), not the LI.
    all_elems = {}            # block_id -> top-level struct_elem
    page_mcid_elem = []       # index == page_idx -> {mcid: struct_elem}
    link_records = []         # (annot, link_elem, page) for ParentTree + StructParent

    for page_idx, (page, block_to_runs, links_info) in enumerate(pages_data):
        page_ref = page.obj
        mcid_elem = {}

        def mcr(m):
            return Dictionary({"/Type": Name("/MCR"), "/Pg": page_ref, "/MCID": m})

        def k_of(mcids):
            return mcr(mcids[0]) if len(mcids) == 1 else Array([mcr(m) for m in mcids])

        for block_id, runs in block_to_runs.items():
            tag_info = tags_map.get(block_id)
            if not tag_info:
                continue
            tag = tag_name_to_pdf_name(tag_info.get("tag", "P"))
            alt_text = tag_info.get("alt_text")

            # ── Split list item: LI -> (Lbl[Span] + LBody) ──
            if tag == "LI" and any(role in ("lbl", "lbody") for role, _ in runs):
                lbl_mcids = [m for role, m in runs if role == "lbl"]
                body_mcids = [m for role, m in runs if role in ("lbody", None)]

                li_elem = pdf.make_indirect(Dictionary({
                    "/Type": Name("/StructElem"), "/S": Name("/LI"), "/K": Array([]),
                }))
                li_kids = []

                if lbl_mcids:
                    marker = blocks_map.get(block_id, {}).get("metadata", {}).get("list_marker", "")
                    span_elem = pdf.make_indirect(Dictionary({
                        "/Type": Name("/StructElem"), "/S": Name("/Span"),
                        "/ActualText": String(actual_text_for_marker(marker)),
                        "/K": k_of(lbl_mcids),
                    }))
                    lbl_elem = pdf.make_indirect(Dictionary({
                        "/Type": Name("/StructElem"), "/S": Name("/Lbl"), "/K": span_elem,
                    }))
                    span_elem["/P"] = lbl_elem
                    lbl_elem["/P"] = li_elem
                    li_kids.append(lbl_elem)
                    for m in lbl_mcids:
                        mcid_elem[m] = span_elem

                if body_mcids:
                    lbody_elem = pdf.make_indirect(Dictionary({
                        "/Type": Name("/StructElem"), "/S": Name("/LBody"), "/K": k_of(body_mcids),
                    }))
                    lbody_elem["/P"] = li_elem
                    li_kids.append(lbody_elem)
                    for m in body_mcids:
                        mcid_elem[m] = lbody_elem

                li_elem["/K"] = Array(li_kids)
                all_elems[block_id] = li_elem
                continue

            # ── Normal block (P/H/Figure/Caption/TH/TD/LI-without-split/...) ──
            mcids = [m for _, m in runs]
            elem_dict = {
                "/Type": Name("/StructElem"),
                "/S": Name(f"/{tag}"),
                "/K": k_of(mcids),
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
            for m in mcids:
                mcid_elem[m] = struct_elem

        # ── Existing Link annotations on this page ──
        # Link -> [ MCR(visible text) ..., OBJR(annotation) ]. The annotation is
        # referenced by an OBJR (object reference), and links back via /StructParent
        # (set during ParentTree construction). /Contents gives the alt description.
        for li in links_info:
            annot = li["annot"]
            mcids = li["mcids"]
            objr = pdf.make_indirect(Dictionary({"/Type": Name("/OBJR"), "/Obj": annot}))
            k_items = [mcr(m) for m in mcids] + [objr]
            link_elem = pdf.make_indirect(Dictionary({
                "/Type": Name("/StructElem"),
                "/S": Name("/Link"),
                "/K": Array(k_items),
            }))
            for m in mcids:
                mcid_elem[m] = link_elem  # visible link text resolves to the Link
            if "/Contents" not in annot:
                annot["/Contents"] = String(LINK_CONTENTS)
            page.obj["/Tabs"] = Name("/S")  # PDF/UA 7.18.3: annotated page needs Tabs=S
            link_records.append((annot, link_elem, page))

        page_mcid_elem.append(mcid_elem)

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

    # Place Link elements in the document tree (under Document).
    for _annot, link_elem, _page in link_records:
        doc_kids.append(link_elem)

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

    for page_idx, (page, block_to_runs, links_info) in enumerate(pages_data):
        mcid_to_elem = page_mcid_elem[page_idx]
        if not mcid_to_elem:
            continue

        # MCIDs are per-page and 0-based, so the array is indexed directly by MCID.
        # Each MCID maps to the DEEPEST element holding it (Span/LBody for split
        # list items, the block element otherwise).
        max_mcid = max(mcid_to_elem)
        null_obj = pikepdf.Object.parse(b"null")
        mcid_refs = Array([])
        for mcid_val in range(0, max_mcid + 1):
            mcid_refs.append(mcid_to_elem.get(mcid_val, null_obj))

        nums_array.append(page_idx)
        nums_array.append(pdf.make_indirect(mcid_refs))

        # Set StructParents on the page
        page.obj["/StructParents"] = page_idx

    # Annotation entries in the SAME ParentTree. Page entries are keyed by page
    # index (0..N-1) and map to an ARRAY of per-MCID parents. Annotation entries
    # are keyed by /StructParent (a fresh integer after the page keys) and map to
    # a SINGLE parent struct elem (the Link). Both kinds coexist in /Nums.
    struct_parent_key = len(pages_data)
    for annot, link_elem, _page in link_records:
        annot["/StructParent"] = struct_parent_key
        nums_array.append(struct_parent_key)
        nums_array.append(link_elem)
        struct_parent_key += 1

    parent_tree = pdf.make_indirect(Dictionary({
        "/Type": Name("/NumberTree"),
        "/Nums": nums_array,
    }))

    # Create StructTreeRoot
    struct_tree_root = pdf.make_indirect(Dictionary({
        "/Type": Name("/StructTreeRoot"),
        "/K": doc_elem,
        "/ParentTree": parent_tree,
        "/ParentTreeNextKey": struct_parent_key,
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

    # Group blocks by page; index by id (for the Lbl/LBody split classification).
    blocks_by_page = defaultdict(list)
    for block in blocks:
        blocks_by_page[block["page_idx"]].append(block)
    blocks_map = {b["block_id"]: b for b in blocks}

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
        instructions, text_positions, image_positions = analyze_content_stream(page)

        block_to_runs = {}
        links_info = []
        if page_blocks:
            mediabox = page.obj.get("/MediaBox")
            page_height = float(mediabox[3]) if mediabox else 792.0

            block_ops = match_operations_to_blocks(text_positions, page_blocks, page_height)
            # Match image (Do) ops to image-type blocks and merge them in, so
            # figures get a real MCID + Figure StructElem instead of being swept.
            for bid, ops in match_images_to_blocks(image_positions, page_blocks).items():
                block_ops[bid].extend(ops)
            tagged_block_ops = {bid: ops for bid, ops in block_ops.items() if bid in tags_map}

            # Existing Link annotations: find the visible text op(s) under each
            # link's Rect so they can be tagged /Link (not absorbed by a paragraph).
            link_groups, link_annots = collect_link_groups(page, text_positions, page_height)
            if link_groups:
                link_idx = {i for g in link_groups for i in g}
                for bid in list(tagged_block_ops.keys()):
                    tagged_block_ops[bid] = [i for i in tagged_block_ops[bid] if i not in link_idx]

            matched = len(tagged_block_ops)
            total_matched += matched

            if matched > 0 or link_groups:
                # MCIDs are numbered PER PAGE (start at 0 each page). MCID values
                # only need to be unique within a page; the ParentTree keys content
                # by (StructParents=page_idx, MCID). Per-page numbering keeps each
                # page's ParentTree array indexable directly by MCID.
                pos_x = {p["idx"]: p["x"] for p in text_positions}
                instructions, block_to_runs, link_runs = inject_marked_content(
                    instructions, tagged_block_ops, tags_map, blocks_map, pos_x,
                    link_groups=link_groups, mcid_start=0
                )
                injected = len(block_to_runs)
                total_injected += injected
                for gi, annot in enumerate(link_annots):
                    links_info.append({"annot": annot, "mcids": link_runs.get(gi, [])})
        else:
            matched = 0

        # Artifact sweep: wrap any remaining unmarked content on this page.
        instructions, artifacted = wrap_remaining_as_artifact(instructions)
        total_artifacted += artifacted

        # Replace content stream (every page is rewritten now).
        page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(instructions))

        pages_data.append((page, block_to_runs, links_info))
        print(f"  Page {page_idx}: {matched} matched, "
              f"{len(block_to_runs)} tagged, {artifacted} artifacted"
              + (f", {len(links_info)} link(s)" if links_info else ""))

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
