#!/usr/bin/env python3
"""
Saved Report Fetcher & PDF Generator for Fluke DSP-4000
=======================================================

Requires Python 3.13+.

Connects to a Fluke DSP-4000 cable analyzer over serial (default: /dev/ttyUSB0 @ 115200 8N1),
queries device metadata ("hga"), saved report count ("rc"), report summaries ("rs"),
and downloads each saved test report ("tu<idx>,1", "lm<idx>,0", "tg<idx>").
Exports the retrieved reports to JSON, a formatted vector PDF report, or both.
Can also convert a previously saved JSON file to PDF offline via `--from-json`.

Disclaimer:
Fluke and DSP-4000 are trademarks of Fluke Corporation. This software is an
independent open-source tool and is not affiliated with or endorsed by Fluke Corporation.
"""

import argparse
from itertools import batched
import json
from pathlib import Path
import struct
import time
from typing import Any

from fluke_dsp4000 import (
    DEFAULT_BAUD,
    DEFAULT_DEVICE,
    PosixSerial,
    close_session,
    init_session,
    read_framed_response,
    send_ack,
    wake_device,
)
from fluke_report_pdf import (
    FlukeReport,
    FlukeSummaryReport,
    fetched_data_to_pdf_items,
    generate_pdf,
)


SUMMARY_RECORD_SIZE = 34
TU_HEADER_SIZE = 0x56  # 86 bytes before the 6-byte measurement tuples
PAIR_LABELS_RAW = ("1,2", "3,6", "4,5", "7,8")
PIN_NAMES = (*(f"Pin {p}" for p in range(1, 9)), "Shield")


def extract_cstring(buf: bytes, offset: int = 0, max_len: int | None = None) -> str:
    """Extract a null-terminated ASCII string from `buf` starting at `offset`."""
    sub = buf[offset:] if max_len is None else buf[offset : offset + max_len]
    return sub.partition(b"\x00")[0].decode("ascii", errors="replace").strip()


def parse_hga_metadata(hga: bytes) -> dict[str, Any]:
    """Parse the 1299-byte 'hga' hardware/global attributes block."""
    raw_sn = extract_cstring(hga, 0x50B, 16) if len(hga) > 0x50B else ""
    return {
        "raw_length": len(hga),
        "operator": extract_cstring(hga, 0x000, 367) if hga else "",
        "client_name": extract_cstring(hga, 0x26D, 200) if len(hga) > 0x26D else "",
        "company_name": extract_cstring(hga, 0x4DA, 32) if len(hga) > 0x4DA else "",
        "firmware_version": extract_cstring(hga, 0x4FF, 6) if len(hga) > 0x4FF else "",
        "standards_version": extract_cstring(hga, 0x505, 5) if len(hga) > 0x505 else "",
        "serial_number": raw_sn.lstrip(">").strip(),
    }


