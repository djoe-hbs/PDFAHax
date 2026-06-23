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

from docling.document_converter import DocumentConverter

MIN_COLS = 2  # 1-column "tables" are TOC/list false positives


def detect_tables(pdf_path: str, output_path: str | None = None) -> list[dict]:
    """
    Run Docling on pdf_path and return table regions.
    Writes JSON to output_path when given.
    """
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

        # Page height for BOTTOMLEFT → TOPLEFT conversion
        page_item = doc.pages.get(page_no)
        page_h = page_item.size.height if page_item else 792.0

        # Table bbox: BOTTOMLEFT t/b → TOPLEFT y0/y1
        tb = p.bbox
        table_bbox = [
            round(tb.l, 2),
            round(page_h - tb.t, 2),   # visual top → TOPLEFT y0
            round(tb.r, 2),
            round(page_h - tb.b, 2),   # visual bottom → TOPLEFT y1
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
    args = parser.parse_args()
    detect_tables(args.pdf_path, args.output)
