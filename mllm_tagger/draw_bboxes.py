import json
import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

def draw_bboxes(merged_json_path, playground_dir):
    with open(merged_json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    elements = data.get('elements', [])
    pages = {}
    
    # Group elements by page
    for el in elements:
        page_idx = el.get('page_idx', 0)
        if page_idx not in pages:
            pages[page_idx] = []
        pages[page_idx].append(el)
        
    playground_dir = Path(playground_dir)
    
    # Process each page
    for page_idx, elems in pages.items():
        img_path = playground_dir / f"page_{page_idx}.png"
        if not img_path.exists():
            print(f"Image not found: {img_path}")
            continue
            
        print(f"Annotating {img_path.name} with {len(elems)} bboxes...")
        img = Image.open(img_path).convert('RGB')
        draw = ImageDraw.Draw(img)
        
        # Try to load a font, otherwise use default
        try:
            # Arial or similar standard font
            font = ImageFont.truetype("arial.ttf", 24)
        except:
            font = ImageFont.load_default()
            
        width, height = img.size
        
        for el in elems:
            # We use bbox_normalized since we are drawing on the raw image
            # Wait, bbox_normalized is in [0, 1] relative to the image
            bbox_norm = el.get('bbox_normalized')
            if not bbox_norm:
                continue
                
            x0 = bbox_norm[0] * width
            y0 = bbox_norm[1] * height
            x1 = bbox_norm[2] * width
            y1 = bbox_norm[3] * height
            
            tag = el.get('tag', 'UNKNOWN')
            
            # Choose color based on tag
            color = "red"
            if tag.startswith("H"): color = "blue"
            elif tag == "P": color = "green"
            elif tag == "Artifact": color = "gray"
            elif tag == "Figure": color = "purple"
            elif tag in ["Table", "TD", "TH", "TR"]: color = "orange"
            elif tag in ["L", "LI"]: color = "brown"
            
            draw.rectangle([x0, y0, x1, y1], outline=color, width=4)
            
            # Draw text label background
            text_bbox = draw.textbbox((x0, y0), tag, font=font)
            draw.rectangle(text_bbox, fill=color)
            draw.text((x0, y0), tag, fill="white", font=font)
            
        out_path = playground_dir / f"annotated_page_{page_idx}.png"
        img.save(out_path)
        print(f"Saved {out_path.name}")

if __name__ == "__main__":
    draw_bboxes("merged_tags.json", "playground")
