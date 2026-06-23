"""
PDF/UA validation gate using veraPDF.

Runs veraPDF against a tagged PDF, saves the machine-readable report, and prints
a human-readable pass/fail summary grouped by ISO 14289-1 (PDF/UA-1) rule, with
the number of failing checks and example locations.

Usage:
    python validate_pdf.py tagged_output.pdf
    python validate_pdf.py tagged_output.pdf --profile ua1 --report myreport.xml

Exit code: 0 if compliant, 1 if not (so it can act as a CI/pipeline gate).

veraPDF discovery order:
    1. --verapdf CLI argument
    2. VERAPDF environment variable
    3. verapdf / verapdf.bat on PATH
    4. Default install: %USERPROFILE%\\veraPDF\\verapdf.bat
"""

import argparse
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


def find_verapdf(explicit: str | None) -> str:
    candidates = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("VERAPDF"):
        candidates.append(os.environ["VERAPDF"])
    on_path = shutil.which("verapdf") or shutil.which("verapdf.bat")
    if on_path:
        candidates.append(on_path)
    candidates.append(os.path.join(os.environ.get("USERPROFILE", ""), "veraPDF", "verapdf.bat"))

    for c in candidates:
        if c and os.path.isfile(c):
            return c
    raise FileNotFoundError(
        "veraPDF not found. Pass --verapdf <path>, set VERAPDF env var, or install "
        "to %USERPROFILE%\\veraPDF."
    )


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _find(el, tag):
    for e in el.iter():
        if _local(e.tag) == tag:
            return e
    return None


def _findall(el, tag):
    return [e for e in el.iter() if _local(e.tag) == tag]


def run_validation(verapdf: str, pdf_path: str, profile: str, report_path: str) -> bool:
    """Run veraPDF, write MRR XML to report_path. Returns True if compliant."""
    cmd = [verapdf, "-f", profile, "--format", "mrr", pdf_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # veraPDF writes the report to stdout; stderr carries parser warnings.
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(proc.stdout)

    nested = [ln for ln in proc.stderr.splitlines() if "Nested MCID" in ln]
    if nested:
        print(f"  [!] veraPDF emitted {len(nested)} 'Nested MCID' warning(s) "
              f"(malformed marked-content nesting in the content stream).")

    return summarize(report_path)


def summarize(report_path: str) -> bool:
    tree = ET.parse(report_path)
    root = tree.getroot()

    vr = _find(root, "validationReport")
    details = _find(root, "details")
    profile = vr.get("profileName") if vr is not None else "?"
    compliant = (vr.get("isCompliant") == "true") if vr is not None else False

    print("=" * 72)
    print(f"  Profile  : {profile}")
    print(f"  COMPLIANT: {'YES' if compliant else 'NO'}")
    if details is not None:
        print(f"  Rules    — passed: {details.get('passedRules')}  "
              f"failed: {details.get('failedRules')}")
        print(f"  Checks   — passed: {details.get('passedChecks')}  "
              f"failed: {details.get('failedChecks')}")
    print("=" * 72)

    failed = [r for r in _findall(root, "rule") if r.get("status") == "failed"]
    # Sort by number of failing checks, descending — biggest problems first.
    failed.sort(key=lambda r: int(r.get("failedChecks", 0)), reverse=True)

    if not failed:
        print("\n  No failed rules. \U0001F389")
        return compliant

    print(f"\n  FAILED RULES ({len(failed)}), worst first:\n")
    for r in failed:
        clause = r.get("clause")
        test = r.get("testNumber")
        fc = r.get("failedChecks")
        desc_el = _find(r, "description")
        desc = (desc_el.text or "").strip() if desc_el is not None else ""
        print(f"  - ISO 14289-1 {clause} (test {test}) | failedChecks={fc}")
        print(f"      {desc[:150]}")
        checks = [c for c in _findall(r, "check") if c.get("status") == "failed"]
        for c in checks[:1]:
            ctx = _find(c, "context")
            ctx_t = (ctx.text or "").strip() if ctx is not None else ""
            if ctx_t:
                print(f"      @ {ctx_t[:95]}")
        print()

    return compliant


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate a PDF against PDF/UA using veraPDF.")
    ap.add_argument("pdf_path", help="PDF to validate")
    ap.add_argument("--profile", default="ua1", help="veraPDF flavour (default: ua1)")
    ap.add_argument("--report", default=None, help="MRR XML report path (default: <pdf>_verapdf.xml)")
    ap.add_argument("--verapdf", default=None, help="Path to verapdf.bat / verapdf")
    args = ap.parse_args()

    if not os.path.isfile(args.pdf_path):
        print(f"ERROR: file not found: {args.pdf_path}")
        return 2

    report = args.report or os.path.splitext(args.pdf_path)[0] + "_verapdf.xml"
    verapdf = find_verapdf(args.verapdf)

    print(f"  veraPDF : {verapdf}")
    print(f"  PDF     : {os.path.abspath(args.pdf_path)}")
    print(f"  Report  : {os.path.abspath(report)}")

    compliant = run_validation(verapdf, args.pdf_path, args.profile, report)
    return 0 if compliant else 1


if __name__ == "__main__":
    sys.exit(main())
