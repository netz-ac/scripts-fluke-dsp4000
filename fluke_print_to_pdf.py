#!/usr/bin/env python3
"""
Serial Printout to PDF Converter for Fluke DSP-4000
===================================================

Requires Python 3.13+.

An independent, unofficial tool that reads cable test printouts (both Full
Certification Reports and Summary Reports) printed via serial by a Fluke DSP-4000
cable analyzer (e.g., from /dev/ttyUSB0, a captured text file, or standard input),
automatically detects the report type, and generates a formatted PDF report.

Disclaimer:
Fluke and DSP-4000 are trademarks of Fluke Corporation. This software is an
independent open-source tool and is not affiliated with or endorsed by Fluke Corporation.
"""

import argparse
from pathlib import Path
import sys

from fluke_dsp4000 import DEFAULT_DEVICE, DEFAULT_PRINT_BAUD, read_print_stream
from fluke_report_pdf import (
    FlukeReport,
    FlukeSummaryReport,
    generate_pdf,
    parse_fluke_stream,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert serial printer output (Detail or Summary) for Fluke DSP-4000 into a formatted PDF report."
    )
    parser.add_argument("-i", "--input", help="Path to a saved printout text file (use '-' for stdin).")
    parser.add_argument(
        "-s",
        "--serial",
        default=DEFAULT_DEVICE,
        help=f"Serial port device to capture from (default: {DEFAULT_DEVICE}).",
    )
    parser.add_argument(
        "-b",
        "--baud",
        type=int,
        default=DEFAULT_PRINT_BAUD,
        help=f"Serial baud rate (default: {DEFAULT_PRINT_BAUD}).",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=3.0,
        help="Seconds of serial silence after data starts to consider printout finished (default: 3.0).",
    )
    parser.add_argument("-o", "--output", default="fluke_report.pdf", help="Output PDF file path (default: fluke_report.pdf).")
    parser.add_argument("--save-raw", default=None, help="Optional path to save the raw serial text alongside the PDF.")
    args = parser.parse_args()

    match args.input:
        case "-":
            raw_text = sys.stdin.read()
        case str(in_file):
            raw_text = Path(in_file).read_bytes().decode("latin-1", errors="replace")
        case None if not sys.stdin.isatty():
            raw_text = sys.stdin.read()
        case _:
            raw_text = read_print_stream(port=args.serial, baudrate=args.baud, idle_timeout=args.idle_timeout)

    if args.save_raw:
        Path(args.save_raw).write_text(raw_text, encoding="utf-8")

    if not (items := parse_fluke_stream(raw_text)):
        print("[!] Error: Could not parse any valid report or summary for Fluke DSP-4000 from input.", file=sys.stderr)
        return 1

    n_detail = sum(isinstance(x, FlukeReport) for x in items)
    n_summary = sum(isinstance(x, FlukeSummaryReport) for x in items)
    generate_pdf(items, args.output)
    print(f"[+] Successfully generated PDF ({n_summary} summary report(s), {n_detail} detail report(s)): {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
