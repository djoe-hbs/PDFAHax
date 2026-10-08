import json
import os
import sys

def map_coordinates(odl_box, page_height):
    # odl_box: [left, bottom, right, top]
    left, bottom, right, top = odl_box
    # PyMuPDF: [x0, y0, x1, y1] with origin at top-left
    # Note: ODL's top is the largest Y value (closest to top edge in PDF native, meaning smallest y in PyMuPDF)
    x0 = left
    y0 = page_height - top
    x1 = right
    y1 = page_height - bottom
    return [x0, y0, x1, y1]

def flatten_kids(kids, page_height):
    blocks = []
    
    def traverse(elements, current_list_id=None):
        for elem in elements:
            if "bounding box" not in elem:
                continue
                
            text = elem.get("content", "").strip()
            if not text:
                # Still recurse if it has kids, even if the container is empty
                if "kids" in elem and elem["kids"]:
                    traverse(elem["kids"], current_list_id=elem.get("id"))
                if "list items" in elem and elem["list items"]:
                    traverse(elem["list items"], current_list_id=elem.get("id"))
                continue
                
            block = {
                "type": elem.get("type", "paragraph"),
                "pdfua_tag": elem.get("pdfua_tag", "P"),
                "bbox": map_coordinates(elem["bounding box"], page_height),
                "text": elem.get("content", ""),
                "page_idx": elem.get("page number", 1) - 1,
                "metadata": {}
            }
            if current_list_id is not None:
                block["metadata"]["parent_list_id"] = current_list_id
                
            if elem.get("type") == "list item":
                block["type"] = "list_item"
                text = block["text"]
                if text:
                    block["metadata"]["list_marker"] = text.split(" ")[0]
                    block["metadata"]["marker_x0"] = block["bbox"][0]
                    block["metadata"]["body_x0"] = block["bbox"][0] + 15
            
            blocks.append(block)
            
            if "kids" in elem and elem["kids"]:
                traverse(elem["kids"], current_list_id=elem.get("id"))
            if "list items" in elem and elem["list items"]:
                traverse(elem["list items"], current_list_id=elem.get("id"))
                
    traverse(kids)
    return blocks

def main(pdf_path, odl_json_path, output_path):
    page_height = 792.0
    
    with open(odl_json_path, 'r', encoding='utf-8') as f:
        odl_data = json.load(f)
        
    kids = odl_data.get('kids', [])
    total_pages = odl_data.get("number of pages", 1)
    
    flat_blocks = flatten_kids(kids, page_height)
    
    flat_blocks.sort(key=lambda b: (b['page_idx'], b['bbox'][1], b['bbox'][0]))
    
    for i, b in enumerate(flat_blocks):
        b["block_id"] = i
        
    out_data = {
        "document": {"total_pages": total_pages},
        "blocks": flat_blocks
    }
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(out_data, f, indent=2)
        
    print(f"Extracted {len(flat_blocks)} blocks from ODL JSON -> {output_path}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
