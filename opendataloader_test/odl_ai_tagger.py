import json
import sys
from google import genai
from google.genai import types
import os
from dotenv import load_dotenv
load_dotenv()

def call_gemini(blocks_json_str):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable not set")
    
    client = genai.Client(api_key=api_key)
    
    prompt = f"""
You are a PDF/UA accessibility tagging semantic judge.
You are given a list of document blocks extracted by OpenDataLoader. Each block has text, coordinates, page_idx, and a "pdfua_tag" hint.
Your job is to read the text, the hint, and the spatial context to output the final, corrected PDF/UA tag.

Allowed tags: Document, Sect, H1, H2, H3, H4, H5, H6, P, L, LI, Lbl, LBody, Table, TR, TH, TD, Caption, Figure, Artifact, TOC, TOCI.

CRITICAL RULES:
1. **Table of Contents:** If a block looks like a Table of Contents item (e.g., text followed by repeating dots and a page number like "Introduction ...................... 4"), tag it as `TOCI`.
2. **Artifacts:** If a block is just a page number (e.g., "12") at the top or bottom of a page, or repeating header text across pages, tag it as `Artifact`.
3. **List Items:** Ensure that bulleted lists or numbered lists are strictly tagged as `LI` (even if the hint says `P`).
4. **Headings:** Evaluate heading hints based on their text and size. Adjust heading levels logically (H1, H2, H3, H4) so they form a proper hierarchy.

Output ONLY a JSON array of objects, one for each block, containing:
{{
  "block_id": <int>,
  "tag": "<string>"
}}

Blocks:
{blocks_json_str}
"""
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.0)
    )
    txt = response.text
    if "```json" in txt:
        txt = txt.split("```json")[1].split("```")[0]
    elif "```" in txt:
        txt = txt.split("```")[1].split("```")[0]
    return json.loads(txt.strip())

def main(input_path, output_path):
    with open(input_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    blocks = data.get("blocks", [])
    
    # Pre-format for AI
    ai_input = []
    for b in blocks:
        ai_input.append({
            "block_id": b["block_id"],
            "page_idx": b["page_idx"],
            "bbox": b["bbox"],
            "text": b["text"][:150] + ("..." if len(b["text"]) > 150 else ""),
            "hint": b["pdfua_tag"]
        })
        
    print(f"Calling Gemini to judge {len(ai_input)} blocks...")
    tags = call_gemini(json.dumps(ai_input, indent=2))
    
    # Merge back
    tags_map = {t["block_id"]: t["tag"] for t in tags}
    for b in blocks:
        b["tag"] = tags_map.get(b["block_id"], b["pdfua_tag"])
        
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(tags, f, indent=2)
        
    print(f"Saved judged tags to {output_path}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
