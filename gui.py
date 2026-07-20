"""
CustomTkinter desktop GUI for the PDF/UA auto-tagging pipeline — multi-PDF,
manual-AI-tagging flow (matches guide to run.txt steps 1-7 exactly).

Per PDF, two phases, because AI tagging is a MANUAL step (you paste tags from
an external AI model — the pipeline cannot run end-to-end automatically):

  PHASE 1 (steps 1-4): extract -> detect tables -> merge -> write
                        blocks_for_ai.json. Copy it (+ the `prompt` file) to
                        your AI, get tags_merged.json back.
  MANUAL STEP:          paste (or load) that tags_merged.json for this PDF.
  PHASE 2 (steps 5-7):  inject -> visualize -> bookmarks -> veraPDF.

Files are processed one at a time from a queue; each has its own Phase 1 /
paste-tags / Phase 2 state, so you can prepare all of them, paste tags for
each, and run Phase 2 across the whole batch.

Final outputs per PDF: <stem>_tagged.pdf, <stem>_tagged_visual.pdf, plus one
combined coverage_and_verapdf.xlsx workbook covering every processed PDF (a
summary sheet + one detail sheet per PDF listing its failed veraPDF rules).

Thin front-end only — every pipeline stage reuses the existing functions
unchanged (src.normalizer.normalize_blocks, detect_tables.detect_tables,
merge_tables.merge_tables, inject_tags.inject_tags, visualize_tags.visualize_tags,
add_bookmarks.add_bookmarks, validate_pdf.find_verapdf/_local/_find/_findall).
No pipeline logic is duplicated here.

Launch:
    python gui.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import webbrowser
import xml.etree.ElementTree as ET

import customtkinter as ctk
from tkinter import filedialog, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fitz  # PyMuPDF
import openpyxl
from openpyxl.styles import Font, PatternFill

from src.normalizer import normalize_blocks
from detect_tables import detect_tables
from merge_tables import merge_tables
from inject_tags import inject_tags, normalize_tags
from visualize_tags import visualize_tags
from add_bookmarks import add_bookmarks
from validate_pdf import find_verapdf, _local, _find, _findall
from ai_tagger import tag_document

ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")

PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt")


def copy_to_system_clipboard(text: str) -> None:
    """
    Copy text to the OS clipboard so it survives after this process exits or
    loses focus.

    Tk's own clipboard_clear()/clipboard_append() only work while Tk keeps
    pumping its event loop and owns clipboard rendering (WM_RENDERFORMAT on
    Windows) — content placed there can silently vanish or fail to paste into
    other apps once the window loses focus or closes. On Windows we shell out
    to clip.exe, which does a real one-shot SetClipboardData that persists
    independently of this process. Falls back to Tk's clipboard elsewhere.
    """
    if os.name == "nt":
        # clip.exe reads from stdin; text must be bytes for it to preserve
        # non-ASCII characters correctly (utf-16 is what Windows clipboard uses).
        proc = subprocess.Popen(["clip.exe"], stdin=subprocess.PIPE)
        proc.communicate(input=text.encode("utf-16-le"))
        return
    # Non-Windows fallback: best effort via Tk (caller's root window).
    raise NotImplementedError("Use the Tk clipboard fallback on non-Windows platforms.")


def build_blocks_for_ai(merged_blocks: list[dict]) -> list[dict]:
    """Same slim projection used throughout the project's manual AI-tagging flow."""
    slim = []
    for b in merged_blocks:
        e = {
            "block_id": b["block_id"], "type": b["type"], "text": b["text"][:120],
            "bbox": b["bbox"], "metadata": b.get("metadata", {}),
        }
        if b["type"] == "table_cell":
            e["is_header"] = b["is_header"]
            e["row"] = b["row"]
            e["col"] = b["col"]
        slim.append(e)
    return slim