def parse_report_summaries(rs_payload: bytes) -> list[dict[str, Any]]:
    """Parse 34-byte report summary records returned by 'rs\\ng'."""
    usable_len = (len(rs_payload) // SUMMARY_RECORD_SIZE) * SUMMARY_RECORD_SIZE
    summaries: list[dict[str, Any]] = []
    for i, rec_tuple in enumerate(batched(rs_payload[:usable_len], SUMMARY_RECORD_SIZE, strict=True)):
        rec = bytes(rec_tuple)
        std_id, cable_type_id, result_code = struct.unpack_from(">HHB", rec, 0x18)
        (nvp_raw,) = struct.unpack_from(">H", rec, 0x20)
        summaries.append(
            {
                "index": i,
                "cable_id": extract_cstring(rec, 0x00, 24),
                "result": "PASS" if result_code == 0 else "FAIL",
                "result_code": result_code,
                "standard_id": f"0x{std_id:04x}",
                "cable_type_id": f"0x{cable_type_id:04x}",
                "nvp_percent": round(nvp_raw / 10.0, 1),
                "raw_hex": rec.hex(),
            }
        )
    return summaries


def decode_wiremap_byte(b: int) -> str:
    """Decode a single pin byte from the 9-byte wiremap array (pins 1..8, shield)."""
    match b:
        case 0x29:
            return "Open"
        case _ if b & 0x80:
            pin = (b & 0x7F) + 1
            return "Shield" if pin == 9 else f"Pin {pin}"
        case _:
            return f"0x{b:02x}"


def parse_lm_limits(lm_payload: bytes) -> dict[str, Any]:
    """Extract cable parameter limits (length, propagation delay, delay skew) from 'lm' payload."""
    limits: dict[str, Any] = {
        "length_limit_m": 90.0,
        "prop_delay_limit_ns": 498,
        "delay_skew_limit_ns": 44,
    }
    if len(lm_payload) >= 0x2E:
        len_lim_raw, _, _, skew_lim_raw, delay_lim_raw = struct.unpack_from(">HHHHH", lm_payload, 0x24)
        if len_lim_raw > 0:
            limits["length_limit_m"] = round(len_lim_raw / 10.0, 1)
        if delay_lim_raw > 0:
            limits["prop_delay_limit_ns"] = delay_lim_raw
        if skew_lim_raw > 0:
            limits["delay_skew_limit_ns"] = skew_lim_raw
    return limits


def parse_tu_record(tu_payload: bytes, lm_payload: bytes, tg_payload: bytes, index: int) -> dict[str, Any]:
    """Parse the 1094-byte 'tu' saved test report along with 'lm' (cable model) and 'tg' (test standard)."""
    cable_type_name = extract_cstring(lm_payload, 0)
    test_standard_name = extract_cstring(tg_payload, 0)
    cable_limits = parse_lm_limits(lm_payload)

    if len(tu_payload) < TU_HEADER_SIZE:
        return {
            "index": index,
            "cable_type": cable_type_name,
            "test_standard": test_standard_name,
            "cable_limits": cable_limits,
            "raw_tu_hex": tu_payload.hex(),
            "raw_lm_hex": lm_payload.hex(),
            "raw_tg_hex": tg_payload.hex(),
        }

    cable_id = extract_cstring(tu_payload, 0x06, 24)
    std_id, cable_type_id, result_code = struct.unpack_from(">HHB", tu_payload, 0x1E)
    anomaly_thresh = tu_payload[0x26]
    (nvp_raw,) = struct.unpack_from(">H", tu_payload, 0x2A)
    (remote_sn_int,) = struct.unpack_from(">I", tu_payload, 0x2C)
    main_lia_id, remote_lia_id = struct.unpack_from(">HH", tu_payload, 0x30)

    # 9-byte wiremap at 0x34..0x3d (Pins 1-8 + Shield)
    wiremap = {
        pin_name: decode_wiremap_byte(b)
        for pin_name, b in zip(PIN_NAMES, tu_payload[0x34:0x3D])
    }

    # Pair parameter words at 0x3e..0x56 (Pairs 1,2 / 3,6 / 4,5 / 7,8)
    pair_lengths_raw = struct.unpack_from(">HHHH", tu_payload, 0x3E)
    pair_delays_raw = struct.unpack_from(">HHHH", tu_payload, 0x46)
    pair_skews_raw = struct.unpack_from(">HHHH", tu_payload, 0x4E)

    # 6-byte measurement tuples from 0x56 to end: (val_raw, limit_raw, freq_raw)
    tuples: list[dict[str, Any]] = []
    for idx, off in enumerate(range(TU_HEADER_SIZE, len(tu_payload) - 5, 6)):
        w0, w1, w2 = struct.unpack_from(">HHH", tu_payload, off)
        raw_mag = w0 & 0x3FFF
        signed_mag = (raw_mag - 0x4000) if (raw_mag & 0x2000) else raw_mag
        tuples.append(
            {
                "tuple_index": idx,
                "offset": f"0x{off:03x}",
                "passed": bool(w0 & 0x8000),
                "value": None if raw_mag == 0x3FFF else round(signed_mag / 10.0, 1),
                "limit": round(w1 / 10.0, 1),
                "frequency_mhz": round(w2 / 10.0, 1),
                "raw_words": [f"0x{w0:04x}", f"0x{w1:04x}", f"0x{w2:04x}"],
            }
        )

    return {
        "index": index,
        "cable_id": cable_id,
        "result": "PASS" if result_code == 0 else "FAIL",
        "result_code": result_code,
        "test_standard": test_standard_name,
        "cable_type": cable_type_name,
        "standard_id": f"0x{std_id:04x}",
        "cable_type_id": f"0x{cable_type_id:04x}",
        "nvp_percent": round(nvp_raw / 10.0, 1),
        "fault_anomaly_threshold_percent": anomaly_thresh,
        "remote_serial_number": str(remote_sn_int) if remote_sn_int > 0 else "",
        "main_adapter": f"LIA{main_lia_id:03d}" if main_lia_id > 0 else "",
        "remote_adapter": f"LIA{remote_lia_id:03d}" if remote_lia_id > 0 else "",
        "cable_limits": cable_limits,
        "wiremap": wiremap,
        "pair_metrics_raw": {
            label: {
                "length_m": round((w_len & 0x07FF) / 10.0, 1),
                "word_0x3e": w_len,
                "word_0x46": None if w_del == 0x3FFF else w_del,
                "word_0x4e": None if w_skw == 0x8000 else w_skw,
            }
            for label, w_len, w_del, w_skw in zip(
                PAIR_LABELS_RAW, pair_lengths_raw, pair_delays_raw, pair_skews_raw
            )
        },
        "measurement_tuples": tuples,
        "raw_tu_hex": tu_payload.hex(),
        "raw_lm_hex": lm_payload.hex(),
        "raw_tg_hex": tg_payload.hex(),
    }


def fetch_all_reports(ser: PosixSerial, target_index: int | None = None) -> dict[str, Any]:
    """Execute the full report retrieval workflow over serial."""
    wake_device(ser)
    instrument_type = init_session(ser)

    ser.write_paced(b"rf\nid\n")
    fw_resp = ser.read_until(b"\r\n").decode("ascii", errors="replace").strip().removeprefix("g")

    ser.write_paced(b"tv\n")
    tv_resp = ser.read_until(b"\r\n").decode("ascii", errors="replace").strip()

    ser.write_paced(b"hga\ng")
    hga_payload = read_framed_response(ser)
    send_ack(ser)
    device_info = parse_hga_metadata(hga_payload) | {
        "instrument_type": instrument_type,
        "id_response": fw_resp,
        "tv_response": tv_resp,
    }

    init_session(ser)
    ser.write_paced(b"rc\n")
    rc_str = ser.read_until(b"\r\n").decode("ascii", errors="replace").strip()
    report_count = int(rc_str) if rc_str.isdigit() else 0

    summaries: list[dict[str, Any]] = []
    if report_count > 0:
        ser.write_paced(b"dc1\nrs\ng")
        rs_payload = read_framed_response(ser)
        send_ack(ser)
        summaries = parse_report_summaries(rs_payload)

    reports: list[dict[str, Any]] = []
    indices = [target_index] if target_index is not None else list(range(report_count))

    for idx in indices:
        if not (0 <= idx < report_count):
            print(f"Skipping report index {idx} (device has {report_count} report(s))")
            continue

        print(f"Fetching report #{idx} (tu{idx},1 / lm{idx},0 / tg{idx})...")
        ser.write_paced(f"dc1\ntu{idx},1\n".encode("ascii"))
        time.sleep(0.18)  # 180ms preparation delay before 'g' observed in capture
        ser.write_paced(b"g")
        tu_payload = read_framed_response(ser)
        send_ack(ser)

        ser.write_paced(f"lm{idx},0\ng".encode("ascii"))
        lm_payload = read_framed_response(ser)
        send_ack(ser)

        ser.write_paced(f"tg{idx}\ng".encode("ascii"))
        tg_payload = read_framed_response(ser)
        send_ack(ser)

        reports.append(parse_tu_record(tu_payload, lm_payload, tg_payload, idx))

    return {
        "device": device_info,
        "report_count": report_count,
        "summaries": summaries,
        "reports": reports,
    }


def print_report_summary(data: dict[str, Any]) -> None:
    """Print a clean human-readable summary of the device and fetched reports."""
    dev = data.get("device", {})
    print("\n=== DEVICE INFORMATION ===")
    print(f"  Instrument Type   : {dev.get('instrument_type')}")
    print(f"  Serial Number     : {dev.get('serial_number')}")
    print(f"  Firmware Version  : {dev.get('firmware_version')} (tv={dev.get('tv_response')})")
    print(f"  Standards Version : {dev.get('standards_version')}")
    print(f"  Operator          : {dev.get('operator')}")
    print(f"  Client / Site     : {dev.get('client_name')}")
    print(f"  Company           : {dev.get('company_name')}")
    print(f"  Saved Reports (rc): {data.get('report_count')}")

    if summaries := data.get("summaries"):
        print("\n=== SAVED REPORT SUMMARIES (rs) ===")
        for s in summaries:
            print(
                f"  [{s['index']}] Cable ID: {s['cable_id']!r:<12} | Result: {s['result']:<4} | "
                f"NVP: {s['nvp_percent']}% | StdID: {s['standard_id']}"
            )

    for rep in data.get("reports", []):
        print(f"\n=== REPORT #{rep['index']}: {rep['cable_id']!r} ({rep['result']}) ===")
        print(f"  Test Standard : {rep.get('test_standard')} ({rep.get('standard_id')})")
        print(f"  Cable Type    : {rep.get('cable_type')} ({rep.get('cable_type_id')})")
        print(f"  NVP           : {rep.get('nvp_percent')}%")
        print(f"  Wiremap       : {', '.join(f'{k}: {v}' for k, v in rep.get('wiremap', {}).items())}")
        print(f"  Measurements  : {len(rep.get('measurement_tuples', []))} frequency/parameter tuples decoded")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch saved test reports from a Fluke DSP-4000 over serial (/dev/ttyUSB0) and export to JSON and PDF."
    )
    parser.add_argument("-d", "--device", default=DEFAULT_DEVICE, help=f"Serial port device (default: {DEFAULT_DEVICE})")
    parser.add_argument("-b", "--baud", type=int, default=DEFAULT_BAUD, help=f"Serial baud rate (default: {DEFAULT_BAUD})")
    parser.add_argument("-t", "--timeout", type=float, default=3.0, help="Serial read timeout in seconds (default: 3.0)")
    parser.add_argument("-n", "--index", type=int, default=None, help="Fetch only a specific report index (0-based).")
    parser.add_argument(
        "-o",
        "--output",
        default="DSP_Reports.json",
        help="Output JSON file path (default: DSP_Reports.json). If a .pdf path is given here, writes PDF to that path.",
    )
    parser.add_argument("-p", "--pdf", default=None, help="Output PDF file path (default: derived from -o, e.g. DSP_Reports.pdf).")
    parser.add_argument("--no-pdf", action="store_true", help="Disable PDF generation and only write JSON output.")
    parser.add_argument("--no-json", action="store_true", help="Disable JSON file output and only generate the PDF report.")
    parser.add_argument(
        "--include-summary",
        action="store_true",
        default=None,
        help="Include a summary overview page at the start of the PDF even when only 1 report is present.",
    )
    parser.add_argument(
        "--from-json",
        default=None,
        help="Load report data from an existing JSON file instead of querying the serial device.",
    )
    args = parser.parse_args()

    if args.from_json:
        in_path = Path(args.from_json)
        print(f"Loading saved report data from {in_path}...")
        data = json.loads(in_path.read_text(encoding="utf-8"))
    else:
        print(f"Opening {args.device} @ {args.baud} 8N1...")
        with PosixSerial(args.device, baudrate=args.baud, timeout=args.timeout) as ser:
            try:
                data = fetch_all_reports(ser, target_index=args.index)
            finally:
                close_session(ser, disable_dc=True)

    print_report_summary(data)

    out_arg = Path(args.output)
    if out_arg.suffix.lower() == ".pdf":
        pdf_path = Path(args.pdf) if args.pdf else out_arg
        json_path = out_arg.with_suffix(".json")
    else:
        json_path = out_arg
        pdf_path = Path(args.pdf) if args.pdf else out_arg.with_suffix(".pdf")

    if not args.no_json and not args.from_json:
        json_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"\n[+] Saved JSON report data to {json_path}")

    if not args.no_pdf:
        if pdf_items := fetched_data_to_pdf_items(data, include_summary=args.include_summary):
            generate_pdf(pdf_items, pdf_path)
            n_detail = sum(isinstance(x, FlukeReport) for x in pdf_items)
            n_summary = sum(isinstance(x, FlukeSummaryReport) for x in pdf_items)
            print(
                f"[+] Generated PDF report ({n_summary} summary page(s), {n_detail} certification report(s)): {pdf_path}"
            )
        else:
            print("\n[!] No reports available to render into PDF.")


if __name__ == "__main__":
    main()
