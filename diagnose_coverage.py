"""
Coverage diagnostic — run the FULL pipeline over a folder of PDFs and emit a
per-document scorecard, so we can see what fraction of real documents pass
cleanly today and which patterns fail.

Pipeline per document (fully automatic — uses a PLACEHOLDER tagger in place of
the AI so no manual step is needed; the goal is to measure extraction/structure
coverage and PDF/UA pass rate, not AI tag quality):

    extract -> detect tables -> merge -> placeholder tag -> inject -> bookmarks -> veraPDF

Output: a CSV scorecard, one row per document:
    filename | pages | scanned? | extraction_ok? | headings_found | tables_found
    | figures_found | verapdf_pass? | verapdf_error_count | flag_reason

Usage:
    python diagnose_coverage.py <folder-of-pdfs> --output coverage_report.csv
    python diagnose_coverage.py docs --work-dir _diag_work
"""

import argparse
import csv
import contextlib
import io
import json
import logging
import os
import sys
import traceback
import xml.etree.ElementTree as ET

import fitz  # PyMuPDF

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.normalizer import normalize_blocks
from detect_tables import detect_tables
from merge_tables import merge_tables
from inject_tags import inject_tags
from add_bookmarks import add_bookmarks
from validate_pdf import find_verapdf

logging.getLogger().setLevel(logging.ERROR)

# Heuristic thresholds for intake flags
SCANNED_CHARS_PER_PAGE = 50      # below this avg => likely scanned / image-only
NEAR_EMPTY_TOTAL_BLOCKS = 3      # extraction produced almost nothing


