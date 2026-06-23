import argparse
import json
import os
import fitz  # PyMuPDF

def visualize_tags(pdf_path: str, blocks_json_path: str, tags_json_path: str, output_pdf_path: str):
    """
    Takes the original PDF, the structured blocks JSON, and the AI's tags JSON,
    and creates a new PDF with visual overlays showing the assigned tags.
    """
    # 1. Load data
    with open(blocks_json_path, 'r', encoding='utf-8') as f:
        blocks_data = json.load(f)
        
    with open(tags_json_path, 'r', encoding='utf-8') as f:
        tags_data = json.load(f)

    # Create a mapping of block_id -> tag_info
    tags_map = {item["block_id"]: item for item in tags_data}

    # 2. Open PDF
    doc = fitz.open(pdf_path)

    # Define colors for tags
    colors = {
        "H1": (1, 0, 0),       # Red
        "H2": (1, 0.5, 0),     # Orange
        "H3": (0.8, 0.8, 0),   # Yellow
        "P": (0, 0, 1),        # Blue
        "LI": (0, 0.8, 0),     # Green
        "Figure": (0.5, 0, 0.5), # Purple
        "Artifact": (0.5, 0.5, 0.5), # Gray
    }

    # 3. Draw overlays
    for block in blocks_data["blocks"]:
        block_id = block["block_id"]
        page_idx = block["page_idx"]
        bbox = block.get("bbox")
        
        if not bbox or len(bbox) != 4:
            continue
            
        tag_info = tags_map.get(block_id)
        if not tag_info:
            continue
            
        tag = tag_info.get("tag", "P")
        order = tag_info.get("reading_order", "?")
        
        # Get color based on tag, default to black
        color = colors.get(tag, (0, 0, 0))
        
        page = doc[page_idx]
        rect = fitz.Rect(bbox)
        
        # Draw bounding box
        page.draw_rect(rect, color=color, width=1.5)
        
        # Draw tag label
        label = f"[{order}] {tag}"
        
        # Add a small background rectangle for the text to make it readable
        text_rect = fitz.Rect(rect.x0, max(0, rect.y0 - 12), rect.x0 + len(label) * 6, rect.y0)
        page.draw_rect(text_rect, color=color, fill=color)
        
        # Insert the text label
        page.insert_text(
            (rect.x0 + 2, rect.y0 - 2),
            label,
            fontsize=10,
            color=(1, 1, 1) # White text
        )

    # 4. Save
    doc.save(output_pdf_path)
    doc.close()
    print(f"Visualized PDF saved to: {os.path.abspath(output_pdf_path)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf_path", help="Original PDF")
    parser.add_argument("blocks_json", help="structured_blocks.json from extraction")
    parser.add_argument("tags_json", help="The JSON array returned by the AI")
    parser.add_argument("--output", default="tagged_visual.pdf", help="Output PDF path")
    
    args = parser.parse_args()
    visualize_tags(args.pdf_path, args.blocks_json, args.tags_json, args.output)
