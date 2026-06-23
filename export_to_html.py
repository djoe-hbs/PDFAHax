import argparse
import json
import os

def generate_html(blocks_json_path: str, tags_json_path: str, output_html_path: str):
    """
    Takes the structured blocks and the AI tags, and generates a semantic HTML file.
    This HTML file can be directly opened in Adobe Acrobat (File -> Create -> PDF from Web Page)
    or dragged into Acrobat to generate a perfectly tagged PDF for proof of concept.
    """
    # Load data
    with open(blocks_json_path, 'r', encoding='utf-8') as f:
        blocks_data = json.load(f)
        
    with open(tags_json_path, 'r', encoding='utf-8') as f:
        tags_data = json.load(f)

    # Create mapping of block_id -> tag_info
    tags_map = {item["block_id"]: item for item in tags_data}

    # Sort blocks by reading order from the AI
    sorted_blocks = []
    for block in blocks_data["blocks"]:
        block_id = block["block_id"]
        tag_info = tags_map.get(block_id)
        if tag_info:
            sorted_blocks.append((tag_info.get("reading_order", 999), block, tag_info))
            
    sorted_blocks.sort(key=lambda x: x[0])

    # Build HTML
    html_lines = [
        "<!DOCTYPE html>",
        "<html lang='en'>",
        "<head>",
        "    <meta charset='UTF-8'>",
        f"    <title>{blocks_data['document']['source_file']}</title>",
        "    <style>",
        "        body { font-family: Arial, sans-serif; max-width: 800px; margin: 40px auto; line-height: 1.6; }",
        "        h1, h2, h3, h4, h5, h6 { color: #333; margin-top: 1.5em; }",
        "        ul, ol { margin-bottom: 1em; }",
        "        p { margin-bottom: 1em; }",
        "    </style>",
        "</head>",
        "<body>"
    ]

    in_list = False

    for order, block, tag_info in sorted_blocks:
        tag = tag_info.get("tag", "P").upper()
        parent_tag = tag_info.get("parent_tag")
        text = block.get("text", "").replace("\n", " ")

        # Handle list wrapping
        if parent_tag == "L" or tag == "LI":
            if not in_list:
                html_lines.append("    <ul>")
                in_list = True
        else:
            if in_list:
                html_lines.append("    </ul>")
                in_list = False

        # Generate elements based on tag
        if tag in ["H1", "H2", "H3", "H4", "H5", "H6"]:
            html_lines.append(f"    <{tag.lower()}>{text}</{tag.lower()}>")
        elif tag == "P":
            if in_list: # Sometimes paragraphs are inside list items
                html_lines.append(f"        <p>{text}</p>")
            else:
                html_lines.append(f"    <p>{text}</p>")
        elif tag == "LI":
            html_lines.append(f"        <li>{text}</li>")
        elif tag == "FIGURE":
            alt_text = tag_info.get("alt_text", "Image")
            html_lines.append(f"    <figure><img src='placeholder.jpg' alt='{alt_text}'><figcaption>{alt_text}</figcaption></figure>")
        elif tag == "ARTIFACT":
            # Artifacts are ignored by screen readers, so we use aria-hidden
            html_lines.append(f"    <div aria-hidden='true' style='color: #ccc;'>{text}</div>")
        else:
            # Fallback
            html_lines.append(f"    <div>{text}</div>")

    # Close list if still open
    if in_list:
        html_lines.append("    </ul>")

    html_lines.extend([
        "</body>",
        "</html>"
    ])

    with open(output_html_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(html_lines))
        
    print(f"Semantic HTML generated at: {os.path.abspath(output_html_path)}")
    print("Drag and drop this HTML file into Adobe Acrobat to see the tags generated!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("blocks_json", help="structured_blocks.json from extraction")
    parser.add_argument("tags_json", help="The JSON array returned by the AI (test.json)")
    parser.add_argument("--output", default="tagged_document.html", help="Output HTML path")
    
    args = parser.parse_args()
    generate_html(args.blocks_json, args.tags_json, args.output)
