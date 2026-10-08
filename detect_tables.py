"""
Detect tables in a PDF using Docling's TableFormer model.

Outputs a table-regions JSON: a list of table records, each with:
  table_id   : int  — sequential, 0-based across the document
  page_idx   : int  — 0-based page number (matches PyMuPDF convention)
  bbox       : [x0, y0, x1, y1]  — TOPLEFT coords (matches PyMuPDF output)
  num_rows   : int
  num_cols   : int
  cells      : list of cell dicts:
      row      : int   — 0-based row index
      col      : int   — 0-based col index
      row_span : int
      col_span : int
      is_header: bool  — True for column_header or row_header cells
      bbox     : [x0, y0, x1, y1] in TOPLEFT coords (ready to match PyMuPDF blocks)
      text     : str   — cell text (for debugging / AI tagging reference)

Coordinate notes:
  - Docling cell bboxes use CoordOrigin.TOPLEFT — used as-is.
  - Docling table-level bboxes use CoordOrigin.BOTTOMLEFT — converted here
    using the page height reported by Docling:
      TOPLEFT_y0 = page_h - BOTTOMLEFT_t   (visual top)
      TOPLEFT_y1 = page_h - BOTTOMLEFT_b   (visual bottom)

Filter: 1-column tables are skipped — they are almost always TOC entries or
simple lists that Docling misidentifies as tables.
"""

import json
import argparse
import os
import tempfile

from docling.document_converter import DocumentConverter

MIN_COLS = 2  # 1-column "tables" are TOC/list false positives


def detect_tables_odl(pdf_path: str, output_path: str | None = None) -> list[dict]:
    import opendataloader_pdf
    import fitz

    # We need page heights to map ODL [left, bottom, right, top] to [x0, y0, x1, y1]
    doc = fitz.open(pdf_path)
    page_heights = [page.rect.height for page in doc]
    doc.close()

    with tempfile.TemporaryDirectory() as tmpdir:
        # Run OpenDataLoader
        opendataloader_pdf.convert(input_path=[pdf_path], output_dir=tmpdir, format="json")
        json_file = os.path.join(tmpdir, os.path.splitext(os.path.basename(pdf_path))[0] + ".json")
        
        if not os.path.exists(json_file):
            return []
            
        with open(json_file, 'r', encoding='utf-8') as f:
            odl_data = json.load(f)

    def extract_tables(elements):
        tables = []
        for elem in elements:
            if elem.get("type") == "table" or elem.get("type") == "Table":
                tables.append(elem)
            if "kids" in elem and elem["kids"]:
                tables.extend(extract_tables(elem["kids"]))
            if "list items" in elem and elem["list items"]:
                tables.extend(extract_tables(elem["list items"]))
        return tables

    raw_tables = extract_tables(odl_data.get("kids", []))
    
    tables = []
    table_id = 0
    
    for raw_table in raw_tables:
        rows = raw_table.get("rows", [])
        if not rows:
            continue
            
        cells_out = []
        min_page = 999999
        num_cols = 0
        table_bbox = [99999, 99999, -99999, -99999]
        
        for r_idx, row in enumerate(rows):
            r_cells = row.get("cells", [])
            for cell in r_cells:
                page_idx = cell.get("page number", 1) - 1
                min_page = min(min_page, page_idx)
                
                # ODL returns 1-based indices
                row_idx = cell.get("row number", r_idx + 1) - 1
                col_idx = cell.get("column number", 1) - 1
                row_span = cell.get("row span", 1)
                col_span = cell.get("column span", 1)
                is_header = cell.get("pdfua_tag", "TD") == "TH"
                
                num_cols = max(num_cols, col_idx + col_span)
                
                cb = cell.get("bounding box")
                cell_bbox = None
                if cb:
                    ph = page_heights[page_idx] if page_idx < len(page_heights) else 792.0
                    # ODL: [left, bottom, right, top] -> PyMuPDF: [left, ph - top, right, ph - bottom]
                    x0 = cb[0]
                    y0 = ph - cb[3]
                    x1 = cb[2]
                    y1 = ph - cb[1]
                    cell_bbox = [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)]
                    
                    table_bbox[0] = min(table_bbox[0], x0)
                    table_bbox[1] = min(table_bbox[1], y0)
                    table_bbox[2] = max(table_bbox[2], x1)
                    table_bbox[3] = max(table_bbox[3], y1)
                
                # Try to extract text from kids (mostly paragraphs)
                texts = []
                def extract_text(kids):
                    for k in kids:
                        if "content" in k:
                            texts.append(k["content"])
                        if "kids" in k:
                            extract_text(k["kids"])
                extract_text(cell.get("kids", []))
                
                cells_out.append({
                    "row": row_idx,
                    "col": col_idx,
                    "row_span": row_span,
                    "col_span": col_span,
                    "is_header": is_header,
                    "bbox": cell_bbox,
                    "text": " ".join(texts)
                })
                
        if num_cols < MIN_COLS:
            continue
            
        tables.append({
            "table_id": table_id,
            "page_idx": min_page,
            "bbox": [round(c, 2) for c in table_bbox] if table_bbox[0] != 99999 else None,
            "num_rows": len(rows),
            "num_cols": num_cols,
            "cells": cells_out
        })
        table_id += 1

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(tables, f, indent=2, ensure_ascii=False)
        print(f"[detect_tables] {len(tables)} table(s) written -> {output_path}")

    return tables


