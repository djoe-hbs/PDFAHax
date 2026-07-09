"""
Build the TOC destination map: for each tagged TOCI entry, find its matching
body heading (H1-H6) by TEXT, not printed page number, and write a review table
+ JSON map. Printed page number is used only as a sanity-check/tiebreaker.

This is a REVIEW-FIRST step. It does not touch the PDF or create any
annotations. Run it, read the printed table (or open the JSON), and confirm
every match before passing the map to inject_tags.py --toc-map.

Safeguard: an entry with no confident match is written with
matched_heading_block_id = null and is SKIPPED by inject_tags.py's link
creation — it stays a plain (still fully accessible) TOCI, never a guessed link.

Usage:
    python build_toc_destination_map.py \\
        output/Final_Test_Input/Final_Test_Input_structured_blocks_merged.json \\
        tags_merged.json \\
        --output toc_destination_map.json
"""

import argparse
import json
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inject_tags import normalize_tags

HEAD_TAGS = {"H1", "H2", "H3", "H4", "H5", "H6"}


def norm(s: str) -> str:
    """Normalize text for comparison: lowercase, dot-leaders/punctuation stripped."""
    s = s.lower()
    s = re.sub(r'\.{2,}', ' ', s)      # dot leaders "....." -> space
    s = re.sub(r'[^\w\s]', ' ', s)      # strip remaining punctuation
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def extract_page_number(text: str):
    """Pull a trailing page number off a TOC line, e.g. '...INTRODUCTION....4' -> 4."""
    m = re.search(r'(\d+)\s*$', text.strip())
    return int(m.group(1)) if m else None


def strip_trailing_number(text: str) -> str:
    return re.sub(r'\d+\s*$', '', text).strip()


