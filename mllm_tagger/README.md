# MLLM Vision Tagger — Experimental

A vision-first approach to PDF/UA tagging. Instead of extracting text blocks
and sending them to an LLM, this approach renders each PDF page as an image
and sends it directly to a Multimodal LLM (MLLM), which visually identifies
every content element, its bounding box, and its semantic tag.

## Pipeline

```
Step 1:  render_pages.py         PDF → page images (playground/)
Step 2:  playground.py           Send images to MLLM → per-page JSON (playground/page_N_tags.json)
              (or manual)        You can also manually send images to an MLLM chat and save the JSON
Step 3:  merge_outputs.py        Merge per-page JSONs → merged_tags.json
Step 4:  inject_mllm_tags.py     Convert + inject tags → tagged PDF
```

## Files

| File | Purpose |
|------|---------|
| `render_pages.py` | Render PDF pages to PNG images in `playground/` |
| `playground.py` | Send page images to Gemini API, save per-page tag JSONs |
| `merge_outputs.py` | Merge all `page_N_tags.json` into one `merged_tags.json` |
| `inject_mllm_tags.py` | Convert MLLM output → inject_tags format → tagged PDF |
| `prompt.md` | System prompt for the MLLM |
| `tracking_template.json` | Initial tracking state template |
| `output_template.json` | JSON schema the MLLM must return |

## Quick Start

```bash
cd mllm_tagger

# 1. Render pages to images
python render_pages.py Test_Input.pdf

# 2. Send to MLLM (automated via Gemini API)
python playground.py Test_Input.pdf --all

# 3. Merge per-page outputs
python merge_outputs.py

# 4. Inject tags into PDF
python inject_mllm_tags.py
```

## Manual MLLM Workflow

If you prefer to use a chat interface (e.g., Gemini web UI, ChatGPT) instead
of the API:

1. Run `python render_pages.py` to get the page images in `playground/`
2. Upload each `page_N.png` to your MLLM along with `prompt.md` and `tracking_template.json`
3. Save each MLLM response as `playground/page_N_tags.json`
4. Run `python merge_outputs.py` to combine them
5. Run `python inject_mllm_tags.py` to produce the tagged PDF

## Coordinate System

The MLLM returns bounding boxes as **normalized coordinates** `[0, 1]`:
```
[x0, y0, x1, y1]  where 0.0 = top/left edge, 1.0 = bottom/right edge
```

`merge_outputs.py` converts these to PDF page coordinates using `pages_meta.json`.
