"""
CLI entry point for PDF extraction.

Usage:
    python extract_pdf.py input.pdf [--output-dir ./output] [--pretty]

Examples:
    python extract_pdf.py report.pdf
    python extract_pdf.py report.pdf --output-dir ./my_output --pretty
"""

import argparse
import logging
import sys
import os

# Add project root to path so 'src' package is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.extractor import PDFExtractor
from src.utils import print_extraction_summary


def setup_logging(verbose: bool = False) -> None:
    """Configure logging output."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Extract structured blocks from a PDF using PyMuPDF.",
        epilog="Part of the AutomationAutoTag accessibility remediation engine.",
    )
    parser.add_argument(
        "pdf_path",
        help="Path to the input PDF file.",
    )
    parser.add_argument(
        "--output-dir",
        default="./output",
        help="Directory to store extraction outputs (default: ./output).",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        default=True,
        help="Pretty-print JSON output (default: True).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose/debug logging.",
    )

    args = parser.parse_args()
    return args


def main() -> int:
    """Main entry point."""
    args = parse_args()
    setup_logging(verbose=args.verbose)

    # Validate input file
    if not os.path.isfile(args.pdf_path):
        print(f"\n  ERROR: File not found: {args.pdf_path}\n")
        return 1

    print(f"\n  Extracting: {os.path.abspath(args.pdf_path)}")
    print(f"  Output to:  {os.path.abspath(args.output_dir)}\n")

    try:
        extractor = PDFExtractor(output_dir=args.output_dir)
        result = extractor.extract(
            pdf_path=args.pdf_path
        )

        # Print summary
        print_extraction_summary(result)
        
        print("  Extraction complete!")

        return 0

    except FileNotFoundError as e:
        print(f"\n  ERROR: {e}\n")
        return 1
    except ImportError as e:
        print(f"\n  ERROR: Missing dependency: {e}")
        print("  Run: pip install -r requirements.txt")
        return 1
    except Exception as e:
        logging.exception("Extraction failed")
        print(f"\n  ERROR: Extraction failed: {e}")
        print("  Run with -v flag for more details.\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
