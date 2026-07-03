"""
CustomTkinter desktop GUI for the PDF/UA auto-tagging pipeline.

Two-phase flow because AI tagging is a MANUAL step (you paste tags from an
external AI model — the pipeline cannot run end-to-end automatically):

  PHASE 1 (Extract & Prepare): pick a PDF -> extract -> detect tables -> merge
                                -> write blocks_for_ai.json, show its contents.
  MANUAL STEP:                 paste (or load) the tags_merged.json you got
                                back from the AI.
  PHASE 2 (Inject & Finish):   validate the pasted JSON -> inject -> bookmarks
                                -> veraPDF (if available) -> tagged PDF.

Thin front-end only — every pipeline stage reuses the existing functions
unchanged (src.normalizer.normalize_blocks, detect_tables.detect_tables,
merge_tables.merge_tables, inject_tags.inject_tags, add_bookmarks.add_bookmarks,
validate_pdf.find_verapdf, diagnose_coverage.run_verapdf). No pipeline logic is
duplicated here.

Launch:
    python gui.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import traceback
import webbrowser

import customtkinter as ctk
from tkinter import filedialog, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fitz  # PyMuPDF

from src.normalizer import normalize_blocks
from detect_tables import detect_tables
from merge_tables import merge_tables
from inject_tags import inject_tags, normalize_tags
from add_bookmarks import add_bookmarks
from validate_pdf import find_verapdf
from diagnose_coverage import run_verapdf

ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")


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


class PDFATaggerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("PDF/UA Auto-Tagger")
        self.geometry("880x820")

        self.work_dir = tempfile.mkdtemp(prefix="pdfa_gui_")

        # Phase-1 state, needed by Phase 2.
        self.pdf_path = None
        self.doc_dir = None
        self.merged_json_path = None
        self.blocks_for_ai_path = None

        # Phase-2 state.
        self.tagged_pdf_path = None

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

        # File picker
        file_frame = ctk.CTkFrame(self)
        file_frame.pack(fill="x", **pad)
        ctk.CTkButton(file_frame, text="Choose PDF...", command=self._choose_pdf).pack(side="left", padx=8, pady=8)
        self.file_label = ctk.CTkLabel(file_frame, text="No PDF selected")
        self.file_label.pack(side="left", padx=8)

        # Phase 1
        p1_frame = ctk.CTkFrame(self)
        p1_frame.pack(fill="x", **pad)
        ctk.CTkLabel(p1_frame, text="Phase 1 — Prepare", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=8, pady=(8, 0))
        self.extract_btn = ctk.CTkButton(p1_frame, text="Extract & Prepare", command=self._start_extract)
        self.extract_btn.pack(anchor="w", padx=8, pady=6)
        self.blocks_path_label = ctk.CTkLabel(p1_frame, text="", anchor="w", justify="left")
        self.blocks_path_label.pack(fill="x", padx=8)
        self.blocks_ai_box = ctk.CTkTextbox(p1_frame, height=140)
        self.blocks_ai_box.pack(fill="x", padx=8, pady=(4, 8))
        self.blocks_ai_box.configure(state="disabled")

        # Manual step
        manual_frame = ctk.CTkFrame(self)
        manual_frame.pack(fill="both", expand=True, **pad)
        ctk.CTkLabel(manual_frame, text="Manual step — paste AI tags (tags_merged.json)",
                    font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=8, pady=(8, 0))
        ctk.CTkButton(manual_frame, text="Load tags from file...", command=self._load_tags_file).pack(anchor="w", padx=8, pady=6)
        self.tags_box = ctk.CTkTextbox(manual_frame, height=180)
        self.tags_box.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        # Phase 2
        p2_frame = ctk.CTkFrame(self)
        p2_frame.pack(fill="x", **pad)
        ctk.CTkLabel(p2_frame, text="Phase 2 — Inject", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=8, pady=(8, 0))
        self.inject_btn = ctk.CTkButton(p2_frame, text="Inject Tags & Finish", command=self._start_inject)
        self.inject_btn.pack(anchor="w", padx=8, pady=6)

        # Status / results
        status_frame = ctk.CTkFrame(self)
        status_frame.pack(fill="x", **pad)
        ctk.CTkLabel(status_frame, text="Status", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=8, pady=(8, 0))
        self.status_label = ctk.CTkLabel(status_frame, text="Idle.", anchor="w", justify="left", wraplength=820)
        self.status_label.pack(fill="x", padx=8, pady=4)
        self.progress = ctk.CTkProgressBar(status_frame, mode="indeterminate")
        self.progress.pack(fill="x", padx=8, pady=(0, 6))
        self.progress.set(0)

        result_row = ctk.CTkFrame(status_frame)
        result_row.pack(fill="x", padx=8, pady=(0, 8))
        self.save_btn = ctk.CTkButton(result_row, text="Save tagged PDF...", command=self._save_tagged_pdf, state="disabled")
        self.save_btn.pack(side="left")
        self.open_folder_btn = ctk.CTkButton(result_row, text="Open output folder", command=self._open_output_folder)
        self.open_folder_btn.pack(side="left", padx=10)

    def _set_status(self, text, kind="info"):
        color = {"info": None, "error": "#e03131", "warn": "#e8a33d", "success": "#2f9e44"}.get(kind)
        self.status_label.configure(text=text, text_color=color)

    # ── File picker ──────────────────────────────────────────────────────
    def _choose_pdf(self):
        path = filedialog.askopenfilename(title="Choose a PDF", filetypes=[("PDF files", "*.pdf")])
        if not path:
            return
        self.pdf_path = path
        self.file_label.configure(text=os.path.basename(path))
        # Reset downstream state — a new PDF invalidates any prior Phase 1/2 work.
        self.doc_dir = None
        self.merged_json_path = None
        self.blocks_for_ai_path = None
        self.tagged_pdf_path = None
        self.blocks_path_label.configure(text="")
        self._set_textbox(self.blocks_ai_box, "")
        self.save_btn.configure(state="disabled")
        self._set_status("PDF selected. Click 'Extract & Prepare' to continue.")

    # ── Phase 1: Extract & Prepare ───────────────────────────────────────
    def _start_extract(self):
        if not self.pdf_path:
            messagebox.showinfo("No PDF", "Choose a PDF first.")
            return
        self.extract_btn.configure(state="disabled")
        self.progress.start()
        self._set_status(f"Extracting {os.path.basename(self.pdf_path)} ...")
        threading.Thread(target=self._extract_worker, daemon=True).start()

    def _extract_worker(self):
        try:
            stem = os.path.splitext(os.path.basename(self.pdf_path))[0]
            doc_dir = os.path.join(self.work_dir, stem)
            os.makedirs(doc_dir, exist_ok=True)

            # 1. Extract (PyMuPDF raw blocks -> canonical normalized JSON)
            doc = fitz.open(self.pdf_path)
            raw_blocks = []
            for page_idx, page in enumerate(doc):
                page_dict = page.get_text("dict")
                for b in page_dict["blocks"]:
                    b["page_idx"] = page_idx
                    raw_blocks.append(b)
            doc.close()
            result = normalize_blocks(raw_blocks, self.pdf_path)
            blocks_json = os.path.join(doc_dir, f"{stem}_structured_blocks.json")
            with open(blocks_json, "w", encoding="utf-8") as f:
                json.dump(result, f)

            # 2. Detect tables
            regions_json = os.path.join(doc_dir, "regions.json")
            try:
                detect_tables(self.pdf_path, regions_json)
            except Exception:
                with open(regions_json, "w", encoding="utf-8") as f:
                    json.dump([], f)

            # 3. Merge
            merged_json = os.path.join(doc_dir, f"{stem}_structured_blocks_merged.json")
            merge_tables(blocks_json, regions_json, merged_json)
            merged = json.load(open(merged_json, encoding="utf-8"))["blocks"]

            # 4. Build blocks_for_ai.json
            slim = build_blocks_for_ai(merged)
            blocks_for_ai_path = os.path.join(doc_dir, "blocks_for_ai.json")
            with open(blocks_for_ai_path, "w", encoding="utf-8") as f:
                json.dump(slim, f, indent=2)

            self.doc_dir = doc_dir
            self.merged_json_path = merged_json
            self.blocks_for_ai_path = blocks_for_ai_path

            preview = json.dumps(slim, indent=2)
            self.after(0, self._extract_done, blocks_for_ai_path, preview, len(slim))
        except Exception as e:
            tb = traceback.format_exc()
            self.after(0, self._extract_failed, f"{type(e).__name__}: {e}", tb)

    def _extract_done(self, path, preview, n_blocks):
        self.progress.stop()
        self.progress.set(0)
        self.extract_btn.configure(state="normal")
        self.blocks_path_label.configure(text=f"blocks_for_ai.json ({n_blocks} blocks) -> {path}")
        self._set_textbox(self.blocks_ai_box, preview)
        self._set_status("Prepared — paste your AI tags below.", "success")

    def _extract_failed(self, msg, tb):
        self.progress.stop()
        self.progress.set(0)
        self.extract_btn.configure(state="normal")
        self._set_status(f"Extract & Prepare failed: {msg}", "error")
        print(tb, file=sys.stderr)

    def _set_textbox(self, box, text):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

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
        if not self.merged_json_path:
            self._set_status("Run 'Extract & Prepare' first.", "error")
            return

        raw = self.tags_box.get("1.0", "end").strip()
        if not raw:
            self._set_status("Paste (or load) the AI tags JSON before injecting.", "error")
            return

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

        tags_json_path = os.path.join(self.doc_dir, "tags.json")
        with open(tags_json_path, "w", encoding="utf-8") as f:
            json.dump(tags, f)

        self.inject_btn.configure(state="disabled")
        self.save_btn.configure(state="disabled")
        self.progress.start()
        self._set_status("Injecting tags ...")
        threading.Thread(target=self._inject_worker, args=(tags_json_path,), daemon=True).start()

    def _inject_worker(self, tags_json_path):
        try:
            stem = os.path.splitext(os.path.basename(self.pdf_path))[0]
            tagged_pdf = os.path.join(self.doc_dir, f"{stem}_tagged.pdf")

            # 5. Inject
            inject_tags(self.pdf_path, self.merged_json_path, tags_json_path, tagged_pdf)

            # 6. Bookmarks
            add_bookmarks(tagged_pdf, self.merged_json_path, tags_json_path, tagged_pdf)

            # 7. veraPDF (optional)
            verapdf_pass = "skipped"
            verapdf_errors = ""
            if self.verapdf_path:
                report = os.path.join(self.doc_dir, "verapdf.xml")
                compliant, failed = run_verapdf(self.verapdf_path, tagged_pdf, report)
                verapdf_pass = "PASS" if compliant else "FAIL"
                verapdf_errors = failed

            status = "done"
            if verapdf_pass == "FAIL":
                status = "needs_review"

            self.tagged_pdf_path = tagged_pdf
            self.after(0, self._inject_done, tagged_pdf, status, verapdf_pass, verapdf_errors)
        except Exception as e:
            tb = traceback.format_exc()
            self.after(0, self._inject_failed, f"{type(e).__name__}: {e}", tb)

    def _inject_done(self, tagged_pdf, status, verapdf_pass, verapdf_errors):
        self.progress.stop()
        self.progress.set(0)
        self.inject_btn.configure(state="normal")
        self.save_btn.configure(state="normal")

        vera_note = f"veraPDF: {verapdf_pass}"
        if verapdf_pass == "FAIL":
            vera_note += f" ({verapdf_errors} failed checks)"
        elif verapdf_pass == "skipped":
            vera_note += " (not installed — validation skipped)"

        kind = "success" if status == "done" else "warn"
        self._set_status(
            f"Status: {status}.  Output: {tagged_pdf}\n{vera_note}", kind,
        )

    def _inject_failed(self, msg, tb):
        self.progress.stop()
        self.progress.set(0)
        self.inject_btn.configure(state="normal")
        self._set_status(f"Inject Tags & Finish failed: {msg}", "error")
        print(tb, file=sys.stderr)

    # ── Save / open folder ───────────────────────────────────────────────
    def _save_tagged_pdf(self):
        if not self.tagged_pdf_path or not os.path.isfile(self.tagged_pdf_path):
            messagebox.showwarning("Not available", "No tagged PDF has been produced yet.")
            return
        dest = filedialog.asksaveasfilename(
            title="Save tagged PDF", initialfile=os.path.basename(self.tagged_pdf_path),
            defaultextension=".pdf", filetypes=[("PDF files", "*.pdf")],
        )
        if dest:
            shutil.copyfile(self.tagged_pdf_path, dest)
            messagebox.showinfo("Saved", f"Saved to {dest}")

    def _open_output_folder(self):
        path = self.doc_dir or self.work_dir
        path = os.path.abspath(path)
        if os.name == "nt":
            os.startfile(path)
        else:
            webbrowser.open(f"file://{path}")


if __name__ == "__main__":
    app = PDFATaggerApp()
    app.mainloop()
