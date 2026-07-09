"""
Footnote/reference detection (STAGE 1 + STAGE 2 of the footnote feature).

DETECTION-FIRST: this module only FINDS and PAIRS the two halves of a footnote
— it builds NO structure tree and touches NO PDF. Run it, review the printed
pairing table (or the JSON), and only proceed to chain-building after approval
(same review-gate discipline as build_toc_destination_map.py for TOC links).

Two independent detectors, because normalize_blocks() already collapses the
tiny superscript glyph into its surrounding paragraph's text (the marker isn't
preserved as its own block) — this operates on RAW PyMuPDF spans, not the
normalized blocks/tags JSON.

STAGE 1a — inline reference marker (superscript):
  A span is a superscript-marker CANDIDATE when, compared to the line's
  dominant (most common) font size:
    - its font size is markedly smaller (<= size_ratio_max of the line's
      dominant size), AND
    - its baseline is raised above the line's dominant baseline by at least
      raise_min_pt (smaller y = higher on the page, top-left origin), AND
    - its text is a bare number (optionally with trailing punctuation), so
      normal-sized digits elsewhere in a sentence are never candidates.
  This baseline+size combination is what separates a true superscript from
  merely-small text (e.g. captions), and the digit-only filter keeps ordinary
  prose numerals out entirely.

STAGE 1b — footnote body (bottom-of-page numbered line):
  A span is a footnote-LABEL candidate when it is a small, bare-number span
  that is the FIRST token on its line, and that line sits in the bottom margin
  region of the page (below bottom_region_frac of page height). The rest of
  that line (and any immediately-following same-indent lines with no leading
  marker of their own) is the footnote body text.
  NOTE: this document has no separator rule (page.get_drawings() is empty for
  it), so "below a separator" is NOT used as a signal — bottom-region position
  + small leading numeral is what's actually available and reliable here.

STAGE 2 — pairing:
  Match a marker to a footnote label with the SAME page and the SAME number.
  Multiple footnotes per page are supported (grouped by number). A marker with
  no same-page same-number footnote label (or vice versa) is reported
  UNMATCHED and is never force-paired.
"""

import argparse
import json
import re
import sys
import os
from collections import Counter, defaultdict

import fitz  # PyMuPDF

# ── Tunables ─────────────────────────────────────────────────────────────────
SIZE_RATIO_MAX = 0.85      # marker font size <= this fraction of line's dominant size
RAISE_MIN_PT = 1.5         # marker baseline must sit at least this many pt above
                            # the line's dominant baseline (smaller y = higher)
BOTTOM_REGION_FRAC = 0.75  # footnote-label lines must start below this fraction
                            # of page height (0=top, 1=bottom)
NUMBER_RE = re.compile(r'^\(?(\d{1,3})\)?[.\)]?$')  # bare number, optional (), ., )


def _line_dominant(line):
    """(dominant_size, dominant_baseline_y) = the CHARACTER-COUNT-weighted most
    common (size, baseline) among a line's non-space spans, i.e. what the
    "normal" text in this line looks like, to compare candidate
    superscript/label spans against.

    Weighted by character count (not span count): a line with a 1-character
    marker span ("1" @ 6.5pt) and a long body span (" The vacation policy..."
    @ 10.0pt) has only 2 spans, so an unweighted majority vote over spans can
    pick the marker's own tiny size as "dominant" on a tie. Weighting by text
    length makes the long body text correctly win.
    """
    sized = [(round(s["size"], 1), round(s["origin"][1], 1), len(s["text"]))
             for s in line["spans"] if s["text"].strip()]
    if not sized:
        return None, None
    size_weight = Counter()
    for size, _, n in sized:
        size_weight[size] += n
    dom_size = size_weight.most_common(1)[0][0]
    baseline_weight = Counter()
    for size, baseline, n in sized:
        if size == dom_size:
            baseline_weight[baseline] += n
    dom_baseline = baseline_weight.most_common(1)[0][0]
    return dom_size, dom_baseline


def _is_bare_number(text):
    m = NUMBER_RE.match(text.strip())
    return m.group(1) if m else None


