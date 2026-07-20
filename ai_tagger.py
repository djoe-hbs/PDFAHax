"""
Automated AI tagging using the Google Gemini API.

Replaces the manual copy-paste step (Step 4) in the PDF/UA pipeline. Sends
blocks page-by-page to Gemini with the system prompt, overlap context, and
tracking state, then merges the per-page responses into a single flat
tags_merged.json that the rest of the pipeline expects.

Usage (standalone CLI):
    python ai_tagger.py blocks_merged.json --output tags_merged.json
    python ai_tagger.py blocks_merged.json --footnote-candidates-dir footnote_candidates --output tags_merged.json

Usage (from Python):
    from ai_tagger import tag_document
    tags = tag_document(merged_blocks, footnote_candidates_dir=None, progress_cb=None)
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict

from dotenv import load_dotenv
from google import genai
from google.genai import types

# ── Configuration ────────────────────────────────────────────────────────────

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL_NAME = "gemini-3.1-flash-lite"

PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt")

# Retry settings for transient API errors
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds (exponential backoff: 2, 4, 8)

# ── Initial tracking state ───────────────────────────────────────────────────

INITIAL_TRACKING = {
    "heading_hierarchy": {
        "h1_used": False,
        "h1_text": None,
        "last_h2_text": None,
        "last_h3_text": None,
        "last_h4_text": None,
        "current_heading_level": 0,
    },
    "list_state": {
        "in_list": False,
        "list_nesting_depth": 0,
        "list_type": None,
    },
    "table_state": {
        "in_table": False,
        "current_table_has_header": False,
    },
    "counters": {
        "next_reading_order": 1,
        "total_blocks_tagged": 0,
    },
}

# ── Output format template ──────────────────────────────────────────────────

OUTPUT_FORMAT_TEMPLATE = {
    "tagged_blocks": [
        {
            "block_id": "INTEGER — the block's original block_id",
            "tag": "STRING — the PDF/UA tag (H1, H2, P, Artifact, LI, TH, TD, Figure, TOCI, Caption, Reference, Note)",
            "alt_text": "STRING or null — required for Figure tags",
            "reading_order": "INTEGER or null — null only for Artifact",
            "parent_tag": "STRING or null — L for LI, TR for TH/TD, TOC for TOCI, null otherwise",
            "role": "STRING — heading, paragraph, artifact, listitem, tablecell, figure, toc, caption, reference, footnote",
            "notes": "STRING — brief rationale for your tagging decision",
        }
    ],
    "updated_tracking": {
        "heading_hierarchy": {},
        "list_state": {},
        "table_state": {},
        "counters": {},
    },
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_prompt() -> str:
    """Load the system prompt from the project's `prompt` file."""
    if not os.path.isfile(PROMPT_PATH):
        raise FileNotFoundError(
            f"System prompt file not found at: {PROMPT_PATH}\n"
            "This file is required for the AI tagger to work."
        )
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _build_slim_block(block: dict) -> dict:
    """Project a merged block into the slim AI-facing format."""
    e = {
        "block_id": block["block_id"],
        "type": block["type"],
        "text": block["text"][:120],
        "bbox": block["bbox"],
        "metadata": block.get("metadata", {}),
    }
    if block["type"] == "table_cell":
        e["is_header"] = block["is_header"]
        e["row"] = block["row"]
        e["col"] = block["col"]
    return e


def _group_blocks_by_page(blocks: list[dict]) -> dict[int, list[dict]]:
    """Group blocks by page_idx, preserving order within each page."""
    pages = defaultdict(list)
    for b in blocks:
        pages[b["page_idx"]].append(b)
    return dict(sorted(pages.items()))


def _load_footnote_candidates(candidates_dir: str | None, page_idx: int) -> list[dict]:
    """Load footnote candidates for a specific page, if they exist."""
    if not candidates_dir or not os.path.isdir(candidates_dir):
        return []
    path = os.path.join(candidates_dir, f"page_{page_idx}_footnote_candidates.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _build_user_message(
    page_idx: int,
    page_blocks: list[dict],
    overlap_blocks: list[dict],
    tracking: dict,
    footnote_candidates: list[dict],
) -> str:
    """Build the user message for a single page's API call.

    Mirrors the file-based approach from the prompt (page_N_chunk.json,
    page_N_overlap.json, tracking.json, footnote_candidates.json,
    output_format_template.json) but inlines everything into one message.
    """
    slim_blocks = [_build_slim_block(b) for b in page_blocks]
    slim_overlap = [_build_slim_block(b) for b in overlap_blocks]

    parts = [
        f"## Page {page_idx} — tag these blocks\n",
        f"### page_{page_idx}_chunk.json\n```json\n{json.dumps(slim_blocks, indent=2)}\n```\n",
    ]

    if slim_overlap:
        parts.append(
            f"### page_{page_idx}_overlap.json (last blocks from previous page — do NOT re-tag)\n"
            f"```json\n{json.dumps(slim_overlap, indent=2)}\n```\n"
        )
    else:
        parts.append(
            f"### page_{page_idx}_overlap.json\nNo overlap — this is the first page.\n"
        )

    parts.append(
        f"### tracking.json\n```json\n{json.dumps(tracking, indent=2)}\n```\n"
    )

    parts.append(
        f"### footnote_candidates.json\n```json\n{json.dumps(footnote_candidates, indent=2)}\n```\n"
    )

    parts.append(
        f"### output_format_template.json\n```json\n{json.dumps(OUTPUT_FORMAT_TEMPLATE, indent=2)}\n```\n"
    )

    parts.append(
        "Return ONLY valid JSON matching the output_format_template. "
        "No markdown fences, no commentary — pure JSON."
    )

    return "\n".join(parts)


