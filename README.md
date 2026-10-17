# Serial Utilities & PDF Generator for Fluke DSP-4000 (`scripts-fluke-dsp4000`)

Zero-dependency Python 3.13+ utilities for **Fluke DSP-4000** series cable analyzers over RS-232 (`/dev/ttyUSB0`).

Repository: [https://github.com/netz-ac/scripts-fluke-dsp4000](https://github.com/netz-ac/scripts-fluke-dsp4000)

## Tools

- [`fetch_reports.py`](fetch_reports.py) — Downloads saved reports (`115200 8N1`) and exports **JSON** + vector **PDF**.
- [`fluke_print_to_pdf.py`](fluke_print_to_pdf.py) — Captures serial printer output (`115200 8N1` or text file) and generates a **PDF** (auto-detects full vs. summary reports).
- [`live-screenshot.py`](live-screenshot.py) — Captures the 240x200 LCD display (`115200 8N1`) to a **PNG** image (one-shot or live refresh).
- [`fluke_dsp4000.py`](fluke_dsp4000.py) & [`fluke_report_pdf.py`](fluke_report_pdf.py) — Shared serial protocol and PDF 1.4 rendering libraries.

## Quick Start

Requires **Python 3.13+** on Linux/POSIX (no external packages needed).

```bash
# 1. Download saved reports from device -> DSP_Reports.json + DSP_Reports.pdf
python3 fetch_reports.py -d /dev/ttyUSB0
python3 fetch_reports.py --from-json samples/sample_fetched_reports.json -p report.pdf

# 2. Capture serial printer stream -> PDF (or convert saved printout text)
python3 fluke_print_to_pdf.py --serial /dev/ttyUSB0 -o fluke_report.pdf
python3 fluke_print_to_pdf.py -i samples/sample_report.txt -o samples/sample_report.pdf

# 3. Capture LCD display screenshot -> PNG (single frame or refresh every 1s)
python3 live-screenshot.py -d /dev/ttyUSB0 -o screenshot.png
python3 live-screenshot.py -d /dev/ttyUSB0 -i 1.0 --ascii
```

Run any script with `--help` for all command-line options. Example inputs and generated outputs are in [`samples/`](samples/).

## Trademark Notice & License

*Fluke* and *DSP-4000* are trademarks of Fluke Corporation and are used strictly for identification and compatibility purposes. This project is an independent third-party tool and is not affiliated with or endorsed by Fluke Corporation.

Licensed under the [MIT License](LICENSE).
