"""
Batch process a folder of PDFs through the full PDF/UA tagging pipeline.

Each PDF is processed COMPLETELY INDEPENDENTLY — no shared state between files.
One bad PDF (corrupt, scanned, edge case) is caught and logged; it never stops
the batch. Reuses the exact same pipeline functions as the single-file path and
the coverage diagnostic — this script is purely a loop + isolation + reporting
layer around them.

Pipeline per PDF:
    extract -> detect tables -> merge -> tag -> inject -> bookmarks -> veraPDF

AI TAGGING IN BATCH MODE (the one step that isn't automated):
    --tagger placeholder   (default) Deterministic type->tag mapping, same as
                           diagnose_coverage.py. Fully unattended — use this to
                           get real coverage numbers across 30-50 files today.
    --tagger dir:<folder>  For each Doc.pdf, looks for <folder>/Doc.tags.json
                           (an AI-produced tags file you generated separately).
                           If present, uses it (real AI quality). If missing,
                           the file is still fully processed with placeholder
                           tags to produce a status row, but is marked
                           needs_review / awaiting_ai_tags so you know to
                           supply real tags and re-run with --resume.

STATUS MODEL (three states):
    done          - pipeline completed AND veraPDF passed. Auto-release candidate.
    needs_review  - pipeline completed but flagged (veraPDF fail, scanned,
                    no headings, near-empty extraction, or awaiting AI tags).
    failed        - an exception was caught; the file could not be processed.

RESUME CORRECTNESS (--resume):
    A file is only skipped if it has a `_status.json` marker written AFTER the
    ENTIRE per-file pipeline finished (see _mark_complete). A crash at any
    point — mid-extract, mid-inject, mid-veraPDF, or even after saving the
    tagged PDF but before the marker is written — leaves no marker, so the file
    is correctly REPROCESSED on resume. Never trust "does tagged.pdf exist" —
    only trust the marker written by the last line of successful processing.

Output layout:
    <output-dir>/
        batch_report.csv         one row per PDF (the coverage scorecard)
        batch_summary.txt        totals + itemized needs_review / failed lists
        <DocName>/
            <DocName>_structured_blocks.json
            <DocName>_structured_blocks_merged.json
            tags.json
            <DocName>_tagged.pdf   <- the deliverable
            verapdf.xml
            _status.json           <- resume marker (only on full completion)
            error.log               <- only present if this file failed

Usage:
    python batch_process.py <input_folder> --output-dir <output_folder>
    python batch_process.py docs --output-dir out --resume
    python batch_process.py docs --output-dir out --tagger dir:ai_tags
    python batch_process.py docs --output-dir out --no-verapdf
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
from diagnose_coverage import placeholder_tags, page_text_stats, run_verapdf, \
    SCANNED_CHARS_PER_PAGE, NEAR_EMPTY_TOTAL_BLOCKS

logging.getLogger().setLevel(logging.ERROR)

STATUS_FILE = "_status.json"
COLUMNS = ["filename", "pages", "scanned?", "extraction_ok?", "headings", "tables",
           "figures", "verapdf_pass?", "verapdf_errors", "status", "reason"]


@contextlib.contextmanager
def _silence():
    """Suppress noisy stdout from sub-stages during batch runs."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield


def _is_complete(doc_dir: str) -> dict | None:
    """
    Return the saved row dict if this doc's PREVIOUS run fully completed, else
    None. Completion is determined SOLELY by the presence of a well-formed
    _status.json — that file is written as the very last step of a successful
    run (see _mark_complete), so a crash anywhere before that point (including
    partially-written tagged.pdf) leaves no marker and _is_complete returns
    None, forcing reprocessing.
    """
    status_path = os.path.join(doc_dir, STATUS_FILE)
    if not os.path.isfile(status_path):
        return None
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            row = json.load(f)
    except Exception:
        return None  # corrupt/partial marker -> not complete, reprocess

    # A completed run's status is always "done" or "needs_review" — "failed"
    # runs never write this marker (see process_one), but double-check anyway.
    if row.get("status") not in ("done", "needs_review"):
        return None

    # Belt-and-suspenders: the tagged PDF the marker refers to must still exist
    # and be non-empty, in case of manual deletion/tampering between runs.
    tagged_pdf = row.get("_tagged_pdf")
    if tagged_pdf and (not os.path.isfile(tagged_pdf) or os.path.getsize(tagged_pdf) == 0):
        return None

    return row


def _mark_complete(doc_dir: str, row: dict, tagged_pdf: str) -> None:
    """Write the resume marker. Only called after every pipeline stage succeeded."""
    to_save = dict(row)
    to_save["_tagged_pdf"] = tagged_pdf
    with open(os.path.join(doc_dir, STATUS_FILE), "w", encoding="utf-8") as f:
        json.dump(to_save, f)


