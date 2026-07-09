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
from collections import defaultdict, Counter

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


# Operators that bound a "state region" a marked-content sequence must not cross,
# per ISO 32000-2 Figure 9 (BDC/BMC opened in one text/graphics state and EMC'd
# in another is invalid syntax).
_STATE_BOUNDARY_OPS = {"BT", "ET", "q", "Q"}


def repair_straddling_source_marks(instructions):
    """
    Fix PRE-EXISTING malformed marked-content in the SOURCE content stream: any
    BDC/BMC..EMC sequence whose open and close are separated by a BT/ET/q/Q is
    invalid per ISO 32000-2 Figure 9 (this is what PAC reports as "Operator 'BMC'
    not allowed in this current state"). Some PDF authoring tools (observed here:
    Word/Acrobat pagination tagging) write /Artifact spans this way.

    Fix: split each straddling sequence into multiple shorter sequences, one per
    state region, each opened with the SAME tag name and property dictionary
    (so /Artifact + /Attached + /Type /Pagination etc. are preserved verbatim)
    and closed before the next boundary operator, then reopened after it.

    SCOPE — deliberately narrow:
    - Only sequences whose open/close straddle a boundary are touched. A source
      sequence that is already valid (opens and closes within one state region)
      is left completely alone.
    - This function runs BEFORE our own tag injection (inject_marked_content)
      and BEFORE the Artifact sweep (wrap_remaining_as_artifact) ever sees the
      stream, on the RAW source instructions — so it can only ever be splitting
      sequences that were already present in the source. It has no interaction
      with MCIDs: Artifact-tagged marked content never carries an /MCID key (that
      is only ever added by inject_marked_content for OUR tag runs), so splitting
      these spans cannot renumber, shift, or duplicate any MCID our structure
      tree depends on.

    Returns: (new_instructions, n_repaired) where n_repaired counts the source
    sequences that were split (0 if the source stream had none).
    """
    # First pass: find which BDC/BMC opens are matched by an EMC that is
    # separated by at least one state-boundary operator.
    stack = []          # indices of currently-open BDC/BMC (source-level only)
    to_split = set()    # open_idx of sequences that need splitting
    for idx, (operands, operator) in enumerate(instructions):
        op = str(operator)
        if op in ("BDC", "BMC"):
            stack.append(idx)
        elif op == "EMC":
            if not stack:
                continue
            open_idx = stack.pop()
            crosses = any(
                str(instructions[j][1]) in _STATE_BOUNDARY_OPS
                for j in range(open_idx + 1, idx)
            )
            if crosses:
                to_split.add(open_idx)

    if not to_split:
        return instructions, 0

    # Second pass: rebuild the stream, splitting flagged sequences at each
    # boundary operator into close/reopen pairs with identical tag+properties.
    out = []
    active_tag = None  # (operands, operator) of the currently-open split sequence
    n_repaired = 0
    depth_stack = []   # tracks open_idx values in source order (mirrors pass 1)

    for idx, (operands, operator) in enumerate(instructions):
        op = str(operator)

        if op in ("BDC", "BMC"):
            depth_stack.append(idx)
            out.append((operands, operator))
            if idx in to_split:
                active_tag = (operands, operator)
                n_repaired += 1
            continue

        if op == "EMC":
            if depth_stack:
                open_idx = depth_stack.pop()
                if open_idx in to_split:
                    active_tag = None
            out.append((operands, operator))
            continue

        if op in _STATE_BOUNDARY_OPS and active_tag is not None:
            # Close the split sequence before the boundary, emit the boundary,
            # then reopen with the identical tag+properties right after.
            out.append((pikepdf._core._ObjectList([]), pikepdf.Operator("EMC")))
            out.append((operands, operator))
            out.append(active_tag)
            continue

        out.append((operands, operator))

    return out, n_repaired


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
                          link_groups=None, mcid_start=0, pos_font_size=None,
                          footnote_pairs=None):
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

    FOOTNOTE MARKER SPLIT (superscript precision): for a block tagged Reference
    that has an AI-confirmed footnote pairing, its ops are split by FONT SIZE
    (not x-position — the marker sits inline within the same line as the
    sentence, not to its left): any op whose font_size is <= a marker_ratio_max
    fraction of the block's own dominant font size is the tiny superscript
    glyph ("marker" role); everything else is ordinary sentence text
    ("para_text" role, tagged P — the surrounding prose is not part of the
    footnote reference itself). Symmetrically, a block tagged Note splits its
    leading small-font label op ("note_lbl") from the note body ("note_body").
    This is exactly how the real content stream is shaped: the marker "1" is
    its own separate Tj/TJ operation at 6.48pt inside a 9.96pt-dominant line,
    so splitting by measured per-op font size wraps ONLY that tiny glyph's MCID
    — never the adjacent normal-size text.

    Returns:
        new_instructions,
        block_to_runs : dict block_id -> [(role, mcid), ...]
                        role is None (normal), "lbl"/"lbody" (list),
                        "marker"/"para_text" (Reference), or
                        "note_lbl"/"note_body" (Note).
    """
    footnote_pairs = footnote_pairs or {}  # block_id -> "reference" | "note"
    MARKER_RATIO_MAX = 0.85  # matches detect_footnotes.py's SIZE_RATIO_MAX

    # Map each op index -> target key. Target is (role, block_id); role is None
    # for normal blocks, "lbl"/"lbody" for split list items, "marker"/"para_text"
    # for Reference blocks, "note_lbl"/"note_body" for Note blocks. Artifact-
    # tagged blocks are excluded (handled by the Artifact sweep).
    op_to_target = {}
    for block_id, op_indices in block_ops.items():
        tag_info = tags_map.get(block_id)
        if not tag_info or tag_info.get("tag", "").upper() == "ARTIFACT":
            continue
        block_tag = tag_info.get("tag", "").upper()

        blk = blocks_map.get(block_id, {})
        meta = blk.get("metadata", {})
        marker_x0 = meta.get("marker_x0")
        body_x0 = meta.get("body_x0")
        splittable = (block_tag == "LI"
                      and blk.get("type") == "list_item"
                      and marker_x0 is not None and body_x0 is not None
                      and (body_x0 - marker_x0) > 3)

        footnote_role = footnote_pairs.get(block_id)  # "reference" | "note" | None

        if splittable:
            threshold = body_x0 - 3
            for idx in op_indices:
                x = pos_x.get(idx)
                role = "lbl" if (x is not None and x < threshold) else "lbody"
                op_to_target[idx] = (role, block_id)
        elif footnote_role in ("reference", "note") and pos_font_size:
            sizes = [pos_font_size[idx] for idx in op_indices if idx in pos_font_size]
            if sizes:
                dom_size = Counter(sizes).most_common(1)[0][0]
            else:
                dom_size = None
            small_role = "marker" if footnote_role == "reference" else "note_lbl"
            normal_role = "para_text" if footnote_role == "reference" else "note_body"
            for idx in op_indices:
                fs = pos_font_size.get(idx)
                if dom_size and fs is not None and fs <= dom_size * MARKER_RATIO_MAX:
                    op_to_target[idx] = (small_role, block_id)
                else:
                    op_to_target[idx] = (normal_role, block_id)
        else:
            for idx in op_indices:
                op_to_target[idx] = (None, block_id)

    # Link runs take priority over block assignment for ops that AREN'T a
    # block's only content. If an op is a block's sole content, keep the block
    # tag instead — letting a link Rect claim it would silently drop the block
    # (0 MCIDs for blocks that were "matched"), which happens whenever a link's
    # Rect fully covers a block's visible glyphs (e.g. an image-map style link
    # laid over the whole page/paragraph).
    if link_groups:
        block_only_op_count = defaultdict(int)
        for idx, target in op_to_target.items():
            block_only_op_count[target] += 1

        for gi, ops in enumerate(link_groups):
            for idx in ops:
                existing = op_to_target.get(idx)
                if existing is not None and block_only_op_count[existing] <= 1:
                    continue  # this op is the block's only content — keep the block tag
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

    # Depth of PRE-EXISTING marked content in the source stream. Content already
    # inside a source BDC/BMC (commonly /Artifact on headers/footers) must NOT be
    # re-tagged with our own MCID — that nests tagged content inside an Artifact
    # (ISO 14289-1 7.1 test 2). We leave such content exactly as the source had it.
    src_depth = 0

    for idx, (operands, operator) in enumerate(instructions):
        op = str(operator)

        # Track source marked-content boundaries; never let our run cross one.
        if op in ("BDC", "BMC"):
            close_run()
            src_depth += 1
            new_instructions.append((operands, operator))
            continue
        if op == "EMC":
            close_run()
            src_depth = max(0, src_depth - 1)
            new_instructions.append((operands, operator))
            continue

        target = op_to_target.get(idx)

        if target is not None and src_depth == 0:
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
                elif role == "marker":
                    pdf_tag = "Link"      # the superscript glyph itself lives in <Link>
                elif role == "para_text":
                    pdf_tag = "P"         # surrounding sentence, NOT part of <Reference>
                elif role == "note_lbl":
                    pdf_tag = "Lbl"
                elif role == "note_body":
                    pdf_tag = "Note"      # note body's own tag (Lbl is nested separately)
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
            if op in ("BT", "ET", "q", "Q"):
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


# ── TOC destination links ─────────────────────────────────────────────────────

def create_toc_link_annotations(pdf, pages_data, dest_map, blocks_map):
    """
    Create one GoTo link annotation per approved TOC destination-map entry,
    reusing the exact mechanism already proven for pre-existing page links
    (Link StructElem -> [MCR(visible text), OBJR(annotation)], /StructParent,
    ParentTree, /Tabs /S) — the only difference is these annotations are newly
    created here rather than pre-existing in the source PDF.

    dest_map: list of entries (see toc_destination_map.json) with at least
      toc_block_id, matched_heading_block_id, matched_page_idx, matched_top_y,
      toc_text. Entries with matched_heading_block_id == None (unmatched) are
      skipped entirely — per the safeguard, no guessed link is ever created.

    Returns: links_by_page : {page_idx: [ {"annot":.., "toc_block_id":..}, ... ]}
      to be merged into each page's links_info before build_structure_tree
      wires them into the tree.
    """
    links_by_page = defaultdict(list)
    page_objs = [p for p, _, _ in pages_data]

    for entry in dest_map:
        if entry.get("matched_heading_block_id") is None:
            continue  # unmatched — leave as plain TOCI, no guessed link

        toc_bid = entry["toc_block_id"]
        toc_block = blocks_map.get(toc_bid)
        if not toc_block or not toc_block.get("bbox"):
            continue

        dest_page_idx = entry["matched_page_idx"]
        if dest_page_idx >= len(page_objs):
            continue
        dest_page_obj = page_objs[dest_page_idx].obj

        toc_page_idx = toc_block["page_idx"]
        if toc_page_idx >= len(page_objs):
            continue

        # /Rect over the TOCI entry text, in PDF (bottom-left origin) coords —
        # the block bbox is stored top-left, so flip Y using that page's height.
        toc_page_obj = page_objs[toc_page_idx].obj
        mb = toc_page_obj.get("/MediaBox")
        toc_page_h = float(mb[3]) if mb else 792.0
        bx0, by0, bx1, by1 = toc_block["bbox"]
        rect = Array([bx0, toc_page_h - by1, bx1, toc_page_h - by0])

        # /Dest: GoTo the matched heading's page, positioned at its top-y (XYZ,
        # left unchanged (null), top = heading's top-y converted to PDF coords).
        dmb = dest_page_obj.get("/MediaBox")
        dest_page_h = float(dmb[3]) if dmb else 792.0
        top_y = entry.get("matched_top_y")
        dest_top_pdf = (dest_page_h - top_y) if top_y is not None else dest_page_h
        dest_array = Array([dest_page_obj, Name("/XYZ"), pikepdf.Object.parse(b"null"), dest_top_pdf, 0])

        annot = pdf.make_indirect(Dictionary({
            "/Type": Name("/Annot"),
            "/Subtype": Name("/Link"),
            "/Rect": rect,
            "/Border": Array([0, 0, 0]),   # invisible border — purely a nav aid
            "/Dest": dest_array,
            "/Contents": String(toc_block["text"].strip()),
        }))

        toc_page_annots = toc_page_obj.get("/Annots")
        if toc_page_annots is None:
            toc_page_obj["/Annots"] = Array([annot])
        else:
            toc_page_annots.append(annot)

        links_by_page[toc_page_idx].append({"annot": annot, "toc_block_id": toc_bid})

    return links_by_page


# ── Footnote (Reference -> Note) destination links ──────────────────────────

def create_footnote_link_annotations(pdf, pages_data, footnote_geometry, tags_map, blocks_map):
    """
    Create one GoTo link annotation per AI-CONFIRMED footnote pair, reusing the
    same mechanism as create_toc_link_annotations — the only structural
    difference is WHERE the /Rect sits.

    TOC links use the whole TOCI block's bbox (the entire visible line is the
    clickable target). A footnote reference is different: only the tiny
    superscript GLYPH is the navigable target, not the sentence it sits in —
    so /Rect here is footnote_geometry's marker_bbox (captured by
    detect_footnotes.py from the raw PyMuPDF span, NOT the containing block's
    full paragraph bbox). This is the geometric half of "superscript MCID
    precision": the clickable area and the MCID it wraps must both cover only
    that ~3.3x8.6pt glyph.

    footnote_geometry: dict block_id_marker(str) -> {block_id_note, marker_bbox,
      note_label_bbox, page_idx, ...} from detect_footnotes.py's
      --geometry-output. This is CODE'S geometric candidate data.

    TRUST BOUNDARY: a geometric candidate only becomes a link if the AI
    ALSO confirmed it — i.e. tags_map[block_id_marker]["tag"] == "Reference"
    AND tags_map[block_id_note]["tag"] == "Note" AND both sides'
    pairs_with_block_id cross-reference each other. A geometric candidate the
    AI rejected (left as plain P, no Reference/Note tag) never reaches here —
    code trusts the AI's confirm/reject verdict, it does not re-decide it.

    Returns: links_by_page : {page_idx: [ {"annot":.., "marker_block_id":..,
      "note_block_id":.., "marker_mcid_role": "marker"}, ... ]}
    """
    links_by_page = defaultdict(list)
    page_objs = [p for p, _, _ in pages_data]

    for marker_bid_str, geo in footnote_geometry.items():
        marker_bid = int(marker_bid_str)
        note_bid = geo["block_id_note"]

        marker_tag_info = tags_map.get(marker_bid)
        note_tag_info = tags_map.get(note_bid)
        if not marker_tag_info or not note_tag_info:
            continue
        if str(marker_tag_info.get("tag", "")).upper() != "REFERENCE":
            continue  # AI rejected or retagged — no guessed link
        if str(note_tag_info.get("tag", "")).upper() != "NOTE":
            continue
        # Cross-reference check: both sides must confirm the SAME pairing (the
        # AI could in principle confirm block 287 as Reference paired with a
        # DIFFERENT note than geometry proposed — trust the AI's own pairing,
        # not geometry's, when they disagree).
        if marker_tag_info.get("pairs_with_block_id") != note_bid:
            continue
        if note_tag_info.get("pairs_with_block_id") != marker_bid:
            continue

        marker_block = blocks_map.get(marker_bid)
        note_block = blocks_map.get(note_bid)
        if not marker_block or not note_block:
            continue

        marker_page_idx = marker_block["page_idx"]
        note_page_idx = note_block["page_idx"]
        if marker_page_idx >= len(page_objs) or note_page_idx >= len(page_objs):
            continue

        # /Rect over ONLY the tiny superscript glyph (marker_bbox), not the
        # paragraph — top-left bbox flipped to PDF bottom-left coords.
        marker_page_obj = page_objs[marker_page_idx].obj
        mb = marker_page_obj.get("/MediaBox")
        marker_page_h = float(mb[3]) if mb else 792.0
        bx0, by0, bx1, by1 = geo["marker_bbox"]
        rect = Array([bx0, marker_page_h - by1, bx1, marker_page_h - by0])

        marker_text = geo.get("number", "")

        # REUSE a pre-existing link annotation if the source PDF already has
        # one covering the marker glyph, instead of creating a redundant
        # duplicate at the same Rect. Some source documents already carry
        # author-created footnote links (observed on Final_Test_Input.pdf page
        # 13 — the author's own annotation's Rect matched the marker glyph
        # almost exactly). Match by Rect containment of the marker glyph's
        # bbox center, restricted to /Subtype /Link annotations.
        existing_annot = None
        rmx0, rmy0, rmx1, rmy1 = float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])
        mcx, mcy = (rmx0 + rmx1) / 2, (rmy0 + rmy1) / 2
        page_annots = marker_page_obj.get("/Annots")
        if page_annots:
            for a in page_annots:
                if a.get("/Subtype") != Name("/Link"):
                    continue
                r = a.get("/Rect")
                if r is None or len(r) != 4:
                    continue
                ax0, ay0, ax1, ay1 = (float(r[0]), float(r[1]), float(r[2]), float(r[3]))
                if ax0 - 1 <= mcx <= ax1 + 1 and ay0 - 1 <= mcy <= ay1 + 1:
                    existing_annot = a
                    break

        if existing_annot is not None:
            # Reuse as-is: keep the source's own Rect/Dest untouched (it is the
            # document author's own footnote link — more authoritative than
            # anything we would compute), just ensure /Contents exists for
            # PDF/UA 7.18.1/7.18.5 (alternate description requirement).
            annot = existing_annot
            if "/Contents" not in annot:
                annot["/Contents"] = String(f"Footnote {marker_text}")
        else:
            # No pre-existing annotation to reuse — create one, same as TOC links.
            note_page_obj = page_objs[note_page_idx].obj
            nmb = note_page_obj.get("/MediaBox")
            note_page_h = float(nmb[3]) if nmb else 792.0
            note_label_bbox = geo.get("note_label_bbox")
            note_top_y = note_label_bbox[1] if note_label_bbox else note_block["bbox"][1]
            dest_top_pdf = note_page_h - note_top_y
            dest_array = Array([note_page_obj, Name("/XYZ"), pikepdf.Object.parse(b"null"),
                                dest_top_pdf, 0])

            annot = pdf.make_indirect(Dictionary({
                "/Type": Name("/Annot"),
                "/Subtype": Name("/Link"),
                "/Rect": rect,
                "/Border": Array([0, 0, 0]),
                "/Dest": dest_array,
                "/Contents": String(f"Footnote {marker_text}"),
            }))
            marker_page_annots = marker_page_obj.get("/Annots")
            if marker_page_annots is None:
                marker_page_obj["/Annots"] = Array([annot])
            else:
                marker_page_annots.append(annot)

        links_by_page[marker_page_idx].append({
            "annot": annot,
            "marker_block_id": marker_bid,
            "note_block_id": note_bid,
            "marker_number": marker_text,
        })

    return links_by_page


# ── Structure Tree Builder (Multi-Page) ──────────────────────────────────────

def build_structure_tree(pdf, pages_data, tags_map, blocks_data, toc_links_by_page=None,
                         footnote_links_by_page=None):
    """
    Build the full PDF logical structure tree across all pages.

    pages_data: list of (page_obj, block_to_mcid) per page
    toc_links_by_page: optional {page_idx: [{"annot":.., "toc_block_id":..}]}
      from create_toc_link_annotations — wires TOCI -> Link -> (MCR + OBJR)
      instead of leaving TOCI as a plain text element.
    footnote_links_by_page: optional {page_idx: [{"annot":.., "marker_block_id":..,
      "note_block_id":..}]} from create_footnote_link_annotations — wires the
      Reference block's marker MCID into a Link (OBJR + MCR) instead of leaving
      it a bare child of Reference. A Reference block with NO matching entry
      here (AI rejected it, or geometry/AI disagreed) still gets its marker
      MCID directly under Reference — degraded but valid PDF/UA structure, not
      a build failure.
    """
    # Index blocks by block_id so cell elems can read span/header info and
    # TH/TD grouping can read table_id and row.
    blocks_map = {b["block_id"]: b for b in blocks_data.get("blocks", [])}

    # Collect all struct elems across all pages, plus a per-page map from MCID to
    # the DEEPEST struct elem holding it (the element whose /K is that MCR). For a
    # split list item that is the Span (bullet) or LBody (body), not the LI.
    all_elems = {}              # block_id -> top-level struct_elem
    page_mcid_elem = []         # index == page_idx -> {mcid: struct_elem}
    link_records = []           # (annot, link_elem, page) for ParentTree + StructParent
    toc_link_records = []       # same, for newly-created TOC destination links
    footnote_link_records = []  # same, for newly-created footnote marker->note links
    reference_para_elems = {}   # Reference block_id -> its sibling <P> elem (prose text)
    reference_marker_mcids = {} # Reference block_id -> [marker mcid, ...] (pre-Link)
    note_elem_by_block_id = {}  # Note block_id -> its built <Note> struct_elem
    notes_nested_in_reference = set()  # Note block_ids re-parented into a Reference
    toc_links_by_page = toc_links_by_page or {}
    footnote_links_by_page = footnote_links_by_page or {}

    for page_idx, (page, block_to_runs, links_info) in enumerate(pages_data):
        page_ref = page.obj
        mcid_elem = {}
        page_toc_links = toc_links_by_page.get(page_idx, [])
        page_footnote_links = footnote_links_by_page.get(page_idx, [])

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

            # ── Footnote Reference: <P>(surrounding sentence) sibling to
            # Reference -> Link(marker MCID + OBJR) ──
            # The AI tags the WHOLE paragraph block Reference, but only the
            # tiny superscript glyph belongs inside <Reference>/<Link> — the
            # sentence text around it is ordinary prose. inject_marked_content
            # already split this block's ops into "marker" (the superscript,
            # its own MCID) and "para_text" (everything else) by font size.
            #
            # all_elems[block_id] stays the Reference elem (a single elem, like
            # every other block_id) so every downstream consumer of all_elems
            # keeps working unchanged. The optional prose-P sibling is tracked
            # separately in reference_para_elems and spliced in immediately
            # BEFORE the Reference when doc_kids is built (same reading-order
            # position, since both came from the same source block).
            if tag == "Reference" and any(role in ("marker", "para_text") for role, _ in runs):
                marker_mcids = [m for role, m in runs if role == "marker"]
                para_mcids = [m for role, m in runs if role in ("para_text", None)]

                if para_mcids:
                    para_elem = pdf.make_indirect(Dictionary({
                        "/Type": Name("/StructElem"), "/S": Name("/P"), "/K": k_of(para_mcids),
                    }))
                    for m in para_mcids:
                        mcid_elem[m] = para_elem
                    reference_para_elems[block_id] = para_elem

                ref_elem = pdf.make_indirect(Dictionary({
                    "/Type": Name("/StructElem"), "/S": Name("/Reference"),
                    "/K": k_of(marker_mcids) if marker_mcids else Array([]),
                }))
                for m in marker_mcids:
                    # SAFE DEFAULT: marker MCID resolves directly to Reference.
                    # If create_footnote_link_annotations() produced a
                    # confirmed annotation for this block_id, the
                    # page_footnote_links handling right below REPLACES this
                    # with Reference -> Link -> MCR+OBJR and remaps mcid_elem
                    # to the Link. A Reference with no confirmed annotation
                    # (shouldn't happen if the AI only tags Reference on
                    # confirmed pairs, but degrade safely if it does) is still
                    # valid PDF/UA structure — just without the clickable jump.
                    mcid_elem[m] = ref_elem
                if marker_mcids:
                    reference_marker_mcids[block_id] = marker_mcids

                all_elems[block_id] = ref_elem
                continue

            # ── Footnote Note: Note -> (Lbl[note_lbl MCID] + note body MCID(s)) ──
            if tag == "Note" and any(role in ("note_lbl", "note_body") for role, _ in runs):
                lbl_mcids = [m for role, m in runs if role == "note_lbl"]
                body_mcids = [m for role, m in runs if role in ("note_body", None)]

                # PDF/UA (ISO 14289-1 7.9 test 1): a Note struct element MUST
                # carry a unique /ID so it can be cross-referenced (e.g. from
                # the marker's Reference back to this exact note). Derived from
                # block_id, which is already unique per document.
                note_elem = pdf.make_indirect(Dictionary({
                    "/Type": Name("/StructElem"), "/S": Name("/Note"), "/K": Array([]),
                    "/ID": pikepdf.String(f"note-{block_id}"),
                }))
                note_kids = []

                if lbl_mcids:
                    lbl_elem = pdf.make_indirect(Dictionary({
                        "/Type": Name("/StructElem"), "/S": Name("/Lbl"), "/K": k_of(lbl_mcids),
                    }))
                    lbl_elem["/P"] = note_elem
                    note_kids.append(lbl_elem)
                    for m in lbl_mcids:
                        mcid_elem[m] = lbl_elem

                if body_mcids:
                    # Body MCIDs attach directly to Note (no extra wrapper —
                    # matches the target structure: Note -> Lbl + body MCIDs).
                    for mc in body_mcids:
                        note_kids.append(mcr(mc))
                    for m in body_mcids:
                        mcid_elem[m] = note_elem

                note_elem["/K"] = Array(note_kids)
                all_elems[block_id] = note_elem
                note_elem_by_block_id[block_id] = note_elem
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

            # TOC clickable link: nest TOCI -> Link -> (MCR(entry text) + OBJR
            # (annotation)), same pattern as a pre-existing page link, just with
            # a NEWLY CREATED annotation and placed one level deeper (inside the
            # TOCI element) instead of at Document level. Only TOCI blocks that
            # matched a body heading in the approved destination map get here —
            # create_toc_link_annotations() already filtered out unmatched ones.
            if tag == "TOCI" and page_toc_links:
                toc_link = next(
                    (tl for tl in page_toc_links if tl["toc_block_id"] == block_id), None
                )
                if toc_link is not None:
                    annot = toc_link["annot"]
                    objr = pdf.make_indirect(Dictionary({"/Type": Name("/OBJR"), "/Obj": annot}))
                    link_elem = pdf.make_indirect(Dictionary({
                        "/Type": Name("/StructElem"),
                        "/S": Name("/Link"),
                        "/K": Array([k_of(mcids), objr]),
                        "/P": struct_elem,
                    }))
                    for m in mcids:
                        mcid_elem[m] = link_elem  # deepest owner of the MCID is now the Link
                    struct_elem["/K"] = link_elem  # TOCI's only child is the Link
                    # /Contents was already set at annotation-creation time.
                    toc_link_records.append((annot, link_elem, page))

        # ── Footnote reference links: nest Reference -> Link(marker MCID +
        # OBJR) ──, replacing the SAFE-DEFAULT marker->Reference mapping set
        # above with marker->Link, same pattern as the TOCI link nesting.
        # page_footnote_links only contains entries create_footnote_link_
        # annotations() already restricted to AI-CONFIRMED Reference/Note
        # pairs (rejected/unmatched candidates never produced an annotation),
        # so no additional confirm/reject logic is needed here — this loop
        # trusts that upstream filtering completely.
        for fl in page_footnote_links:
            marker_bid = fl["marker_block_id"]
            marker_mcids = reference_marker_mcids.get(marker_bid)
            ref_elem = all_elems.get(marker_bid)
            if not marker_mcids or ref_elem is None:
                continue  # Reference block wasn't on this page's block_to_runs; skip

            annot = fl["annot"]
            objr = pdf.make_indirect(Dictionary({"/Type": Name("/OBJR"), "/Obj": annot}))
            link_elem = pdf.make_indirect(Dictionary({
                "/Type": Name("/StructElem"),
                "/S": Name("/Link"),
                "/Alt": String(fl.get("marker_number", "")),
                "/K": Array([k_of(marker_mcids), objr]),
                "/P": ref_elem,
            }))
            for m in marker_mcids:
                mcid_elem[m] = link_elem  # deepest owner of the marker MCID is now the Link

            # Re-parent the paired <Note> INSIDE <Reference>, as a sibling of
            # <Link> (gold-standard target: Reference -> [Link, Note]), instead
            # of leaving it hoisted to Document level. note_elem was already
            # fully built (Note -> Lbl + body MCIDs) when its own block_id was
            # processed earlier in this same page loop, or an earlier page's —
            # note_elem_by_block_id is shared across the whole pages_data pass,
            # so lookups work regardless of which page built it.
            note_bid = fl.get("note_block_id")
            note_elem = note_elem_by_block_id.get(note_bid) if note_bid is not None else None
            ref_kids = [link_elem]
            if note_elem is not None:
                note_elem["/P"] = ref_elem
                ref_kids.append(note_elem)

            ref_elem["/K"] = Array(ref_kids) if len(ref_kids) > 1 else link_elem
            link_elem["/P"] = ref_elem
            footnote_link_records.append((annot, link_elem, page))
            if note_elem is not None:
                notes_nested_in_reference.add(note_bid)

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
        elif tag == "REFERENCE":
            current_list = None
            current_toc = None
            # Gold-standard target: Reference is a CHILD of the surrounding
            # paragraph's <P>, inline at the point in reading order where the
            # superscript marker sits — NOT a sibling hoisted to Document level.
            # The AI tagged the whole source paragraph as one block_id (the
            # marker/prose split is superscript-glyph precision inside the
            # content stream, not a document-structure split), so para_elem's
            # own body MCID(s) already read as the sentence; appending elem
            # (Reference) after them places the marker exactly where it trails
            # that sentence.
            para_elem = reference_para_elems.get(block_id)
            if para_elem is not None:
                existing_k = para_elem["/K"]
                para_kids = list(existing_k) if isinstance(existing_k, pikepdf.Array) else [existing_k]
                para_kids.append(elem)
                para_elem["/K"] = Array(para_kids)
                elem["/P"] = para_elem
                doc_kids.append(para_elem)  # /P (Document) set below, for para_elem only
            else:
                # No surrounding-prose sibling was split out (e.g. the marker
                # was the block's only content) — degrade safely to the old
                # Document-level placement rather than lose the Reference.
                doc_kids.append(elem)
        elif tag == "NOTE" and block_id in notes_nested_in_reference:
            # Already re-parented inside its Reference (as a sibling of Link)
            # when the footnote link was built above — do not ALSO hoist it to
            # Document level, which would double-place the same struct_elem.
            current_list = None
            current_toc = None
            continue
        else:
            current_list = None
            current_toc = None
            doc_kids.append(elem)

    # Place pre-existing-annotation Link elements in the document tree (under
    # Document). TOC destination links are NOT placed here — their link_elem is
    # already nested inside the owning TOCI element's /K (set above), so adding
    # them to doc_kids too would duplicate them in the tree. TOC links still need
    # their own /P set for the parent chain to resolve correctly.
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
    # TOC destination-link annotations use this SAME counter/keyspace, so their
    # /StructParent values continue on from pre-existing links with no collision
    # (each annotation gets one fresh integer; page keys 0..N-1 are already used).
    struct_parent_key = len(pages_data)
    for annot, link_elem, _page in link_records:
        annot["/StructParent"] = struct_parent_key
        nums_array.append(struct_parent_key)
        nums_array.append(link_elem)
        struct_parent_key += 1
    for annot, link_elem, _page in toc_link_records:
        annot["/StructParent"] = struct_parent_key
        nums_array.append(struct_parent_key)
        nums_array.append(link_elem)
        struct_parent_key += 1
    for annot, link_elem, _page in footnote_link_records:
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

def inject_tags(pdf_path, blocks_json_path, tags_json_path, output_path, toc_map_path=None,
                footnote_geometry_path=None):
    """
    Main multi-page injection pipeline.

    toc_map_path: optional path to an APPROVED toc_destination_map.json (see
      build_toc_destination_map.py). When given, a GoTo link annotation is
      created for every entry with a matched_heading_block_id, wired as
      TOCI -> Link -> (MCR + OBJR), reusing the same ParentTree/StructParent
      mechanism as pre-existing page links. Entries with no match are left as
      plain TOCI — never a guessed link.

    footnote_geometry_path: optional path to footnote_geometry.json (see
      detect_footnotes.py --geometry-output). Contains CODE's geometric
      candidates (marker/note bboxes), keyed by block_id_marker. A candidate
      only produces a Reference->Link->Note chain if the AI ALSO confirmed it
      in tags_json (tag=="Reference"/"Note" with matching pairs_with_block_id
      on both sides) — code trusts the AI's confirm/reject verdict here, it
      does not re-decide it. Rejected/unmatched candidates stay plain text.
    """

    # 1. Load data
    with open(blocks_json_path, 'r', encoding='utf-8') as f:
        blocks_data = json.load(f)
    with open(tags_json_path, 'r', encoding='utf-8') as f:
        tags_data = normalize_tags(json.load(f))

    toc_dest_map = None
    if toc_map_path:
        with open(toc_map_path, 'r', encoding='utf-8') as f:
            toc_dest_map = json.load(f)

    footnote_geometry = None
    if footnote_geometry_path:
        with open(footnote_geometry_path, 'r', encoding='utf-8') as f:
            footnote_geometry = json.load(f)

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

    # 2b. AI-CONFIRMED footnote pairs (block_id -> "reference" | "note"), used
    # by inject_marked_content to split a block's ops by font size (superscript
    # marker vs. surrounding prose; note label vs. note body). Built directly
    # from tags_json's tag + pairs_with_block_id fields — independent of
    # footnote_geometry, since the split must happen for ANY AI-confirmed
    # Reference/Note block even if --footnote-geometry wasn't passed (the
    # block still needs correct MCID splitting; only the clickable /Dest
    # annotation additionally needs the geometry file).
    footnote_pairs_map = {}
    for bid, t in tags_map.items():
        tg = str(t.get("tag", "")).upper()
        if tg == "REFERENCE" and t.get("pairs_with_block_id") is not None:
            footnote_pairs_map[bid] = "reference"
        elif tg == "NOTE" and t.get("pairs_with_block_id") is not None:
            footnote_pairs_map[bid] = "note"

    # 3. Process each page
    pages_data = []  # list of (page_obj, block_to_mcid)
    total_matched = 0
    total_injected = 0

    total_artifacted = 0
    total_repaired = 0

    for page_idx in range(len(pdf.pages)):
        page = pdf.pages[page_idx]
        page_blocks = blocks_by_page.get(page_idx, [])

        # Repair pre-existing malformed marked-content in the SOURCE stream
        # (ISO 32000-2 Figure 9 violations, e.g. a source /Artifact BDC..EMC
        # straddling BT/ET/q/Q) BEFORE anything else reads the stream, so every
        # downstream instruction index (text/image positions, our own MCID
        # injection) is computed against the already-repaired instruction list.
        raw_instructions = list(pikepdf.parse_content_stream(page))
        raw_instructions, repaired = repair_straddling_source_marks(raw_instructions)
        total_repaired += repaired
        if repaired:
            page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(raw_instructions))

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
            # A block is only stripped of an op if it has OTHER ops left over —
            # never let this empty a block out entirely. A large/overlapping link
            # Rect (e.g. an image-map style layout) can otherwise cover a whole
            # page's text positions and silently zero out every block's ops,
            # which produces 0 MCIDs injected despite N blocks "matched" (matched
            # counts dict entries, not op-list length, so the loss was invisible).
            link_groups, link_annots = collect_link_groups(page, text_positions, page_height)
            if link_groups:
                link_idx = {i for g in link_groups for i in g}
                # AI-confirmed footnote marker/note ops are EXCLUDED from generic
                # link stripping, even if a pre-existing link annotation's Rect
                # happens to cover them (this happens when the source PDF already
                # has its own author-created footnote link on the marker glyph —
                # observed on Final_Test_Input.pdf page 13). Reference/Note
                # handling must get first claim on these ops so the superscript
                # glyph's MCID isn't silently absorbed into a bare generic /Link
                # with no Reference parent. create_footnote_link_annotations()
                # separately detects and REUSES that same pre-existing annotation
                # (rather than creating a redundant duplicate) when its Rect
                # matches the marker glyph — see that function's docstring.
                footnote_op_idx = {
                    idx for bid in tagged_block_ops if bid in footnote_pairs_map
                    for idx in tagged_block_ops[bid]
                }
                link_idx -= footnote_op_idx
                # Also strip those ops out of link_groups itself (the list
                # inject_marked_content receives) — otherwise its OWN internal
                # link-priority pass (op_to_target overridden to "link") would
                # reclaim them independently of the stripping done here.
                link_groups = [[i for i in g if i not in footnote_op_idx] for g in link_groups]
                for bid in list(tagged_block_ops.keys()):
                    stripped = [i for i in tagged_block_ops[bid] if i not in link_idx]
                    if stripped:
                        tagged_block_ops[bid] = stripped
                    # else: keep the original ops — this block is entirely under
                    # link Rect(s), so its own tag takes priority over splitting
                    # out a redundant /Link run for the same text.

            matched = len(tagged_block_ops)
            total_matched += matched

            if matched > 0 or link_groups:
                # MCIDs are numbered PER PAGE (start at 0 each page). MCID values
                # only need to be unique within a page; the ParentTree keys content
                # by (StructParents=page_idx, MCID). Per-page numbering keeps each
                # page's ParentTree array indexable directly by MCID.
                pos_x = {p["idx"]: p["x"] for p in text_positions}
                pos_font_size = {p["idx"]: p["font_size"] for p in text_positions}
                instructions, block_to_runs, link_runs = inject_marked_content(
                    instructions, tagged_block_ops, tags_map, blocks_map, pos_x,
                    link_groups=link_groups, mcid_start=0,
                    pos_font_size=pos_font_size, footnote_pairs=footnote_pairs_map,
                )
                injected = len(block_to_runs)
                total_injected += injected
                for gi, annot in enumerate(link_annots):
                    links_info.append({"annot": annot, "mcids": link_runs.get(gi, [])})
                if matched > 0 and injected == 0:
                    print(f"  [!] Page {page_idx}: {matched} block(s) matched but 0 MCIDs "
                          f"injected — every matched block ended up with an empty op list "
                          f"(check for oversized/overlapping Link annotation Rects).")
        else:
            matched = 0

        # Artifact sweep: wrap any remaining unmarked content on this page.
        instructions, artifacted = wrap_remaining_as_artifact(instructions)
        total_artifacted += artifacted

        # Replace content stream (every page is rewritten now).
        page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(instructions))

        # PDF/UA 7.18.3 / gold-standard parity: tab order follows structure on
        # EVERY page (previously only set on pages that had link annotations).
        page.obj["/Tabs"] = Name("/S")

        pages_data.append((page, block_to_runs, links_info))
        print(f"  Page {page_idx}: {matched} matched, "
              f"{len(block_to_runs)} tagged, {artifacted} artifacted"
              + (f", {repaired} source-mark repair(s)" if repaired else "")
              + (f", {len(links_info)} link(s)" if links_info else ""))

    # 3b. TOC destination links (approved map only — see toc_map_path docstring).
    toc_links_by_page = {}
    if toc_dest_map:
        toc_links_by_page = create_toc_link_annotations(pdf, pages_data, toc_dest_map, blocks_map)
        n_toc_links = sum(len(v) for v in toc_links_by_page.values())
        n_unmatched = sum(1 for e in toc_dest_map if e.get("matched_heading_block_id") is None)
        print(f"  TOC links: {n_toc_links} created, {n_unmatched} left unmatched (no guessed link)")

    # 3c. Footnote reference links (AI-confirmed pairs only — see
    # footnote_geometry_path docstring). Only candidates where BOTH the
    # Reference and Note blocks were tagged (with matching pairs_with_block_id)
    # by the AI produce an annotation; geometry-only candidates the AI rejected
    # are silently skipped by create_footnote_link_annotations.
    footnote_links_by_page = {}
    if footnote_geometry:
        footnote_links_by_page = create_footnote_link_annotations(
            pdf, pages_data, footnote_geometry, tags_map, blocks_map
        )
        n_fn_links = sum(len(v) for v in footnote_links_by_page.values())
        n_fn_candidates = len(footnote_geometry)
        print(f"  Footnote links: {n_fn_links} created (AI-confirmed) of "
              f"{n_fn_candidates} geometric candidate(s)")

    # 4. Build structure tree across all pages
    print(f"\n  Building StructTreeRoot across {len(pdf.pages)} pages...")
    build_structure_tree(pdf, pages_data, tags_map, blocks_data, toc_links_by_page,
                         footnote_links_by_page)

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
    print(f"  Total source marked-content sequences repaired: {total_repaired}")
    print(f"  Open in Adobe Acrobat -> View -> Navigation Panels -> Tags")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Inject accessibility tags into a PDF using coordinate matching (multi-page)."
    )
    parser.add_argument("pdf_path", help="Original PDF file")
    parser.add_argument("blocks_json", help="structured_blocks.json from extraction")
    parser.add_argument("tags_json", help="AI-generated tags JSON")
    parser.add_argument("--output", default="tagged_output.pdf", help="Output PDF path")
    parser.add_argument("--toc-map", default=None,
                        help="Approved toc_destination_map.json (creates TOC GoTo links)")
    parser.add_argument("--footnote-geometry", default=None,
                        help="footnote_geometry.json from detect_footnotes.py --geometry-output "
                            "(creates footnote GoTo links for AI-confirmed Reference/Note pairs)")

    args = parser.parse_args()
    inject_tags(args.pdf_path, args.blocks_json, args.tags_json, args.output, args.toc_map,
               args.footnote_geometry)
