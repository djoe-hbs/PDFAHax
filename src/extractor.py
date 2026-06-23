"""
PDF Extractor — Core PyMuPDF extraction wrapper.

This module provides the PDFExtractor class which:
1. Takes a PDF file path as input
2. Runs PyMuPDF's block extraction
3. Produces a canonical structured JSON of document blocks
"""

from __future__ import annotations

import os
import logging
import time
import fitz

from src.normalizer import normalize_blocks
from src.utils import ensure_dir, save_json, load_json

logger = logging.getLogger(__name__)

class PDFExtractor:
    def __init__(self, output_dir: str = "./output"):
        self.output_dir = os.path.abspath(output_dir)
        ensure_dir(self.output_dir)
        logger.info(f"PDFExtractor initialized. Output dir: {self.output_dir}")

    def extract(self, pdf_path: str) -> dict:
        pdf_path = os.path.abspath(pdf_path)
        if not os.path.isfile(pdf_path):
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")

        pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]
        doc_output_dir = os.path.join(self.output_dir, pdf_name)
        ensure_dir(doc_output_dir)

        logger.info(f"Starting PyMuPDF extraction: {pdf_path}")
        start_time = time.time()

        doc = fitz.open(pdf_path)
        raw_blocks = []

        for page_idx, page in enumerate(doc):
            page_dict = page.get_text("dict")
            for b in page_dict["blocks"]:
                b["page_idx"] = page_idx
                raw_blocks.append(b)

        doc.close()

        logger.info("Extraction complete, normalizing blocks...")
        canonical_result = normalize_blocks(raw_blocks, pdf_path)
        
        # Save output
        output_file = os.path.join(doc_output_dir, f"{pdf_name}_structured_blocks.json")
        save_json(canonical_result, output_file, pretty=True)
        
        logger.info(f"Extraction took {time.time() - start_time:.2f}s")
        return canonical_result
