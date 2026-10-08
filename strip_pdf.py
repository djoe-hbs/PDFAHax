import pikepdf
import sys

def strip_tags(in_path, out_path):
    pdf = pikepdf.Pdf.open(in_path)
    if "/StructTreeRoot" in pdf.Root:
        del pdf.Root["/StructTreeRoot"]
    if "/MarkInfo" in pdf.Root:
        del pdf.Root["/MarkInfo"]
    for page in pdf.pages:
        if "/Resources" in page and "/Properties" in page["/Resources"]:
            del page["/Resources"]["/Properties"]
        # Strip BDC/BMC/EMC from content stream
        from pikepdf import Operator
        instrs = pikepdf.parse_content_stream(page)
        new_instrs = []
        for ops, op in instrs:
            if str(op) not in ("BDC", "BMC", "EMC"):
                new_instrs.append((ops, op))
        page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(new_instrs))
    pdf.save(out_path)

if __name__ == "__main__":
    strip_tags(sys.argv[1], sys.argv[2])