@contextlib.contextmanager
def _silence():
    """Suppress noisy stdout from sub-stages during batch runs."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield


def placeholder_tags(blocks):
    """Deterministic type->tag mapping standing in for the AI tagger.

    First heading -> H1, the rest -> H2 (so the heading hierarchy is valid enough
    for veraPDF; the real AI assigns proper H1..H6 levels).
    """
    out = []
    h1_used = False
    for b in blocks:
        t = b["type"]
        if t == "table_cell":
            tag = "TH" if b.get("is_header") else "TD"
        elif t == "list_item":
            tag = "LI"
        elif t == "image":
            tag = "Figure"
        elif t == "heading":
            if not h1_used:
                tag = "H1"
                h1_used = True
            else:
                tag = "H2"
        else:
            tag = "P"
        entry = {"block_id": b["block_id"], "tag": tag}
        if tag == "Figure":
            entry["alt_text"] = "Image"
        out.append(entry)
    return out


def page_text_stats(pdf_path):
    """Return (n_pages, total_text_chars, n_images) for scanned/empty detection."""
    doc = fitz.open(pdf_path)
    total_chars = 0
    n_images = 0
    raw_blocks = []
    for pi, page in enumerate(doc):
        total_chars += len(page.get_text("text"))
        pd = page.get_text("dict")
        for b in pd["blocks"]:
            if b.get("type") == 1:
                n_images += 1
            b["page_idx"] = pi
            raw_blocks.append(b)
    n_pages = len(doc)
    doc.close()
    return n_pages, total_chars, n_images, raw_blocks


def run_verapdf(verapdf, pdf_path, report_path):
    """Run veraPDF, return (compliant: bool, failed_checks: int)."""
    import subprocess
    proc = subprocess.run([verapdf, "-f", "ua1", "--format", "mrr", pdf_path],
                          capture_output=True, text=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(proc.stdout)
    try:
        root = ET.parse(report_path).getroot()
        vr = next((e for e in root.iter() if e.tag.split('}')[-1] == "validationReport"), None)
        det = next((e for e in root.iter() if e.tag.split('}')[-1] == "details"), None)
        compliant = (vr.get("isCompliant") == "true") if vr is not None else False
        failed = int(det.get("failedChecks")) if det is not None else -1
        return compliant, failed
    except Exception:
        return False, -1


def diagnose_one(pdf_path, work_dir, verapdf):
    name = os.path.basename(pdf_path)
    row = {
        "filename": name, "pages": 0, "scanned?": "", "extraction_ok?": "",
        "headings_found": 0, "tables_found": 0, "figures_found": 0,
        "verapdf_pass?": "", "verapdf_error_count": "", "flag_reason": "",
    }
    flags = []
    try:
        wd = os.path.join(work_dir, os.path.splitext(name)[0])
        os.makedirs(wd, exist_ok=True)

        # 1. Page/text stats + raw blocks
        n_pages, total_chars, n_images, raw_blocks = page_text_stats(pdf_path)
        row["pages"] = n_pages
        cpp = total_chars / n_pages if n_pages else 0
        scanned = cpp < SCANNED_CHARS_PER_PAGE and n_images > 0
        row["scanned?"] = "YES" if scanned else "no"
        if scanned:
            flags.append("scanned (needs OCR)")

        # 2. Extract / normalize
        with _silence():
            result = normalize_blocks(raw_blocks, pdf_path)
        blocks = result["blocks"]
        extraction_ok = len(blocks) > NEAR_EMPTY_TOTAL_BLOCKS
        row["extraction_ok?"] = "YES" if extraction_ok else "NO"
        if not extraction_ok:
            flags.append("near-empty extraction")

        blocks_json = os.path.join(wd, "blocks.json")
        with open(blocks_json, "w", encoding="utf-8") as f:
            json.dump(result, f)

        # 3. Detect tables (Docling)
        regions_json = os.path.join(wd, "regions.json")
        try:
            with _silence():
                regions = detect_tables(pdf_path, regions_json)
        except Exception as e:
            regions = []
            flags.append(f"table-detect error")
        row["tables_found"] = len(regions)

        # 4. Merge
        merged_json = os.path.join(wd, "merged.json")
        with _silence():
            merge_tables(blocks_json, regions_json, merged_json)
        merged = json.load(open(merged_json, encoding="utf-8"))["blocks"]

        row["headings_found"] = sum(1 for b in merged if b["type"] == "heading")
        row["figures_found"] = sum(1 for b in merged if b["type"] == "image")
        if row["headings_found"] == 0 and extraction_ok and not scanned:
            flags.append("no headings detected")

        # 5. Placeholder tags
        tags_json = os.path.join(wd, "tags.json")
        with open(tags_json, "w", encoding="utf-8") as f:
            json.dump(placeholder_tags(merged), f)

        # 6. Inject
        tagged_pdf = os.path.join(wd, "tagged.pdf")
        with _silence():
            inject_tags(pdf_path, merged_json, tags_json, tagged_pdf)

        # 7. Bookmarks
        with _silence():
            add_bookmarks(tagged_pdf, merged_json, tags_json, tagged_pdf)

        # 8. veraPDF gate
        if verapdf:
            report = os.path.join(wd, "verapdf.xml")
            compliant, failed = run_verapdf(verapdf, tagged_pdf, report)
            row["verapdf_pass?"] = "PASS" if compliant else "FAIL"
            row["verapdf_error_count"] = failed
            if not compliant:
                flags.append(f"veraPDF fail ({failed})")
        else:
            row["verapdf_pass?"] = "skipped"

    except Exception as e:
        flags.append(f"pipeline error: {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)

    row["flag_reason"] = "; ".join(flags) if flags else "PASS"
    return row


def main():
    ap = argparse.ArgumentParser(description="Coverage diagnostic over a folder of PDFs.")
    ap.add_argument("folder", help="Folder containing PDFs to score")
    ap.add_argument("--output", default="coverage_report.csv", help="CSV scorecard path")
    ap.add_argument("--work-dir", default="_diag_work", help="Scratch dir for intermediates")
    ap.add_argument("--no-verapdf", action="store_true", help="Skip veraPDF (faster)")
    args = ap.parse_args()

    pdfs = sorted(
        os.path.join(args.folder, f) for f in os.listdir(args.folder)
        if f.lower().endswith(".pdf")
    )
    if not pdfs:
        print(f"No PDFs found in {args.folder}")
        return 1

    verapdf = None
    if not args.no_verapdf:
        try:
            verapdf = find_verapdf(None)
        except FileNotFoundError:
            print("  [!] veraPDF not found — running with validation skipped.")

    os.makedirs(args.work_dir, exist_ok=True)
    cols = ["filename", "pages", "scanned?", "extraction_ok?", "headings_found",
            "tables_found", "figures_found", "verapdf_pass?", "verapdf_error_count",
            "flag_reason"]

    rows = []
    for i, pdf in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {os.path.basename(pdf)} ...", flush=True)
        row = diagnose_one(pdf, args.work_dir, verapdf)
        rows.append(row)
        print(f"      -> {row['flag_reason']}", flush=True)

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # Console summary
    clean = sum(1 for r in rows if r["flag_reason"] == "PASS")
    print("\n" + "=" * 60)
    print(f"  Documents: {len(rows)}   Clean PASS: {clean} ({100*clean//max(1,len(rows))}%)")
    print(f"  Flagged for review: {len(rows) - clean}")
    print(f"  Scorecard -> {os.path.abspath(args.output)}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