def detect_markers(doc):
    """STAGE 1a: superscript reference-marker candidates. Returns a list of
    dicts: page_idx, number, bbox (TOPLEFT), font_size, baseline_y, line_text.

    A footnote LABEL (the small leading numeral on its own bottom-of-page line,
    e.g. "1 The vacation policy...") is geometrically identical to a true
    superscript marker by size+raise alone — it IS small and raised relative to
    its line's dominant baseline. It must never be double-detected as a marker
    (that would pair a footnote with itself). Excluded here by construction:
    a span is only a marker candidate if it is NOT the first non-space span on
    a bottom-region line — that specific position is reserved for
    detect_footnote_bodies() instead.
    """
    markers = []
    for page_idx, page in enumerate(doc):
        page_h = page.rect.height
        bottom_y = page_h * BOTTOM_REGION_FRAC
        pd = page.get_text("dict")
        for block in pd["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                dom_size, dom_baseline = _line_dominant(line)
                if dom_size is None:
                    continue
                line_text = "".join(s["text"] for s in line["spans"])
                non_space_spans = [s for s in line["spans"] if s["text"].strip()]
                is_bottom_region_line = line["bbox"][1] >= bottom_y
                for i, span in enumerate(non_space_spans):
                    text = span["text"].strip()
                    num = _is_bare_number(text)
                    if num is None:
                        continue
                    # Skip: this is the reserved footnote-label position.
                    if is_bottom_region_line and i == 0:
                        continue
                    if span["size"] > dom_size * SIZE_RATIO_MAX:
                        continue
                    baseline_y = span["origin"][1]
                    raise_amt = dom_baseline - baseline_y  # positive = raised
                    if raise_amt < RAISE_MIN_PT:
                        continue
                    markers.append({
                        "page_idx": page_idx,
                        "number": num,
                        "bbox": list(span["bbox"]),
                        "font_size": round(span["size"], 2),
                        "baseline_y": round(baseline_y, 2),
                        "raise_pt": round(raise_amt, 2),
                        "line_text": line_text.strip()[:80],
                    })
    return markers


def detect_footnote_bodies(doc):
    """STAGE 1b: bottom-of-page numbered-line candidates. Returns a list of
    dicts: page_idx, number, label_bbox, body_text, body_bbox."""
    bodies = []
    for page_idx, page in enumerate(doc):
        page_h = page.rect.height
        bottom_y = page_h * BOTTOM_REGION_FRAC
        pd = page.get_text("dict")
        for block in pd["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                if line["bbox"][1] < bottom_y:
                    continue  # not in the bottom margin region
                spans = [s for s in line["spans"] if s["text"].strip()]
                if not spans:
                    continue
                dom_size, _ = _line_dominant(line)
                first = spans[0]
                num = _is_bare_number(first["text"])
                if num is None:
                    continue
                if dom_size is None or first["size"] > dom_size * SIZE_RATIO_MAX:
                    continue  # first token isn't notably smaller -> not a label
                body_text = "".join(s["text"] for s in spans[1:]).strip()
                if not body_text:
                    continue
                bodies.append({
                    "page_idx": page_idx,
                    "number": num,
                    "label_bbox": list(first["bbox"]),
                    "body_text": body_text,
                    "body_bbox": list(line["bbox"]),
                })
    return bodies


def pair(markers, bodies):
    """STAGE 2: pair by (page_idx, number). Returns (pairs, unmatched_markers,
    unmatched_bodies)."""
    body_index = defaultdict(list)
    for b in bodies:
        body_index[(b["page_idx"], b["number"])].append(b)

    pairs = []
    used_bodies = set()
    unmatched_markers = []

    for m in markers:
        key = (m["page_idx"], m["number"])
        candidates = body_index.get(key, [])
        if not candidates:
            unmatched_markers.append(m)
            continue
        # If multiple same-number bodies exist on the same page (rare/ambiguous),
        # take the first and flag confidence accordingly.
        body = candidates[0]
        confidence = "HIGH" if len(candidates) == 1 else f"AMBIGUOUS ({len(candidates)} candidates)"
        pairs.append({"marker": m, "body": body, "confidence": confidence})
        used_bodies.add(id(body))

    unmatched_bodies = [b for b in bodies if id(b) not in used_bodies]
    return pairs, unmatched_markers, unmatched_bodies


def print_report(pairs, unmatched_markers, unmatched_bodies):
    print(f"{'#':>3}  {'MARKER':<8} {'PAGE':>5} {'MARKER TEXT (line)':<45} "
          f"{'FOOTNOTE BODY':<45} CONFIDENCE")
    print("-" * 140)
    for i, p in enumerate(pairs):
        m, b = p["marker"], p["body"]
        print(f"{i:>3}  {m['number']:<8} {m['page_idx']+1:>5}  "
              f"{m['line_text'][:43]:<45} {b['body_text'][:43]:<45} {p['confidence']}")

    print(f"\nTotal pairs: {len(pairs)}")
    print(f"Unmatched markers (superscript with NO matching footnote body): {len(unmatched_markers)}")
    for m in unmatched_markers:
        print(f"   p.{m['page_idx']+1} marker={m['number']!r} in line: {m['line_text'][:70]!r}")
    print(f"Unmatched footnote-body candidates (numbered bottom line, NO matching marker): {len(unmatched_bodies)}")
    for b in unmatched_bodies:
        print(f"   p.{b['page_idx']+1} label={b['number']!r} body: {b['body_text'][:70]!r}")


def resolve_block_ids(pairs, blocks_json_path):
    """
    Map each pair's marker/body geometry to the containing NORMALIZED block
    (from structured_blocks_merged.json), by point-in-bbox on the geometric
    span's own bbox center. This is what lets the AI prompt reference
    block_id_marker / block_id_note — the AI only ever sees block_ids, never
    raw PyMuPDF span coordinates.

    A pair whose marker or body span doesn't resolve to any normalized block
    (bbox mismatch / edge case) is dropped with a warning — it is better to
    surface nothing than to hand the AI a candidate it can't act on.
    """
    blocks = json.load(open(blocks_json_path, encoding="utf-8"))["blocks"]
    by_page = defaultdict(list)
    for b in blocks:
        by_page[b["page_idx"]].append(b)

    def find_block(page_idx, bbox):
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        best, best_area = None, None
        for blk in by_page.get(page_idx, []):
            bb = blk.get("bbox")
            if not bb or len(bb) != 4:
                continue
            if bb[0] - 2 <= cx <= bb[2] + 2 and bb[1] - 2 <= cy <= bb[3] + 2:
                area = max(0.0, bb[2] - bb[0]) * max(0.0, bb[3] - bb[1])
                if best_area is None or area < best_area:
                    best, best_area = blk["block_id"], area
        return best

    resolved = []
    dropped = 0
    for p in pairs:
        m, b = p["marker"], p["body"]
        bid_marker = find_block(m["page_idx"], m["bbox"])
        bid_note = find_block(b["page_idx"], b["body_bbox"])
        if bid_marker is None or bid_note is None:
            dropped += 1
            continue
        resolved.append({
            "page_idx": m["page_idx"],
            "block_id_marker": bid_marker,
            "block_id_note": bid_note,
            "number": m["number"],
            "confidence": p["confidence"],
            # Precise glyph geometry (from the raw span, NOT the containing
            # block's full bbox) — carried through so inject_tags.py can build
            # a tight /Rect over just the tiny marker glyph, not the whole
            # paragraph, without having to re-run PyMuPDF span analysis itself.
            "marker_bbox": m["bbox"],
            "marker_font_size": m["font_size"],
            "note_label_bbox": b["label_bbox"],
        })
    if dropped:
        print(f"  [!] {dropped} geometric pair(s) could not be resolved to a "
              f"normalized block_id and were dropped.")
    return resolved


def write_footnote_candidates_per_page(resolved, total_pages, out_dir):
    """
    Write footnote_candidates.json content SPLIT PER PAGE, matching the
    per-page chunking the AI prompt describes (page_N_chunk.json /
    page_N_overlap.json / footnote_candidates.json all live together per page).
    Pages with no candidates get an empty list — the prompt tells the AI that's
    normal and requires no search on their part.

    AI-FACING FILE ONLY: deliberately lean (block_id_marker, block_id_note,
    number) — the AI's job is a text-content judgment call (rule 9a), not
    geometry, so glyph bboxes are withheld here and kept in the SEPARATE
    footnote_geometry.json (write_footnote_geometry) that inject_tags.py reads
    instead, for MCID-precision /Rect placement.
    """
    os.makedirs(out_dir, exist_ok=True)
    by_page = defaultdict(list)
    for r in resolved:
        by_page[r["page_idx"]].append({
            "block_id_marker": r["block_id_marker"],
            "block_id_note": r["block_id_note"],
            "number": r["number"],
        })
    for page_idx in range(total_pages):
        path = os.path.join(out_dir, f"page_{page_idx}_footnote_candidates.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(by_page.get(page_idx, []), f, indent=2)
    return {p: len(v) for p, v in by_page.items()}


def write_footnote_geometry(resolved, output_path):
    """
    CODE-FACING FILE: full geometry (marker_bbox, note_label_bbox, font size)
    keyed by block_id_marker, for inject_tags.py --footnote-geometry to build
    a tight /Rect over just the marker glyph. Written for ALL geometric
    candidates regardless of AI verdict — inject_tags.py cross-references this
    against the AI's CONFIRMED tags and only acts on pairs that are both
    geometrically detected AND AI-confirmed.
    """
    out = {str(r["block_id_marker"]): r for r in resolved}
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)


def write_map(pairs, output_path):
    out = []
    for p in pairs:
        m, b = p["marker"], p["body"]
        out.append({
            "marker_page_idx": m["page_idx"],
            "marker_number": m["number"],
            "marker_bbox": m["bbox"],
            "marker_font_size": m["font_size"],
            "marker_line_text": m["line_text"],
            "body_page_idx": b["page_idx"],
            "body_number": b["number"],
            "body_label_bbox": b["label_bbox"],
            "body_text": b["body_text"],
            "body_bbox": b["body_bbox"],
            "confidence": p["confidence"],
        })
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Detect footnote-marker/footnote-body CANDIDATE pairs by geometry "
                    "(font size + baseline raise + bottom-of-page position + matching "
                    "number). These are CANDIDATES only — the AI confirms or rejects "
                    "each one (rule 9a) using text context the geometry can't see."
    )
    ap.add_argument("pdf_path", help="Input PDF")
    ap.add_argument("--blocks-json", default=None,
                    help="structured_blocks_merged.json — required to resolve "
                        "geometric spans to block_ids for the AI. If omitted, only "
                        "the human-readable review table + geometry map are produced.")
    ap.add_argument("--output", default="footnote_pairing_map.json",
                    help="Human-readable geometry pairing map (bbox-based, for review)")
    ap.add_argument("--candidates-dir", default=None,
                    help="Directory to write page_N_footnote_candidates.json into, "
                        "for the AI prompt (requires --blocks-json)")
    ap.add_argument("--geometry-output", default=None,
                    help="Path for footnote_geometry.json (code-facing, glyph "
                        "bboxes for inject_tags.py --footnote-geometry; requires "
                        "--blocks-json)")
    args = ap.parse_args()

    doc = fitz.open(args.pdf_path)
    markers = detect_markers(doc)
    bodies = detect_footnote_bodies(doc)
    n_pages = len(doc)
    doc.close()

    print(f"Detected {len(markers)} superscript marker candidate(s)")
    print(f"Detected {len(bodies)} footnote-body candidate(s)\n")

    pairs, unmatched_markers, unmatched_bodies = pair(markers, bodies)
    print_report(pairs, unmatched_markers, unmatched_bodies)
    write_map(pairs, args.output)
    print(f"\nPairing map written -> {os.path.abspath(args.output)}")

    if args.blocks_json:
        resolved = resolve_block_ids(pairs, args.blocks_json)
        print(f"\nResolved {len(resolved)}/{len(pairs)} pair(s) to block_ids:")
        for r in resolved:
            print(f"   p.{r['page_idx']+1}  marker=block_id {r['block_id_marker']}  "
                  f"note=block_id {r['block_id_note']}  number={r['number']!r}")
        if args.candidates_dir:
            counts = write_footnote_candidates_per_page(resolved, n_pages, args.candidates_dir)
            print(f"\nPer-page footnote_candidates.json written -> "
                  f"{os.path.abspath(args.candidates_dir)}")
            print(f"Pages with >=1 candidate: {counts}")
        if args.geometry_output:
            write_footnote_geometry(resolved, args.geometry_output)
            print(f"Footnote geometry (glyph bboxes, code-facing) written -> "
                  f"{os.path.abspath(args.geometry_output)}")

    print("\nREVIEW THIS BEFORE PROCEEDING. No PDF has been touched. These are CODE'S "
          "GEOMETRIC CANDIDATES ONLY — the AI still confirms/rejects each one before "
          "any Reference/Note tag is created (rule 9a).")
