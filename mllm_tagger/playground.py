"""
MLLM Vision Tagger — Playground

Renders PDF pages as images and sends them to a Multimodal LLM (Gemini)
for visual PDF/UA tagging. The MLLM sees the actual page layout and returns
bounding boxes + semantic tags for every element it detects.

Usage:
    python playground.py document.pdf                    # Tag page 0 only
    python playground.py document.pdf --page 3           # Tag page 3 only
    python playground.py document.pdf --all              # Tag all pages
    python playground.py document.pdf --pages 2-5        # Tag pages 2 through 5
    python playground.py document.pdf --page 0 --dpi 300 # Higher resolution render
"""

import argparse
import json
import os
import sys
import time
import base64
from pathlib import Path

import fitz  # PyMuPDF
from dotenv import load_dotenv
from google import genai
from google.genai import types

# ── Configuration ────────────────────────────────────────────────────────────

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL_NAME = "gemini-3.1-flash-lite"

SCRIPT_DIR = Path(__file__).parent
PROMPT_PATH = SCRIPT_DIR / "prompt.md"
TRACKING_TEMPLATE_PATH = SCRIPT_DIR / "tracking_template.json"
OUTPUT_DIR = SCRIPT_DIR / "playground"

# Retry settings
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2

DEFAULT_DPI = 200  # Resolution for page rendering


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_prompt() -> str:
    """Load the MLLM system prompt."""
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def load_tracking_template() -> dict:
    """Load a fresh copy of the initial tracking state."""
    with open(TRACKING_TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def render_page_to_image(pdf_path: str, page_idx: int, dpi: int = DEFAULT_DPI) -> tuple[bytes, dict]:
    """Render a single PDF page to a PNG image.

    Returns:
        (png_bytes, page_info) where page_info contains dimensions for
        coordinate conversion.
    """
    doc = fitz.open(pdf_path)
    if page_idx >= len(doc):
        doc.close()
        raise ValueError(f"Page {page_idx} does not exist (document has {len(doc)} pages)")

    page = doc[page_idx]
    # Scale factor from DPI (72 DPI is PDF's native resolution)
    scale = dpi / 72.0
    mat = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    png_bytes = pix.tobytes("png")

    page_info = {
        "page_idx": page_idx,
        "total_pages": len(doc),
        "pdf_width": page.rect.width,     # in PDF points (72 dpi)
        "pdf_height": page.rect.height,
        "image_width": pix.width,          # in pixels at rendered DPI
        "image_height": pix.height,
        "dpi": dpi,
    }

    doc.close()
    return png_bytes, page_info


def normalized_to_pdf_coords(bbox_norm: list[float], page_info: dict) -> list[float]:
    """Convert normalized [0,1] bounding box to PDF page coordinates (top-left origin).

    Args:
        bbox_norm: [x0, y0, x1, y1] in [0,1] range
        page_info: dict with pdf_width and pdf_height

    Returns:
        [x0, y0, x1, y1] in PDF points (top-left origin)
    """
    x0 = bbox_norm[0] * page_info["pdf_width"]
    y0 = bbox_norm[1] * page_info["pdf_height"]
    x1 = bbox_norm[2] * page_info["pdf_width"]
    y1 = bbox_norm[3] * page_info["pdf_height"]
    return [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)]


def build_user_message(page_idx: int, total_pages: int, tracking: dict) -> str:
    """Build the text portion of the user message."""
    parts = [
        f"## Page {page_idx + 1} of {total_pages} (0-indexed: {page_idx})\n",
        f"Analyze the attached page image and tag every visible element.\n",
        f"### tracking.json (current state)\n```json\n{json.dumps(tracking, indent=2)}\n```\n",
        "Return ONLY valid JSON matching the output schema. No markdown fences, no commentary — pure JSON.",
    ]
    return "\n".join(parts)


def call_gemini(
    client: genai.Client,
    system_prompt: str,
    user_message: str,
    image_bytes: bytes,
) -> dict:
    """Call Gemini with the page image and prompt. Returns parsed JSON."""
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=[
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_bytes(
                                data=image_bytes,
                                mime_type="image/png",
                            ),
                            types.Part.from_text(text=user_message),
                        ],
                    )
                ],
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )

            text = response.text
            if not text:
                raise ValueError("Empty response from Gemini API")

            # Strip markdown fences if present despite JSON mode
            text = text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines)

            return json.loads(text)

        except json.JSONDecodeError as e:
            last_error = ValueError(
                f"Invalid JSON from Gemini (attempt {attempt + 1}/{MAX_RETRIES}): {e}\n"
                f"Raw: {text[:500]}"
            )
        except Exception as e:
            last_error = e

        if attempt < MAX_RETRIES - 1:
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            print(f"  Retrying in {delay}s ...", flush=True)
            time.sleep(delay)

    raise last_error


def convert_result_coords(result: dict, page_info: dict) -> dict:
    """Convert all normalized bboxes in the MLLM result to PDF coordinates."""
    if "elements" in result:
        for elem in result["elements"]:
            if "bbox" in elem and elem["bbox"]:
                elem["bbox_normalized"] = list(elem["bbox"])
                elem["bbox_pdf"] = normalized_to_pdf_coords(elem["bbox"], page_info)
    return result


def save_page_image(png_bytes: bytes, page_idx: int, output_dir: Path) -> Path:
    """Save the rendered page image for reference."""
    img_path = output_dir / f"page_{page_idx}.png"
    with open(img_path, "wb") as f:
        f.write(png_bytes)
    return img_path