def run_verapdf_detailed(verapdf, pdf_path, report_path):
    """
    Run veraPDF, return (compliant, failed_checks, [ {clause, test, failed, desc}, ... ] ).
    Reuses validate_pdf's own XML-walking helpers so parsing logic isn't duplicated.
    """
    proc = subprocess.run([verapdf, "-f", "ua1", "--format", "mrr", pdf_path],
                          capture_output=True, text=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(proc.stdout)

    try:
        root = ET.parse(report_path).getroot()
    except ET.ParseError:
        return False, -1, []

    vr = _find(root, "validationReport")
    details = _find(root, "details")
    compliant = (vr.get("isCompliant") == "true") if vr is not None else False
    failed_total = int(details.get("failedChecks")) if details is not None else -1

    rules = []
    for r in _findall(root, "rule"):
        if r.get("status") != "failed":
            continue
        desc_el = _find(r, "description")
        rules.append({
            "clause": r.get("clause"),
            "test": r.get("testNumber"),
            "failed": int(r.get("failedChecks", 0)),
            "desc": (desc_el.text or "").strip() if desc_el is not None else "",
        })
    rules.sort(key=lambda x: x["failed"], reverse=True)
    return compliant, failed_total, rules


class PdfJob:
    """Per-PDF state carried through both phases."""

    def __init__(self, path):
        self.pdf_path = path
        self.name = os.path.basename(path)
        self.stem = os.path.splitext(self.name)[0]
        self.doc_dir = None
        self.merged_json_path = None
        self.blocks_for_ai_path = None
        self.blocks_for_ai_text = ""
        self.tagged_pdf_path = None
        self.visual_pdf_path = None
        self.phase1_done = False
        self.tags_pasted = False
        self.phase2_done = False
        self.status = "pending"          # pending -> prepared -> done / needs_review / failed
        self.verapdf_pass = ""
        self.verapdf_errors = ""
        self.verapdf_rules = []
        self.error = ""


class PDFATaggerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("PDF/UA Auto-Tagger")
        self.geometry("980x860")

        self.work_dir = tempfile.mkdtemp(prefix="pdfa_gui_")
        self.jobs: list[PdfJob] = []
        self.current_job: PdfJob | None = None

        try:
            self.verapdf_path = find_verapdf(None)
        except FileNotFoundError:
            self.verapdf_path = None

        self._build_layout()

        if self.verapdf_path is None:
            self._set_status(
                "[!] veraPDF not found — validation will be SKIPPED for this run.",
                "warn",
            )

    # ── Layout ───────────────────────────────────────────────────────────
    def _build_layout(self):
        pad = {"padx": 12, "pady": 6}

        # Everything lives inside a scrollable frame, since the stacked
        # sections (file picker, Phase 1, manual step, Phase 2, status,
        # results table) are taller than most screens can show at once —
        # without this, Phase 2 and everything after it is pushed off-window
        # with no way to reach it.
        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)

        # File picker (multi)
        file_frame = ctk.CTkFrame(body)
        file_frame.pack(fill="x", **pad)
        ctk.CTkButton(file_frame, text="Choose PDF(s)...", command=self._choose_pdfs).pack(side="left", padx=8, pady=8)
        self.file_select = ctk.CTkOptionMenu(file_frame, values=["(no files loaded)"], command=self._on_select_job)
        self.file_select.pack(side="left", padx=8)
        self.file_status_label = ctk.CTkLabel(file_frame, text="0 file(s) loaded")
        self.file_status_label.pack(side="left", padx=8)

        # Phase 1
        p1_frame = ctk.CTkFrame(body)
        p1_frame.pack(fill="x", **pad)
        ctk.CTkLabel(p1_frame, text="Phase 1 — Prepare (steps 1-4)", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=8, pady=(8, 0))
        btn_row = ctk.CTkFrame(p1_frame, fg_color="transparent")
        btn_row.pack(fill="x", padx=8, pady=6)
        self.extract_btn = ctk.CTkButton(btn_row, text="Extract & Prepare (this file)", command=self._start_extract)
        self.extract_btn.pack(side="left")
        self.extract_all_btn = ctk.CTkButton(btn_row, text="Prepare ALL files", command=self._start_extract_all)
        self.extract_all_btn.pack(side="left", padx=8)
        self.copy_blocks_btn = ctk.CTkButton(btn_row, text="Copy blocks_for_ai.json", command=self._copy_blocks_for_ai, state="disabled")
        self.copy_blocks_btn.pack(side="left", padx=8)
        self.save_blocks_btn = ctk.CTkButton(btn_row, text="Save blocks_for_ai.json As...", command=self._save_blocks_for_ai, state="disabled")
        self.save_blocks_btn.pack(side="left")
        self.copy_prompt_btn = ctk.CTkButton(btn_row, text="Copy AI prompt", command=self._copy_prompt)
        self.copy_prompt_btn.pack(side="left", padx=8)
        self.auto_tag_btn = ctk.CTkButton(btn_row, text="\u2728 Auto-Tag with Gemini", command=self._start_auto_tag, state="disabled",
                                          fg_color="#6c47d9", hover_color="#5535b8")
        self.auto_tag_btn.pack(side="left", padx=8)

        self.p1_progress_label = ctk.CTkLabel(p1_frame, text="", anchor="w")
        self.p1_progress_label.pack(fill="x", padx=8)
        self.p1_progress = ctk.CTkProgressBar(p1_frame, mode="determinate")
        self.p1_progress.pack(fill="x", padx=8, pady=(0, 6))
        self.p1_progress.set(0)

        self.blocks_path_label = ctk.CTkLabel(p1_frame, text="", anchor="w", justify="left")
        self.blocks_path_label.pack(fill="x", padx=8)
        self.blocks_ai_box = ctk.CTkTextbox(p1_frame, height=130)
        self.blocks_ai_box.pack(fill="x", padx=8, pady=(4, 8))
        self.blocks_ai_box.configure(state="disabled")

        # Manual step
        manual_frame = ctk.CTkFrame(body)
        manual_frame.pack(fill="both", expand=True, **pad)
        ctk.CTkLabel(manual_frame, text="Manual step — paste this file's AI tags (tags_merged.json)",
                    font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=8, pady=(8, 0))
        ctk.CTkButton(manual_frame, text="Load tags from file...", command=self._load_tags_file).pack(anchor="w", padx=8, pady=6)
        self.tags_box = ctk.CTkTextbox(manual_frame, height=160)
        self.tags_box.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        # Phase 2
        p2_frame = ctk.CTkFrame(body)
        p2_frame.pack(fill="x", **pad)
        ctk.CTkLabel(p2_frame, text="Phase 2 — Inject & Finish (steps 5-7)", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=8, pady=(8, 0))
        p2_btn_row = ctk.CTkFrame(p2_frame, fg_color="transparent")
        p2_btn_row.pack(fill="x", padx=8, pady=6)
        self.inject_btn = ctk.CTkButton(p2_btn_row, text="Inject Tags & Finish (this file)", command=self._start_inject)
        self.inject_btn.pack(side="left")
        self.export_xlsx_btn = ctk.CTkButton(p2_btn_row, text="Export veraPDF Report (.xlsx)", command=self._export_xlsx, state="disabled")
        self.export_xlsx_btn.pack(side="left", padx=8)

        self.p2_progress_label = ctk.CTkLabel(p2_frame, text="", anchor="w")
        self.p2_progress_label.pack(fill="x", padx=8)
        self.p2_progress = ctk.CTkProgressBar(p2_frame, mode="determinate")
        self.p2_progress.pack(fill="x", padx=8, pady=(0, 8))
        self.p2_progress.set(0)

        # Status / results
        status_frame = ctk.CTkFrame(body)
        status_frame.pack(fill="x", **pad)
        ctk.CTkLabel(status_frame, text="Status", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=8, pady=(8, 0))
        self.status_label = ctk.CTkLabel(status_frame, text="Idle.", anchor="w", justify="left", wraplength=920)
        self.status_label.pack(fill="x", padx=8, pady=4)
        self._status_default_color = self.status_label.cget("text_color")

        result_row = ctk.CTkFrame(status_frame)
        result_row.pack(fill="x", padx=8, pady=(0, 8))
        self.save_btn = ctk.CTkButton(result_row, text="Save tagged PDF...", command=self._save_tagged_pdf, state="disabled")
        self.save_btn.pack(side="left")
        self.save_visual_btn = ctk.CTkButton(result_row, text="Save visualize PDF...", command=self._save_visual_pdf, state="disabled")
        self.save_visual_btn.pack(side="left", padx=10)
        self.open_folder_btn = ctk.CTkButton(result_row, text="Open output folder", command=self._open_output_folder)
        self.open_folder_btn.pack(side="left")

        # Results table (simple text-based, one line per file)
        table_frame = ctk.CTkFrame(body)
        table_frame.pack(fill="both", expand=False, **pad)
        ctk.CTkLabel(table_frame, text="All files", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=8, pady=(8, 0))
        self.results_box = ctk.CTkTextbox(table_frame, height=110)
        self.results_box.pack(fill="x", padx=8, pady=(4, 8))
        self.results_box.configure(state="disabled")

    def _set_status(self, text, kind="info"):
        color = {"error": "#e03131", "warn": "#e8a33d", "success": "#2f9e44"}.get(
            kind, self._status_default_color)
        self.status_label.configure(text=text, text_color=color)

    # ── File picker (multi) ─────────────────────────────────────────────
    def _choose_pdfs(self):
        paths = filedialog.askopenfilenames(title="Choose PDF(s)", filetypes=[("PDF files", "*.pdf")])
        if not paths:
            return
        self.jobs = [PdfJob(p) for p in paths]
        self.current_job = self.jobs[0]
        names = [j.name for j in self.jobs]
        self.file_select.configure(values=names)
        self.file_select.set(names[0])
        self.file_status_label.configure(text=f"{len(self.jobs)} file(s) loaded")
        self._refresh_job_view()
        self._refresh_results_table()
        self._set_status(f"{len(self.jobs)} PDF(s) selected. Click 'Extract & Prepare' to continue.")

    def _job_by_name(self, name):
        return next((j for j in self.jobs if j.name == name), None)

    def _on_select_job(self, name):
        job = self._job_by_name(name)
        if job:
            self.current_job = job
            self._refresh_job_view()

    def _refresh_job_view(self):
        job = self.current_job
        if job is None:
            return
        self.blocks_path_label.configure(text=job.blocks_for_ai_path or "")
        self._set_textbox(self.blocks_ai_box, job.blocks_for_ai_text)
        self.copy_blocks_btn.configure(state="normal" if job.blocks_for_ai_text else "disabled")
        self.save_blocks_btn.configure(state="normal" if job.blocks_for_ai_text else "disabled")
        self.auto_tag_btn.configure(state="normal" if job.phase1_done else "disabled")

        self.tags_box.delete("1.0", "end")
        # tags_box holds per-job pasted text; stash it on the job itself.
        if hasattr(job, "_pasted_tags_text"):
            self.tags_box.insert("1.0", job._pasted_tags_text)

        self.save_btn.configure(state="normal" if job.tagged_pdf_path else "disabled")
        self.save_visual_btn.configure(state="normal" if job.visual_pdf_path else "disabled")
        self.export_xlsx_btn.configure(state="normal" if any(j.phase2_done for j in self.jobs) else "disabled")

    def _refresh_results_table(self):
        icon = {"pending": "-", "prepared": "*", "done": "[OK]",
                "needs_review": "[!]", "failed": "[X]"}
        lines = []
        for j in self.jobs:
            vera = f"{j.verapdf_pass}" if j.verapdf_pass else ""
            reason = j.error or (f"{j.verapdf_errors} veraPDF check(s) failed" if j.status == "needs_review" else "")
            lines.append(f"{icon.get(j.status, '-'):5} {j.name:<40} status={j.status:<13} verapdf={vera:<8} {reason}")
        self._set_textbox(self.results_box, "\n".join(lines))

    # ── Phase 1: Extract & Prepare ───────────────────────────────────────
    def _start_extract(self):
        if not self.current_job:
            messagebox.showinfo("No PDF", "Choose PDF(s) first.")
            return
        self._run_phase1([self.current_job])

    def _start_extract_all(self):
        if not self.jobs:
            messagebox.showinfo("No PDF", "Choose PDF(s) first.")
            return
        self._run_phase1(self.jobs)

    def _run_phase1(self, jobs):
        self.extract_btn.configure(state="disabled")
        self.extract_all_btn.configure(state="disabled")
        self.p1_progress.set(0)
        self.p1_progress_label.configure(text=f"0 / {len(jobs)} prepared")
        self._set_status(f"Preparing {len(jobs)} file(s) ...")
        threading.Thread(target=self._phase1_worker, args=(jobs,), daemon=True).start()

    # Phase 1 sub-stages, for the "working on X: <stage>" status line. Table
    # detection (Docling) is by far the slowest — it loads ML models on first
    # use, which can take 30-90+ seconds even for a 1-2 page PDF. That cost is
    # per-process (not cached across separate `python` runs), so it happens
    # again each time the GUI is launched fresh. Surfacing the stage name is
    # what turns "looks stuck" into "on Detect Tables (this step is slow)".
    _PHASE1_STAGES = ["Extract", "Detect Tables (slow on first run)", "Merge", "Build AI input"]

    def _phase1_worker(self, jobs):
        total = len(jobs)
        for i, job in enumerate(jobs, 1):
            self.after(0, self._phase1_progress, i - 1, total, job.name, self._PHASE1_STAGES[0])
            try:
                doc_dir = os.path.join(self.work_dir, job.stem)
                os.makedirs(doc_dir, exist_ok=True)

                # 1. Extract (PyMuPDF raw blocks -> canonical normalized JSON)
                doc = fitz.open(job.pdf_path)
                raw_blocks = []
                for page_idx, page in enumerate(doc):
                    page_dict = page.get_text("dict")
                    for b in page_dict["blocks"]:
                        b["page_idx"] = page_idx
                        raw_blocks.append(b)
                doc.close()
                result = normalize_blocks(raw_blocks, job.pdf_path)
                blocks_json = os.path.join(doc_dir, f"{job.stem}_structured_blocks.json")
                with open(blocks_json, "w", encoding="utf-8") as f:
                    json.dump(result, f)

                # 2. Detect tables (slow — see _PHASE1_STAGES note above)
                self.after(0, self._phase1_progress, i - 1, total, job.name, self._PHASE1_STAGES[1])
                regions_json = os.path.join(doc_dir, "regions.json")
                try:
                    detect_tables(job.pdf_path, regions_json)
                except Exception:
                    with open(regions_json, "w", encoding="utf-8") as f:
                        json.dump([], f)

                # 3. Merge
                self.after(0, self._phase1_progress, i - 1, total, job.name, self._PHASE1_STAGES[2])
                merged_json = os.path.join(doc_dir, f"{job.stem}_structured_blocks_merged.json")
                merge_tables(blocks_json, regions_json, merged_json)
                merged = json.load(open(merged_json, encoding="utf-8"))["blocks"]

                # 4. Build blocks_for_ai.json
                self.after(0, self._phase1_progress, i - 1, total, job.name, self._PHASE1_STAGES[3])
                slim = build_blocks_for_ai(merged)
                blocks_for_ai_path = os.path.join(doc_dir, "blocks_for_ai.json")
                preview = json.dumps(slim, indent=2)
                with open(blocks_for_ai_path, "w", encoding="utf-8") as f:
                    f.write(preview)

                job.doc_dir = doc_dir
                job.merged_json_path = merged_json
                job.blocks_for_ai_path = blocks_for_ai_path
                job.blocks_for_ai_text = preview
                job.phase1_done = True
                job.status = "prepared"
            except Exception as e:
                job.error = f"{type(e).__name__}: {e}"
                job.status = "failed"
                print(traceback.format_exc(), file=sys.stderr)
            self.after(0, self._phase1_progress, i, total, job.name, None)

        self.after(0, self._phase1_done)

    def _phase1_progress(self, done, total, current_name, stage=None):
        self.p1_progress.set(done / total if total else 0)
        if done < total:
            stage_txt = f" — {stage} ..." if stage else " ..."
            self.p1_progress_label.configure(
                text=f"{done} / {total} prepared — working on {current_name}{stage_txt}")
        else:
            self.p1_progress_label.configure(text=f"{done} / {total} prepared")

    def _phase1_done(self):
        self.extract_btn.configure(state="normal")
        self.extract_all_btn.configure(state="normal")
        self._refresh_job_view()
        self._refresh_results_table()
        n_ok = sum(1 for j in self.jobs if j.phase1_done)
        self._set_status(f"Prepared {n_ok}/{len(self.jobs)} file(s). "
                         f"Copy blocks_for_ai.json + the prompt to your AI, then paste tags below.",
                         "success" if n_ok else "error")

    def _set_textbox(self, box, text):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    # ── Copy / save blocks_for_ai.json + prompt ─────────────────────────
    def _copy_text(self, text: str) -> None:
        """Copy to the OS clipboard so it's still there after this window loses
        focus (see copy_to_system_clipboard for why Tk's own clipboard isn't used)."""
        try:
            copy_to_system_clipboard(text)
        except NotImplementedError:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update()  # required for Tk's clipboard fallback to take ownership

    def _copy_blocks_for_ai(self):
        job = self.current_job
        if not job or not job.blocks_for_ai_text:
            return
        self._copy_text(job.blocks_for_ai_text)
        self._set_status(f"Copied blocks_for_ai.json ({job.name}) to clipboard.", "success")

    def _save_blocks_for_ai(self):
        job = self.current_job
        if not job or not job.blocks_for_ai_path:
            return
        dest = filedialog.asksaveasfilename(
            title="Save blocks_for_ai.json", initialfile=f"{job.stem}_blocks_for_ai.json",
            defaultextension=".json", filetypes=[("JSON files", "*.json")],
        )
        if not dest:
            return
        try:
            shutil.copyfile(job.blocks_for_ai_path, dest)
        except OSError as e:
            messagebox.showerror("Save failed", f"Could not write to:\n{dest}\n\n{type(e).__name__}: {e}")
            return
        messagebox.showinfo("Saved", f"Saved to {dest}")

    def _copy_prompt(self):
        if not os.path.isfile(PROMPT_PATH):
            messagebox.showwarning("Not found", f"Prompt file not found at {PROMPT_PATH}")
            return
        with open(PROMPT_PATH, "r", encoding="utf-8") as f:
            text = f.read()
        self._copy_text(text)
        self._set_status("Copied AI tagging prompt to clipboard.", "success")

    # ── Auto-Tag with Gemini ─────────────────────────────────────────────
    def _start_auto_tag(self):
        job = self.current_job
        if not job or not job.phase1_done:
            self._set_status("Run 'Extract & Prepare' for this file first.", "error")
            return
        if not job.merged_json_path:
            self._set_status("Merged blocks not found — run Phase 1 again.", "error")
            return

        self.auto_tag_btn.configure(state="disabled")
        self.extract_btn.configure(state="disabled")
        self.extract_all_btn.configure(state="disabled")
        self._set_status(f"Auto-tagging {job.name} with Gemini...")
        self.p1_progress.set(0)
        threading.Thread(target=self._auto_tag_worker, args=(job,), daemon=True).start()

    def _auto_tag_worker(self, job):
        try:
            with open(job.merged_json_path, "r", encoding="utf-8") as f:
                merged = json.load(f)["blocks"]

            # Determine footnote candidates dir (if it exists alongside the merged JSON)
            fn_dir = os.path.join(job.doc_dir, "footnote_candidates") if job.doc_dir else None
            if fn_dir and not os.path.isdir(fn_dir):
                fn_dir = None

            total_pages = len(set(b["page_idx"] for b in merged))

            def progress_cb(page_idx, total, status):
                if page_idx >= 0:
                    frac = (page_idx + 1) / total if total else 0
                    self.after(0, self._auto_tag_progress, frac, status)
                else:
                    self.after(0, self._auto_tag_progress, 1.0, status)

            tags = tag_document(
                merged,
                footnote_candidates_dir=fn_dir,
                progress_cb=progress_cb,
            )

            tags_text = json.dumps(tags, indent=2)
            self.after(0, self._auto_tag_done, job, tags_text)

        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            print(traceback.format_exc(), file=sys.stderr)
            self.after(0, self._auto_tag_failed, job, err_msg)

    def _auto_tag_progress(self, fraction, status_text):
        self.p1_progress.set(fraction)
        self.p1_progress_label.configure(text=status_text)

    def _auto_tag_done(self, job, tags_text):
        # Fill the tags box with the AI's response
        self.tags_box.delete("1.0", "end")
        self.tags_box.insert("1.0", tags_text)
        job._pasted_tags_text = tags_text
        job.tags_pasted = True

        self.auto_tag_btn.configure(state="normal")
        self.extract_btn.configure(state="normal")
        self.extract_all_btn.configure(state="normal")
        self.p1_progress.set(1.0)
        self._set_status(
            f"\u2705 {job.name} auto-tagged successfully! "
            f"Review the tags below, then click 'Inject Tags & Finish'.",
            "success",
        )

    def _auto_tag_failed(self, job, error_msg):
        self.auto_tag_btn.configure(state="normal")
        self.extract_btn.configure(state="normal")
        self.extract_all_btn.configure(state="normal")
        self.p1_progress.set(0)
        self.p1_progress_label.configure(text="Auto-tag failed")
        self._set_status(
            f"Auto-tag failed for {job.name}: {error_msg}\n"
            f"You can still paste tags manually.",
            "error",
        )

    # ── Manual step: load tags from file ────────────────────────────────
    def _load_tags_file(self):
        path = filedialog.askopenfilename(title="Choose tags JSON", filetypes=[("JSON files", "*.json")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            messagebox.showerror("Load failed", f"Could not read file: {e}")
            return
        self.tags_box.delete("1.0", "end")
        self.tags_box.insert("1.0", content)
        self._set_status(f"Loaded tags from {os.path.basename(path)}.")

    # ── Phase 2: Inject & Finish ─────────────────────────────────────────
    def _start_inject(self):
        job = self.current_job
        if not job or not job.merged_json_path:
            self._set_status("Run 'Extract & Prepare' for this file first.", "error")
            return

        raw = self.tags_box.get("1.0", "end").strip()
        if not raw:
            self._set_status("Paste (or load) the AI tags JSON before injecting.", "error")
            return
        job._pasted_tags_text = raw

        try:
            tags_data = json.loads(raw)
        except json.JSONDecodeError as e:
            self._set_status(f"Invalid JSON in tags box: {e}", "error")
            return

        try:
            tags = normalize_tags(tags_data)
        except Exception as e:
            self._set_status(f"Could not interpret tags JSON: {e}", "error")
            return

        if not isinstance(tags, list) or not tags:
            self._set_status("Tags JSON parsed but contains no taggable blocks.", "error")
            return

        tags_json_path = os.path.join(job.doc_dir, "tags.json")
        with open(tags_json_path, "w", encoding="utf-8") as f:
            json.dump(tags, f)
        job.tags_pasted = True

        self.inject_btn.configure(state="disabled")
        self.save_btn.configure(state="disabled")
        self.save_visual_btn.configure(state="disabled")
        self._phase2_progress(0, job.name)
        self._set_status(f"Injecting tags for {job.name} ...")
        threading.Thread(target=self._inject_worker, args=(job, tags_json_path), daemon=True).start()

    # Phase 2 has 4 discrete steps: inject, visualize, bookmarks, veraPDF.
    _PHASE2_STEPS = ["Inject", "Visualize", "Bookmarks", "veraPDF"]

    def _phase2_progress(self, step, name):
        total = len(self._PHASE2_STEPS)
        self.p2_progress.set(step / total)
        if step < total:
            self.p2_progress_label.configure(text=f"{name}: step {step + 1}/{total} — {self._PHASE2_STEPS[step]} ...")
        else:
            self.p2_progress_label.configure(text=f"{name}: done ({total}/{total})")

    def _inject_worker(self, job, tags_json_path):
        try:
            tagged_pdf = os.path.join(job.doc_dir, f"{job.stem}_tagged.pdf")
            visual_pdf = os.path.join(job.doc_dir, f"{job.stem}_tagged_visual.pdf")

            # 5. Inject
            self.after(0, self._phase2_progress, 0, job.name)
            inject_tags(job.pdf_path, job.merged_json_path, tags_json_path, tagged_pdf)

            # 6. Visualize (colored tag boxes, drawn from the original PDF)
            self.after(0, self._phase2_progress, 1, job.name)
            visualize_tags(job.pdf_path, job.merged_json_path, tags_json_path, visual_pdf)

            # 6b. Bookmarks (on the tagged PDF)
            self.after(0, self._phase2_progress, 2, job.name)
            add_bookmarks(tagged_pdf, job.merged_json_path, tags_json_path, tagged_pdf)

            # 7. veraPDF (optional)
            self.after(0, self._phase2_progress, 3, job.name)
            verapdf_pass, verapdf_errors, rules = "skipped", "", []
            if self.verapdf_path:
                report = os.path.join(job.doc_dir, "verapdf.xml")
                compliant, failed, rules = run_verapdf_detailed(self.verapdf_path, tagged_pdf, report)
                verapdf_pass = "PASS" if compliant else "FAIL"
                verapdf_errors = failed

            status = "done"
            if verapdf_pass == "FAIL":
                status = "needs_review"

            job.tagged_pdf_path = tagged_pdf
            job.visual_pdf_path = visual_pdf
            job.verapdf_pass = verapdf_pass
            job.verapdf_errors = verapdf_errors
            job.verapdf_rules = rules
            job.phase2_done = True
            job.status = status

            self.after(0, self._phase2_progress, 4, job.name)
            self.after(0, self._inject_done, job)
        except Exception as e:
            job.error = f"{type(e).__name__}: {e}"
            job.status = "failed"
            print(traceback.format_exc(), file=sys.stderr)
            self.after(0, self._inject_failed, job)

    def _inject_done(self, job):
        self.inject_btn.configure(state="normal")
        self._refresh_job_view()
        self._refresh_results_table()

        vera_note = f"veraPDF: {job.verapdf_pass}"
        if job.verapdf_pass == "FAIL":
            vera_note += f" ({job.verapdf_errors} failed checks)"
        elif job.verapdf_pass == "skipped":
            vera_note += " (not installed — validation skipped)"

        kind = "success" if job.status == "done" else "warn"
        self._set_status(
            f"{job.name} — status: {job.status}.\n"
            f"Tagged: {job.tagged_pdf_path}\nVisualize: {job.visual_pdf_path}\n{vera_note}", kind,
        )

    def _inject_failed(self, job):
        self.p2_progress.set(0)
        self.p2_progress_label.configure(text=f"{job.name}: failed")
        self.inject_btn.configure(state="normal")
        self._refresh_results_table()
        self._set_status(f"Inject Tags & Finish failed for {job.name}: {job.error}", "error")

    # ── Save / open folder ───────────────────────────────────────────────
    def _save_tagged_pdf(self):
        job = self.current_job
        if not job or not job.tagged_pdf_path or not os.path.isfile(job.tagged_pdf_path):
            messagebox.showwarning("Not available", "No tagged PDF has been produced yet.")
            return
        dest = filedialog.asksaveasfilename(
            title="Save tagged PDF", initialfile=os.path.basename(job.tagged_pdf_path),
            defaultextension=".pdf", filetypes=[("PDF files", "*.pdf")],
        )
        if not dest:
            return
        try:
            shutil.copyfile(job.tagged_pdf_path, dest)
        except OSError as e:
            messagebox.showerror(
                "Save failed",
                f"Could not write to:\n{dest}\n\n{type(e).__name__}: {e}\n\n"
                "The destination file may be open in another program (e.g. a PDF "
                "viewer) or you may not have permission to write there. Close it "
                "or choose a different location and try again.",
            )
            return
        messagebox.showinfo("Saved", f"Saved to {dest}")

    def _save_visual_pdf(self):
        job = self.current_job
        if not job or not job.visual_pdf_path or not os.path.isfile(job.visual_pdf_path):
            messagebox.showwarning("Not available", "No visualize PDF has been produced yet.")
            return
        dest = filedialog.asksaveasfilename(
            title="Save visualize PDF", initialfile=os.path.basename(job.visual_pdf_path),
            defaultextension=".pdf", filetypes=[("PDF files", "*.pdf")],
        )
        if not dest:
            return
        try:
            shutil.copyfile(job.visual_pdf_path, dest)
        except OSError as e:
            messagebox.showerror(
                "Save failed",
                f"Could not write to:\n{dest}\n\n{type(e).__name__}: {e}\n\n"
                "The destination file may be open in another program (e.g. a PDF "
                "viewer) or you may not have permission to write there. Close it "
                "or choose a different location and try again.",
            )
            return
        messagebox.showinfo("Saved", f"Saved to {dest}")

    def _open_output_folder(self):
        job = self.current_job
        path = (job.doc_dir if job else None) or self.work_dir
        path = os.path.abspath(path)
        if os.name == "nt":
            os.startfile(path)
        else:
            webbrowser.open(f"file://{path}")

    # ── Excel veraPDF report (all processed files) ──────────────────────
    def _export_xlsx(self):
        done_jobs = [j for j in self.jobs if j.phase2_done]
        if not done_jobs:
            messagebox.showinfo("Nothing to export", "Run Phase 2 on at least one file first.")
            return
        dest = filedialog.asksaveasfilename(
            title="Save veraPDF report", initialfile="coverage_and_verapdf.xlsx",
            defaultextension=".xlsx", filetypes=[("Excel files", "*.xlsx")],
        )
        if not dest:
            return

        wb = openpyxl.Workbook()
        summary = wb.active
        summary.title = "Summary"
        header_fill = PatternFill("solid", fgColor="D9D9D9")
        bold = Font(bold=True)

        cols = ["filename", "status", "verapdf_pass", "verapdf_failed_checks", "error"]
        for ci, c in enumerate(cols, 1):
            cell = summary.cell(row=1, column=ci, value=c)
            cell.font = bold
            cell.fill = header_fill

        status_fill = {
            "done": PatternFill("solid", fgColor="C6EFCE"),
            "needs_review": PatternFill("solid", fgColor="FFEB9C"),
            "failed": PatternFill("solid", fgColor="FFC7CE"),
        }

        for ri, job in enumerate(done_jobs, 2):
            summary.cell(row=ri, column=1, value=job.name)
            status_cell = summary.cell(row=ri, column=2, value=job.status)
            fill = status_fill.get(job.status)
            if fill:
                status_cell.fill = fill
            summary.cell(row=ri, column=3, value=job.verapdf_pass)
            summary.cell(row=ri, column=4, value=job.verapdf_errors)
            summary.cell(row=ri, column=5, value=job.error)

        for ci, width in enumerate([40, 14, 12, 18, 50], 1):
            summary.column_dimensions[chr(64 + ci)].width = width

        # One detail sheet per file listing its failed veraPDF rules.
        for job in done_jobs:
            safe_name = job.stem[:28] or "doc"
            ws = wb.create_sheet(title=safe_name)
            dcols = ["clause", "test", "failed_checks", "description"]
            for ci, c in enumerate(dcols, 1):
                cell = ws.cell(row=1, column=ci, value=c)
                cell.font = bold
                cell.fill = header_fill
            for ri, rule in enumerate(job.verapdf_rules, 2):
                ws.cell(row=ri, column=1, value=rule["clause"])
                ws.cell(row=ri, column=2, value=rule["test"])
                ws.cell(row=ri, column=3, value=rule["failed"])
                ws.cell(row=ri, column=4, value=rule["desc"])
            for ci, width in enumerate([12, 8, 14, 90], 1):
                ws.column_dimensions[chr(64 + ci)].width = width
            if not job.verapdf_rules:
                ws.cell(row=2, column=1, value="No failed rules — fully PDF/UA compliant.")

        try:
            wb.save(dest)
        except OSError as e:
            messagebox.showerror("Save failed", f"Could not write to:\n{dest}\n\n{type(e).__name__}: {e}")
            return
        messagebox.showinfo("Saved", f"veraPDF report saved to {dest}")


if __name__ == "__main__":
    app = PDFATaggerApp()
    app.mainloop()