def resolve_tags(merged_blocks, pdf_path, tagger_spec, flags):
    """
    Return the tags list for this document per --tagger.
      "placeholder"   -> deterministic mapping, always available.
      "dir:<folder>"  -> <folder>/<stem>.tags.json if present, else placeholder
                          + an 'awaiting_ai_tags' flag so the row is reviewable.
    """
    if tagger_spec.startswith("dir:"):
        ai_dir = tagger_spec[4:]
        stem = os.path.splitext(os.path.basename(pdf_path))[0]
        candidate = os.path.join(ai_dir, f"{stem}.tags.json")
        if os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Accept the same shapes inject_tags.normalize_tags accepts.
            if isinstance(data, dict):
                return data.get("tagged_blocks", []), True
            if isinstance(data, list) and data and isinstance(data[0], dict) \
                    and "tagged_blocks" in data[0]:
                out = []
                for chunk in data:
                    out.extend(chunk.get("tagged_blocks", []))
                return out, True
            return data, True
        flags.append("awaiting_ai_tags")
        return placeholder_tags(merged_blocks), False

    return placeholder_tags(merged_blocks), False


def process_one(pdf_path: str, doc_dir: str, verapdf: str | None, tagger_spec: str) -> dict:
    """
    Run the full pipeline for ONE pdf, completely independently of any other.
    Returns the row dict. Never raises — all failures are caught by the caller
    (kept here as a thin function so the try/except lives at the call site,
    making the isolation boundary obvious in main()).
    """
    name = os.path.basename(pdf_path)
    stem = os.path.splitext(name)[0]
    row = {
        "filename": name, "pages": 0, "scanned?": "", "extraction_ok?": "",
        "headings": 0, "tables": 0, "figures": 0,
        "verapdf_pass?": "", "verapdf_errors": "", "status": "", "reason": "",
    }
    flags = []

    # 1. Page/text stats + raw blocks
    n_pages, total_chars, n_images, raw_blocks = page_text_stats(pdf_path)
    row["pages"] = n_pages
    cpp = total_chars / n_pages if n_pages else 0
    scanned = cpp < SCANNED_CHARS_PER_PAGE and n_images > 0
    row["scanned?"] = "YES" if scanned else "no"
    if scanned:
        flags.append("scanned")

    # 2. Extract / normalize
    with _silence():
        result = normalize_blocks(raw_blocks, pdf_path)
    blocks = result["blocks"]
    extraction_ok = len(blocks) > NEAR_EMPTY_TOTAL_BLOCKS
    row["extraction_ok?"] = "YES" if extraction_ok else "NO"
    if not extraction_ok:
        flags.append("near_empty_extraction")

    blocks_json = os.path.join(doc_dir, f"{stem}_structured_blocks.json")
    with open(blocks_json, "w", encoding="utf-8") as f:
        json.dump(result, f)

    # 3. Detect tables
    regions_json = os.path.join(doc_dir, "regions.json")
    try:
        with _silence():
            regions = detect_tables(pdf_path, regions_json)
    except Exception:
        regions = []
        flags.append("table_detect_error")
        with open(regions_json, "w", encoding="utf-8") as f:
            json.dump([], f)
    row["tables"] = len(regions)

    # 4. Merge
    merged_json = os.path.join(doc_dir, f"{stem}_structured_blocks_merged.json")
    with _silence():
        merge_tables(blocks_json, regions_json, merged_json)
    merged = json.load(open(merged_json, encoding="utf-8"))["blocks"]

    row["headings"] = sum(1 for b in merged if b["type"] == "heading")
    row["figures"] = sum(1 for b in merged if b["type"] == "image")
    if row["headings"] == 0 and extraction_ok and not scanned:
        flags.append("no_headings")

    # 5. Tags (placeholder or pre-supplied AI tags)
    tags = resolve_tags(merged, pdf_path, tagger_spec, flags)[0]
    tags_json = os.path.join(doc_dir, "tags.json")
    with open(tags_json, "w", encoding="utf-8") as f:
        json.dump(tags, f)

    # 6. Inject
    tagged_pdf = os.path.join(doc_dir, f"{stem}_tagged.pdf")
    with _silence():
        inject_tags(pdf_path, merged_json, tags_json, tagged_pdf)

    # 7. Bookmarks
    with _silence():
        add_bookmarks(tagged_pdf, merged_json, tags_json, tagged_pdf)

    # 8. veraPDF gate
    if verapdf:
        report = os.path.join(doc_dir, "verapdf.xml")
        compliant, failed = run_verapdf(verapdf, tagged_pdf, report)
        row["verapdf_pass?"] = "PASS" if compliant else "FAIL"
        row["verapdf_errors"] = failed
        if not compliant:
            flags.append(f"verapdf_failed({failed})")
    else:
        row["verapdf_pass?"] = "skipped"

    # Status: done only if nothing was flagged (includes veraPDF passing).
    row["status"] = "done" if not flags else "needs_review"
    row["reason"] = "; ".join(flags) if flags else ""

    _mark_complete(doc_dir, row, tagged_pdf)
    return row