# ── Main ─────────────────────────────────────────────────────────────────────

def tag_page(
    client: genai.Client,
    system_prompt: str,
    pdf_path: str,
    page_idx: int,
    tracking: dict,
    dpi: int = DEFAULT_DPI,
    save_images: bool = True,
) -> tuple[dict, dict]:
    """Tag a single page. Returns (result, updated_tracking)."""

    # 1. Render page
    png_bytes, page_info = render_page_to_image(pdf_path, page_idx, dpi)

    # Update tracking with document info
    tracking.setdefault("document_info", {})
    tracking["document_info"]["total_pages"] = page_info["total_pages"]
    tracking["document_info"]["current_page"] = page_idx

    # 2. Save image for reference
    if save_images:
        OUTPUT_DIR.mkdir(exist_ok=True)
        img_path = save_page_image(png_bytes, page_idx, OUTPUT_DIR)
        print(f"  Saved page image: {img_path}")

    # 3. Build message and call MLLM
    user_msg = build_user_message(page_idx, page_info["total_pages"], tracking)
    result = call_gemini(client, system_prompt, user_msg, png_bytes)

    # 4. Convert coordinates
    result = convert_result_coords(result, page_info)

    # 5. Store page_info for reference
    result["_page_info"] = page_info

    # 6. Extract updated tracking
    updated_tracking = result.get("updated_tracking", tracking)

    return result, updated_tracking


def main():
    parser = argparse.ArgumentParser(description="MLLM Vision Tagger Playground")
    parser.add_argument("pdf", help="Path to the PDF file")
    parser.add_argument("--page", type=int, default=None, help="Tag a single page (0-indexed)")
    parser.add_argument("--pages", default=None, help="Tag a range of pages (e.g., '2-5')")
    parser.add_argument("--all", action="store_true", help="Tag all pages")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI, help=f"Render DPI (default: {DEFAULT_DPI})")
    parser.add_argument("--api-key", default=None, help="Gemini API key (overrides .env)")
    parser.add_argument("--no-images", action="store_true", help="Don't save rendered page images")
    args = parser.parse_args()

    if not os.path.isfile(args.pdf):
        print(f"Error: PDF not found: {args.pdf}")
        return 1

    key = args.api_key or GEMINI_API_KEY
    if not key:
        print("Error: No API key. Set GEMINI_API_KEY in ../.env or pass --api-key")
        return 1

    # Determine which pages to process
    doc = fitz.open(args.pdf)
    total = len(doc)
    doc.close()

    if args.all:
        page_indices = list(range(total))
    elif args.pages:
        start, end = map(int, args.pages.split("-"))
        page_indices = list(range(start, min(end + 1, total)))
    elif args.page is not None:
        page_indices = [args.page]
    else:
        page_indices = [0]

    print(f"PDF: {args.pdf} ({total} pages)")
    print(f"Processing pages: {page_indices}")
    print(f"DPI: {args.dpi}")
    print(f"Model: {MODEL_NAME}")
    print()

    # Initialize
    client = genai.Client(api_key=key)
    system_prompt = load_prompt()
    tracking = load_tracking_template()
    tracking["document_info"]["total_pages"] = total

    OUTPUT_DIR.mkdir(exist_ok=True)
    all_results = []

    for i, page_idx in enumerate(page_indices):
        print(f"[{i + 1}/{len(page_indices)}] Tagging page {page_idx} ...", flush=True)

        try:
            result, tracking = tag_page(
                client, system_prompt, args.pdf, page_idx,
                tracking, dpi=args.dpi, save_images=not args.no_images,
            )

            n_elements = len(result.get("elements", []))
            print(f"  -> {n_elements} elements detected")
            all_results.append(result)

            # Save per-page result
            page_out = OUTPUT_DIR / f"page_{page_idx}_tags.json"
            with open(page_out, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            print(f"  Saved: {page_out}")

        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            all_results.append({"page_idx": page_idx, "error": str(e), "elements": []})

    # Save combined output
    combined = {
        "pdf": os.path.basename(args.pdf),
        "model": MODEL_NAME,
        "dpi": args.dpi,
        "pages_processed": page_indices,
        "results": all_results,
        "final_tracking": tracking,
    }
    combined_path = OUTPUT_DIR / "combined_tags.json"
    with open(combined_path, "w", encoding="utf-8") as f:
        json.dump(combined, f, indent=2)

    # Save final tracking state
    tracking_path = OUTPUT_DIR / "tracking_final.json"
    tracking = {}
    if tracking_path.exists():
        with open(tracking_path, "r", encoding="utf-8") as f:
            tracking = json.load(f)
            
    # Mark every MLLM-processed document as needing review due to hallucination risk
    tracking["needs_review"] = True

    with open(tracking_path, "w", encoding="utf-8") as f:
        json.dump(tracking, f, indent=2)

    # Summary
    total_elements = sum(len(r.get("elements", [])) for r in all_results)
    errors = sum(1 for r in all_results if "error" in r)
    print()
    print("=" * 60)
    print(f"Done! {len(page_indices)} pages processed, {total_elements} total elements detected.")
    if errors:
        print(f"  [!] {errors} page(s) had errors.")
    print(f"  Combined output: {combined_path}")
    print(f"  Final tracking:  {tracking_path}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