def token_overlap_score(a: str, b: str) -> float:
    """Jaccard similarity over whitespace tokens."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def build_map(blocks_json_path: str, tags_json_path: str):
    blocks = json.load(open(blocks_json_path, encoding="utf-8"))["blocks"]
    bmap = {b["block_id"]: b for b in blocks}
    tags = normalize_tags(json.load(open(tags_json_path, encoding="utf-8")))
    tmap = {t["block_id"]: t for t in tags}

    headings = []
    for bid, t in tmap.items():
        if str(t.get("tag", "")).upper() in HEAD_TAGS:
            b = bmap.get(bid)
            if not b:
                continue
            headings.append({
                "block_id": bid, "level": str(t["tag"]).upper(), "text": b["text"],
                "norm": norm(strip_trailing_number(b["text"])),
                "page_idx": b["page_idx"],
                "top_y": b["bbox"][1] if b.get("bbox") else None,
            })

    tocis = []
    for bid, t in tmap.items():
        tag = str(t.get("tag", "")).upper()
        parent = str(t.get("parent_tag") or "").upper()
        if tag == "TOCI" or (tag != "TOC" and parent == "TOC"):
            b = bmap.get(bid)
            if not b:
                continue
            tocis.append({
                "block_id": bid, "raw_text": b["text"],
                "norm": norm(strip_trailing_number(b["text"])),
                "printed_page": extract_page_number(b["text"]),
                "toc_page_idx": b["page_idx"], "bbox": b.get("bbox"),
            })

    results = []
    used_heading_ids = set()

    for entry in tocis:
        candidates = []
        for h in headings:
            if h["norm"] == entry["norm"]:
                score = 1.0
            else:
                score = token_overlap_score(entry["norm"], h["norm"])
                # Boost for a token-wise prefix/suffix match (handles minor
                # wording drift, e.g. "6.1 INSURANCE Plans" vs "6.1 INSURANCE
                # PLANS"). Must respect word boundaries — a raw substring check
                # would wrongly treat "exempt" as contained in "non exempt".
                a_tok, b_tok = entry["norm"].split(), h["norm"].split()
                shorter, longer = (a_tok, b_tok) if len(a_tok) <= len(b_tok) else (b_tok, a_tok)
                if shorter and (longer[:len(shorter)] == shorter or longer[-len(shorter):] == shorter):
                    score = max(score, 0.9)
            if score > 0:
                candidates.append((score, h))
        candidates.sort(key=lambda c: -c[0])

        best = candidates[0] if candidates else None
        second = candidates[1] if len(candidates) > 1 else None

        if best is None:
            results.append({**entry, "match": None, "confidence": "UNMATCHED", "score": 0.0})
            continue

        score, h = best
        if score >= 0.999:
            conf = "HIGH (exact text match)"
        elif score >= 0.75:
            conf = "MEDIUM (strong overlap)"
        elif score >= 0.5:
            conf = "LOW (partial overlap)"
        else:
            conf = "UNMATCHED (too weak)"
            h = None

        # Ambiguity is only meaningful when the TOP match is itself inexact
        # (score < 1.0). A perfect exact-text match cannot be ambiguous — it IS
        # the entry's own text, regardless of a partial-overlap runner-up.
        if h and score < 0.999 and second and (score - second[0]) < 0.15 and second[0] > 0.4:
            conf = f"AMBIGUOUS (tie with '{second[1]['text'][:40]}')"

        # Printed-page sanity check — informational only, never overrides text.
        page_note = ""
        if h and entry["printed_page"] is not None:
            lo, hi = entry["printed_page"] - 2, entry["printed_page"] + 1
            if not (lo <= h["page_idx"] <= hi):
                page_note = (f" [printed p.{entry['printed_page']} vs "
                             f"body page {h['page_idx']+1} - LARGE GAP]")

        results.append({**entry, "match": h, "confidence": conf + page_note, "score": round(score, 3)})
        if h:
            used_heading_ids.add(h["block_id"])

    unreferenced = [h for h in headings if h["block_id"] not in used_heading_ids]
    return results, unreferenced


def print_review_table(results, unreferenced):
    print(f"{'#':>3}  {'TOC ENTRY':<45} {'MATCHED HEADING':<40} {'BodyPg':>6} {'Printed':>7}  CONFIDENCE")
    print("-" * 140)
    for i, r in enumerate(results):
        entry_disp = r["raw_text"][:43]
        if r["match"]:
            heading_disp = r["match"]["text"][:38]
            body_pg = r["match"]["page_idx"] + 1
        else:
            heading_disp, body_pg = "(no match)", "-"
        printed = r["printed_page"] if r["printed_page"] is not None else "-"
        print(f"{i:>3}  {entry_disp:<45} {heading_disp:<40} {str(body_pg):>6} {str(printed):>7}  {r['confidence']}")

    n_high = sum(1 for r in results if r["confidence"].startswith("HIGH"))
    n_med = sum(1 for r in results if r["confidence"].startswith("MEDIUM"))
    n_low = sum(1 for r in results if r["confidence"].startswith("LOW"))
    n_amb = sum(1 for r in results if r["confidence"].startswith("AMBIGUOUS"))
    n_un = sum(1 for r in results if r["confidence"].startswith("UNMATCHED"))
    print(f"\nTotal: {len(results)}  HIGH={n_high}  MEDIUM={n_med}  LOW={n_low}  "
          f"AMBIGUOUS={n_amb}  UNMATCHED={n_un}")

    print(f"\nBody headings with NO TOC entry pointing to them: {len(unreferenced)}")
    for h in unreferenced:
        print(f"   [{h['level']}] p.{h['page_idx']+1}: {h['text'][:60]}")


def write_map(results, output_path):
    out = []
    for r in results:
        out.append({
            "toc_block_id": r["block_id"],
            "toc_text": r["raw_text"],
            "toc_page_idx": r["toc_page_idx"],
            "printed_page": r["printed_page"],
            "matched_heading_block_id": r["match"]["block_id"] if r["match"] else None,
            "matched_heading_text": r["match"]["text"] if r["match"] else None,
            "matched_page_idx": r["match"]["page_idx"] if r["match"] else None,
            "matched_top_y": r["match"]["top_y"] if r["match"] else None,
            "score": r["score"],
            "confidence": r["confidence"],
        })
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("blocks_json", help="structured_blocks_merged.json")
    ap.add_argument("tags_json", help="AI tags JSON (tags_merged.json)")
    ap.add_argument("--output", default="toc_destination_map.json", help="Output map path")
    args = ap.parse_args()

    results, unreferenced = build_map(args.blocks_json, args.tags_json)
    print_review_table(results, unreferenced)
    write_map(results, args.output)
    print(f"\nMap written -> {os.path.abspath(args.output)}")
    print("REVIEW THIS BEFORE PROCEEDING. Then run inject_tags.py with --toc-map "
          f"{args.output} to create the approved links.")