def write_csv(rows: list[dict], output_path: str) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_summary(rows: list[dict], output_path: str) -> None:
    done = [r for r in rows if r["status"] == "done"]
    review = [r for r in rows if r["status"] == "needs_review"]
    failed = [r for r in rows if r["status"] == "failed"]

    lines = []
    lines.append("=" * 72)
    lines.append(" BATCH PROCESSING SUMMARY")
    lines.append("=" * 72)
    lines.append(f"  Total:         {len(rows)}")
    pct = (100 * len(done) // len(rows)) if rows else 0
    lines.append(f"  Done (auto):   {len(done)} ({pct}%)")
    lines.append(f"  Needs review:  {len(review)}")
    lines.append(f"  Failed:        {len(failed)}")
    lines.append("")

    if review:
        lines.append("-" * 72)
        lines.append(" NEEDS REVIEW (itemized)")
        lines.append("-" * 72)
        # Group by reason so patterns are visible at a glance.
        by_reason = {}
        for r in review:
            key = r["reason"] or "unknown"
            by_reason.setdefault(key, []).append(r["filename"])
        for reason, files in sorted(by_reason.items()):
            lines.append(f"  [{reason}] ({len(files)}):")
            for fn in files:
                lines.append(f"      - {fn}")
        lines.append("")

    if failed:
        lines.append("-" * 72)
        lines.append(" FAILED (itemized)")
        lines.append("-" * 72)
        for r in failed:
            lines.append(f"  - {r['filename']}: {r['reason']}")
        lines.append("")

    lines.append("=" * 72)
    text = "\n".join(lines)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text)
    print("\n" + text)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Batch process a folder of PDFs through the full PDF/UA pipeline."
    )
    ap.add_argument("input_folder", help="Folder containing PDFs to process")
    ap.add_argument("--output-dir", required=True, help="Output/mirror folder")
    ap.add_argument("--tagger", default="placeholder",
                    help="'placeholder' (default) or 'dir:<folder-of-Doc.tags.json>'")
    ap.add_argument("--resume", action="store_true",
                    help="Skip files that already fully completed in a prior run")
    ap.add_argument("--no-verapdf", action="store_true", help="Skip veraPDF (faster)")
    args = ap.parse_args()

    pdfs = sorted(
        os.path.join(args.input_folder, f) for f in os.listdir(args.input_folder)
        if f.lower().endswith(".pdf")
    )
    if not pdfs:
        print(f"No PDFs found in {args.input_folder}")
        return 1

    verapdf = None
    if not args.no_verapdf:
        try:
            verapdf = find_verapdf(None)
        except FileNotFoundError:
            print("  [!] veraPDF not found — running with validation skipped.")

    os.makedirs(args.output_dir, exist_ok=True)
    csv_path = os.path.join(args.output_dir, "batch_report.csv")
    summary_path = os.path.join(args.output_dir, "batch_summary.txt")

    rows = []
    n = len(pdfs)
    for i, pdf_path in enumerate(pdfs, 1):
        name = os.path.basename(pdf_path)
        stem = os.path.splitext(name)[0]
        doc_dir = os.path.join(args.output_dir, stem)
        os.makedirs(doc_dir, exist_ok=True)
        error_log = os.path.join(doc_dir, "error.log")

        print(f"[{i}/{n}] {name} ...", flush=True)

        if args.resume:
            prior = _is_complete(doc_dir)
            if prior is not None:
                rows.append(prior)
                print(f"      -> SKIPPED (already {prior['status']})", flush=True)
                write_csv(rows, csv_path)  # keep the report current even on skips
                continue

        # ISOLATION BOUNDARY: this file's entire pipeline lives inside one
        # try/except. Any exception — corrupt PDF, Docling crash, injection
        # bug, whatever — is caught HERE and never propagates to stop the loop.
        try:
            row = process_one(pdf_path, doc_dir, verapdf, args.tagger)
            if os.path.isfile(error_log):
                os.remove(error_log)  # clear a stale error from a prior failed run
        except Exception as e:
            row = {
                "filename": name, "pages": "", "scanned?": "", "extraction_ok?": "",
                "headings": "", "tables": "", "figures": "",
                "verapdf_pass?": "", "verapdf_errors": "",
                "status": "failed", "reason": f"{type(e).__name__}: {e}",
            }
            with open(error_log, "w", encoding="utf-8") as f:
                f.write(f"{name}\n{'='*60}\n")
                traceback.print_exc(file=f)
            print(f"      -> FAILED: {type(e).__name__}: {e}", flush=True)
        else:
            print(f"      -> {row['status'].upper()}"
                  f"{' (' + row['reason'] + ')' if row['reason'] else ''}", flush=True)

        rows.append(row)
        write_csv(rows, csv_path)  # crash-safe: rewritten after EVERY file

    write_summary(rows, summary_path)
    print(f"\n  Report  -> {os.path.abspath(csv_path)}")
    print(f"  Summary -> {os.path.abspath(summary_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