def _call_gemini(
    client: genai.Client,
    system_prompt: str,
    user_message: str,
) -> dict:
    """Call the Gemini API with retry logic. Returns parsed JSON dict."""
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    temperature=0.1,  # Low temperature for deterministic tagging
                ),
            )

            text = response.text
            if not text:
                raise ValueError("Empty response from Gemini API")

            # Strip markdown fences if the model wraps them despite JSON mode
            text = text.strip()
            if text.startswith("```"):
                # Remove ```json ... ``` wrapper
                lines = text.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines)

            return json.loads(text)

        except json.JSONDecodeError as e:
            last_error = ValueError(
                f"Gemini returned invalid JSON (attempt {attempt + 1}/{MAX_RETRIES}): {e}\n"
                f"Raw response: {text[:500]}"
            )
        except Exception as e:
            last_error = e

        if attempt < MAX_RETRIES - 1:
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            time.sleep(delay)

    raise last_error  # type: ignore[misc]


# ── Main tagging function ───────────────────────────────────────────────────

def tag_document(
    merged_blocks: list[dict],
    footnote_candidates_dir: str | None = None,
    progress_cb=None,
    api_key: str | None = None,
) -> list[dict]:
    """Tag all blocks in a document using the Gemini API.

    Args:
        merged_blocks: The full list of blocks from the merged JSON
                       (the "blocks" array from *_structured_blocks_merged.json).
        footnote_candidates_dir: Optional path to directory containing
                                 page_N_footnote_candidates.json files.
        progress_cb: Optional callback(page_idx, total_pages, status_text)
                     for UI progress updates.
        api_key: Optional override for the API key (uses .env if not provided).

    Returns:
        A flat list of tag dicts (the tags_merged.json format the pipeline expects).

    Raises:
        ValueError: If the API key is missing or the API returns unusable output.
        Exception: On unrecoverable API errors after retries.
    """
    key = api_key or GEMINI_API_KEY
    if not key:
        raise ValueError(
            "No Gemini API key found. Set GEMINI_API_KEY in .env or pass api_key=."
        )

    system_prompt = _load_prompt()
    client = genai.Client(api_key=key)

    # Group blocks by page
    pages = _group_blocks_by_page(merged_blocks)
    page_indices = sorted(pages.keys())
    total_pages = len(page_indices)

    if total_pages == 0:
        return []

    tracking = json.loads(json.dumps(INITIAL_TRACKING))  # deep copy
    all_tagged_blocks: list[dict] = []
    prev_page_blocks: list[dict] = []  # for overlap context

    for i, page_idx in enumerate(page_indices):
        page_blocks = pages[page_idx]

        if progress_cb:
            progress_cb(page_idx, total_pages, f"Tagging page {page_idx + 1} of {total_pages}...")

        # Build overlap (last 3 blocks from previous page)
        overlap = prev_page_blocks[-3:] if prev_page_blocks else []

        # Load footnote candidates for this page
        footnote_candidates = _load_footnote_candidates(footnote_candidates_dir, page_idx)

        # Build the message and call the API
        user_message = _build_user_message(
            page_idx, page_blocks, overlap, tracking, footnote_candidates,
        )

        result = _call_gemini(client, system_prompt, user_message)

        # Extract tagged blocks from the response
        if isinstance(result, dict):
            tagged = result.get("tagged_blocks", [])
            # Update tracking state from AI's response for next page
            updated_tracking = result.get("updated_tracking")
            if updated_tracking and isinstance(updated_tracking, dict):
                tracking = updated_tracking
        elif isinstance(result, list):
            # Some AI responses are just a flat list of tagged blocks
            tagged = result
        else:
            raise ValueError(
                f"Unexpected response format for page {page_idx}: {type(result)}"
            )

        all_tagged_blocks.extend(tagged)
        prev_page_blocks = page_blocks

    if progress_cb:
        progress_cb(-1, total_pages, f"Done — {len(all_tagged_blocks)} blocks tagged.")

    return all_tagged_blocks


# ── CLI entry point ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Auto-tag PDF blocks using the Gemini API."
    )
    parser.add_argument(
        "merged_json",
        help="Path to *_structured_blocks_merged.json",
    )
    parser.add_argument(
        "--output", "-o",
        default="tags_merged.json",
        help="Output path for the merged tags JSON (default: tags_merged.json)",
    )
    parser.add_argument(
        "--footnote-candidates-dir",
        default=None,
        help="Directory containing page_N_footnote_candidates.json files",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Gemini API key (overrides .env)",
    )
    args = parser.parse_args()

    # Load blocks
    with open(args.merged_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    blocks = data["blocks"] if isinstance(data, dict) and "blocks" in data else data

    def progress(page_idx, total, status):
        print(f"  [{page_idx + 1}/{total}] {status}" if page_idx >= 0 else f"  {status}")

    print(f"Tagging {len(blocks)} blocks from {args.merged_json} ...")
    tags = tag_document(
        blocks,
        footnote_candidates_dir=args.footnote_candidates_dir,
        progress_cb=progress,
        api_key=args.api_key,
    )

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(tags, f, indent=2)

    print(f"Wrote {len(tags)} tagged blocks to {args.output}")


if __name__ == "__main__":
    main()