def detect_tables(pdf_path: str, output_path: str | None = None, engine: str = "docling") -> list[dict]:
    """
    Run table detection on pdf_path and return table regions.
    Writes JSON to output_path when given.
    """
    if engine.lower() == "opendataloader":
        return detect_tables_odl(pdf_path, output_path)
        
    conv = DocumentConverter()
    result = conv.convert(pdf_path)
    doc = result.document

    tables = []
    table_id = 0

    for raw_table in doc.tables:
        grid = raw_table.data
        if grid.num_cols < MIN_COLS:
            continue

        prov = raw_table.prov
        if not prov:
            continue
        p = prov[0]
        page_no = p.page_no  # Docling is 1-based

        # Page height for BOTTOMLEFT -> TOPLEFT conversion
        page_item = doc.pages.get(page_no)
        page_h = page_item.size.height if page_item else 792.0

        # Table bbox: BOTTOMLEFT t/b -> TOPLEFT y0/y1
        tb = p.bbox
        table_bbox = [
            round(tb.l, 2),
            round(page_h - tb.t, 2),   # visual top -> TOPLEFT y0
            round(tb.r, 2),
            round(page_h - tb.b, 2),   # visual bottom -> TOPLEFT y1
        ]

        cells = []
        for cell in grid.table_cells:
            cb = cell.bbox  # already TOPLEFT
            cell_bbox = (
                [round(cb.l, 2), round(cb.t, 2), round(cb.r, 2), round(cb.b, 2)]
                if cb is not None else None
            )
            cells.append({
                "row":      cell.start_row_offset_idx,
                "col":      cell.start_col_offset_idx,
                "row_span": cell.row_span,
                "col_span": cell.col_span,
                "is_header": bool(cell.column_header or cell.row_header),
                "bbox":     cell_bbox,
                "text":     cell.text,
            })

        tables.append({
            "table_id": table_id,
            "page_idx": page_no - 1,   # convert to 0-based
            "bbox":     table_bbox,
            "num_rows": grid.num_rows,
            "num_cols": grid.num_cols,
            "cells":    cells,
        })
        table_id += 1

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(tables, f, indent=2, ensure_ascii=False)
        print(f"[detect_tables] {len(tables)} table(s) written -> {output_path}")

    return tables


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Detect tables in a PDF using Docling and output table regions JSON."
    )
    parser.add_argument("pdf_path", help="Input PDF file")
    parser.add_argument("--output", default="table_regions.json", help="Output JSON path")
    parser.add_argument("--engine", default="docling", choices=["docling", "opendataloader"], help="Table detection engine to use")
    args = parser.parse_args()
    detect_tables(args.pdf_path, args.output, args.engine)
