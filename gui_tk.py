"""
Minimal Tkinter desktop GUI for the PDF/UA auto-tagging pipeline.

Thin front-end only — all pipeline logic is reused unchanged from
batch_process.py (process_one, the same per-file function batch mode uses) and
validate_pdf.py (find_verapdf). No pipeline logic is duplicated here.

Launch:
    python gui_tk.py
"""

import os
import queue
import shutil
import tempfile
import threading
import traceback
import webbrowser
from tkinter import Tk, StringVar, filedialog, messagebox
from tkinter import ttk

from batch_process import process_one, write_csv
from validate_pdf import find_verapdf

STATUS_TAGS = {
    "done": ("done", "#d4f7d4"),
    "needs_review": ("needs review", "#fff3cd"),
    "failed": ("failed", "#f8d7da"),
}


class PDFATaggerApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("PDF/UA Auto-Tagger")
        self.root.geometry("820x560")

        self.work_dir = tempfile.mkdtemp(prefix="pdfa_gui_")
        self.input_dir = os.path.join(self.work_dir, "input")
        self.output_dir = os.path.join(self.work_dir, "output")
        os.makedirs(self.input_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)

        self.selected_files = []          # list of source paths chosen by the user
        self.results = []                 # list of row dicts from process_one
        self.tagged_pdfs = {}             # filename -> tagged pdf path
        self.csv_path = None
        self.ui_queue = queue.Queue()     # worker thread -> main thread messages
        self.processing = False

        try:
            self.verapdf_path = find_verapdf(None)
        except FileNotFoundError:
            self.verapdf_path = None

        self._build_layout()
        if self.verapdf_path is None:
            self._log(
                "[!] veraPDF not found — validation will be SKIPPED "
                "(verapdf_pass will show 'skipped'). See 'guide to run.txt'.",
                warn=True,
            )

        self.root.after(100, self._poll_queue)

    # ── UI layout ────────────────────────────────────────────────────────
    def _build_layout(self):
        pad = {"padx": 10, "pady": 6}

        # 1. Upload
        upload_frame = ttk.LabelFrame(self.root, text="1. Upload")
        upload_frame.pack(fill="x", **pad)

        btn_row = ttk.Frame(upload_frame)
        btn_row.pack(fill="x", padx=8, pady=6)
        ttk.Button(btn_row, text="Choose PDFs...", command=self._choose_files).pack(side="left")
        self.files_label = ttk.Label(btn_row, text="No files selected")
        self.files_label.pack(side="left", padx=10)

        # 2. Process
        process_frame = ttk.LabelFrame(self.root, text="2. Process")
        process_frame.pack(fill="x", **pad)

        row2 = ttk.Frame(process_frame)
        row2.pack(fill="x", padx=8, pady=6)
        self.process_btn = ttk.Button(row2, text="Process", command=self._start_processing)
        self.process_btn.pack(side="left")
        self.progress = ttk.Progressbar(row2, mode="determinate", length=400)
        self.progress.pack(side="left", padx=10, fill="x", expand=True)

        self.status_var = StringVar(value="")
        ttk.Label(process_frame, textvariable=self.status_var).pack(anchor="w", padx=8, pady=(0, 6))

        # 3. Results
        results_frame = ttk.LabelFrame(self.root, text="3. Results")
        results_frame.pack(fill="both", expand=True, **pad)

        cols = ("filename", "pages", "status", "verapdf_pass", "reason")
        self.tree = ttk.Treeview(results_frame, columns=cols, show="headings", height=8)
        widths = {"filename": 180, "pages": 50, "status": 100, "verapdf_pass": 90, "reason": 260}
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=widths.get(c, 120), anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8, pady=6)
        self.tree.bind("<<TreeviewSelect>>", self._on_select_result)

        for status, (_, color) in STATUS_TAGS.items():
            self.tree.tag_configure(status, background=color)

        self.summary_var = StringVar(value="")
        ttk.Label(results_frame, textvariable=self.summary_var).pack(anchor="w", padx=8, pady=(0, 6))

        # 4. Download
        download_frame = ttk.LabelFrame(self.root, text="4. Download")
        download_frame.pack(fill="x", **pad)

        row4 = ttk.Frame(download_frame)
        row4.pack(fill="x", padx=8, pady=6)
        self.download_selected_btn = ttk.Button(
            row4, text="Download selected tagged PDF...",
            command=self._download_selected, state="disabled",
        )
        self.download_selected_btn.pack(side="left")
        self.download_csv_btn = ttk.Button(
            row4, text="Download batch report (CSV)...",
            command=self._download_csv, state="disabled",
        )
        self.download_csv_btn.pack(side="left", padx=10)
        self.open_output_btn = ttk.Button(
            row4, text="Open output folder", command=self._open_output_folder,
        )
        self.open_output_btn.pack(side="left")

    def _log(self, msg, warn=False):
        self.status_var.set(msg)

    # ── 1. Upload ────────────────────────────────────────────────────────
    def _choose_files(self):
        paths = filedialog.askopenfilenames(
            title="Choose PDFs", filetypes=[("PDF files", "*.pdf")]
        )
        if not paths:
            return
        self.selected_files = list(paths)
        self.files_label.config(text=f"{len(self.selected_files)} file(s) selected")

    # ── 2. Process ───────────────────────────────────────────────────────
    def _start_processing(self):
        if self.processing:
            return
        if not self.selected_files:
            messagebox.showinfo("No files", "Choose one or more PDFs first.")
            return

        self.processing = True
        self.process_btn.config(state="disabled")
        self.download_selected_btn.config(state="disabled")
        self.download_csv_btn.config(state="disabled")
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.results = []
        self.tagged_pdfs = {}
        self.progress.config(value=0, maximum=len(self.selected_files))
        self.summary_var.set("")

        # Copy uploads into the temp input folder (mirrors the GUI's "upload" step).
        saved_paths = []
        for src in self.selected_files:
            dest = os.path.join(self.input_dir, os.path.basename(src))
            shutil.copyfile(src, dest)
            saved_paths.append(dest)

        thread = threading.Thread(target=self._process_worker, args=(saved_paths,), daemon=True)
        thread.start()

    def _process_worker(self, saved_paths):
        """Runs on a background thread. Never touches Tk widgets directly —
        only pushes messages onto ui_queue, which the main thread drains."""
        n = len(saved_paths)
        for i, pdf_path in enumerate(saved_paths, 1):
            name = os.path.basename(pdf_path)
            stem = os.path.splitext(name)[0]
            self.ui_queue.put(("progress", i, n, name))

            doc_dir = os.path.join(self.output_dir, stem)
            os.makedirs(doc_dir, exist_ok=True)

            # ISOLATION: identical to batch_process.py's per-file try/except —
            # one bad PDF is caught and recorded as failed; the loop continues.
            try:
                row = process_one(pdf_path, doc_dir, self.verapdf_path, "placeholder")
                tagged_pdf = os.path.join(doc_dir, f"{stem}_tagged.pdf")
                tagged_path = tagged_pdf if os.path.isfile(tagged_pdf) else None
            except Exception as e:
                row = {
                    "filename": name, "pages": "", "scanned?": "", "extraction_ok?": "",
                    "headings": "", "tables": "", "figures": "",
                    "verapdf_pass?": "", "verapdf_errors": "",
                    "status": "failed", "reason": f"{type(e).__name__}: {e}",
                }
                with open(os.path.join(doc_dir, "error.log"), "w", encoding="utf-8") as f:
                    f.write(f"{name}\n{'='*60}\n")
                    traceback.print_exc(file=f)
                tagged_path = None

            self.ui_queue.put(("result", row, tagged_path))

        self.ui_queue.put(("finished", n))

    def _poll_queue(self):
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, i, n, name = msg
                    self.progress.config(value=i - 1)
                    self.status_var.set(f"File {i} of {n}: {name}")
                elif kind == "result":
                    _, row, tagged_path = msg
                    self._add_result_row(row, tagged_path)
                    self.progress.config(value=self.progress["value"] + 1)
                elif kind == "finished":
                    _, n = msg
                    self._finish_processing(n)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _add_result_row(self, row, tagged_path):
        self.results.append(row)
        if tagged_path:
            self.tagged_pdfs[row["filename"]] = tagged_path
        status = row.get("status", "")
        label, _ = STATUS_TAGS.get(status, (status, ""))
        self.tree.insert("", "end", values=(
            row.get("filename", ""), row.get("pages", ""), label,
            row.get("verapdf_pass?", ""), row.get("reason", ""),
        ), tags=(status,))

    def _finish_processing(self, n):
        self.processing = False
        self.process_btn.config(state="normal")
        self.status_var.set(f"Done — processed {n} file(s).")

        done = sum(1 for r in self.results if r.get("status") == "done")
        review = sum(1 for r in self.results if r.get("status") == "needs_review")
        failed = sum(1 for r in self.results if r.get("status") == "failed")
        self.summary_var.set(f"done: {done}    needs_review: {review}    failed: {failed}")

        self.csv_path = os.path.join(self.output_dir, "batch_report.csv")
        write_csv(self.results, self.csv_path)
        self.download_csv_btn.config(state="normal")

    # ── 3. Results selection ─────────────────────────────────────────────
    def _on_select_result(self, _event):
        sel = self.tree.selection()
        if not sel:
            self.download_selected_btn.config(state="disabled")
            return
        values = self.tree.item(sel[0], "values")
        filename = values[0] if values else None
        if filename and filename in self.tagged_pdfs:
            self.download_selected_btn.config(state="normal")
        else:
            self.download_selected_btn.config(state="disabled")

    # ── 4. Download ──────────────────────────────────────────────────────
    def _download_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        filename = self.tree.item(sel[0], "values")[0]
        src = self.tagged_pdfs.get(filename)
        if not src or not os.path.isfile(src):
            messagebox.showwarning("Not available", "No tagged PDF was produced for this file.")
            return
        dest = filedialog.asksaveasfilename(
            title="Save tagged PDF", initialfile=os.path.basename(src),
            defaultextension=".pdf", filetypes=[("PDF files", "*.pdf")],
        )
        if dest:
            shutil.copyfile(src, dest)
            messagebox.showinfo("Saved", f"Saved to {dest}")

    def _download_csv(self):
        if not self.csv_path or not os.path.isfile(self.csv_path):
            return
        dest = filedialog.asksaveasfilename(
            title="Save batch report", initialfile="batch_report.csv",
            defaultextension=".csv", filetypes=[("CSV files", "*.csv")],
        )
        if dest:
            shutil.copyfile(self.csv_path, dest)
            messagebox.showinfo("Saved", f"Saved to {dest}")

    def _open_output_folder(self):
        path = os.path.abspath(self.output_dir)
        if os.name == "nt":
            os.startfile(path)
        else:
            webbrowser.open(f"file://{path}")


if __name__ == "__main__":
    root = Tk()
    app = PDFATaggerApp(root)
    root.mainloop()
