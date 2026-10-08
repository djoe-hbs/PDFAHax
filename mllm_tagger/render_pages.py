"""
Render PDF pages to images — saves each page as a PNG in the playground/ folder.

Usage:
    python render_pages.py                             # Render Test_Input.pdf (default)
    python render_pages.py path/to/document.pdf        # Render a specific PDF
    python render_pages.py document.pdf --dpi 300      # Higher resolution
    python render_pages.py document.pdf --pages 0-4    # Only pages 0 through 4
"""

import argparse
import os
import sys
from pathlib import Path

import fitz  # PyMuPDF

SCRIPT_DIR = Path(__file__).parent
DEFAULT_PDF = SCRIPT_DIR / "Test_Input.pdf"
PLAYGROUND_DIR = SCRIPT_DIR / "playground"

DEFAULT_DPI = 200


def render_pages(
    pdf_path: str,
    output_dir: Path,
    dpi: int = DEFAULT_DPI,
    page_range: tuple[int, int] | None = None,
) -> list[dict]:
    """Render PDF pages to PNG images.

    Args:
        pdf_path: Path to the PDF file.
        output_dir: Directory to save images.
        dpi: Render resolution (default 200).
        page_range: Optional (start, end) inclusive range of pages.

    Returns:
        List of page info dicts with paths and dimensions.
    """
    doc = fitz.open(pdf_path)
    total_pages = len(doc)

    if page_range:
        start, end = page_range
        start = max(0, start)
        end = min(end, total_pages - 1)
        indices = list(range(start, end + 1))
    else:
        indices = list(range(total_pages))

    output_dir.mkdir(parents=True, exist_ok=True)
    scale = dpi / 72.0
    mat = fitz.Matrix(scale, scale)

    pages_info = []

    for page_idx in indices:
        page = doc[page_idx]
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_path = output_dir / f"page_{page_idx}.png"
        pix.save(str(img_path))

        info = {
            "page_idx": page_idx,
            "image_path": str(img_path),
            "pdf_width": round(page.rect.width, 2),
            "pdf_height": round(page.rect.height, 2),
            "image_width": pix.width,
            "image_height": pix.height,
            "dpi": dpi,
        }
        pages_info.append(info)
        print(f"  [{page_idx + 1}/{total_pages}] {img_path.name}  "
              f"({pix.width}x{pix.height}px, PDF {info['pdf_width']}x{info['pdf_height']}pt)")

    doc.close()

    # Save page metadata for use by other scripts
    import json
    meta_path = output_dir / "pages_meta.json"
    meta = {
        "pdf": os.path.basename(pdf_path),
        "pdf_path": os.path.abspath(pdf_path),
        "total_pages": total_pages,
        "rendered_pages": indices,
        "dpi": dpi,
        "pages": pages_info,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"\n  Metadata saved: {meta_path}")

    return pages_info


def main():
    parser = argparse.ArgumentParser(description="Render PDF pages to PNG images")
    parser.add_argument("pdf", nargs="?", default=str(DEFAULT_PDF),
                        help=f"PDF file (default: {DEFAULT_PDF.name})")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                        help=f"Render resolution (default: {DEFAULT_DPI})")
    parser.add_argument("--pages", default=None,
                        help="Page range to render, e.g. '0-4' (default: all)")
    parser.add_argument("--output-dir", default=str(PLAYGROUND_DIR),
                        help=f"Output directory (default: {PLAYGROUND_DIR})")
    args = parser.parse_args()

    if not os.path.isfile(args.pdf):
        print(f"Error: PDF not found: {args.pdf}")
        return 1

    page_range = None
    if args.pages:
        parts = args.pages.split("-")
        page_range = (int(parts[0]), int(parts[-1]))

    print(f"Rendering: {args.pdf}")
    print(f"DPI: {args.dpi}")
    print(f"Output: {args.output_dir}\n")

    pages = render_pages(args.pdf, Path(args.output_dir), args.dpi, page_range)

    print(f"\n  Done! {len(pages)} page(s) rendered to {args.output_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
