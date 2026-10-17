#!/usr/bin/env python3
"""
Shared Report Models, Parsers, and Vector PDF 1.4 Generator for Fluke DSP-4000
==============================================================================

Requires Python 3.13+.

Provides shared data structures, parsers, and PDF rendering used by both:
- `fluke_print_to_pdf.py` (converts serial printer text streams to PDF)
- `fetch_reports.py` (downloads binary saved reports via serial protocol to JSON & PDF)

Disclaimer:
Fluke and DSP-4000 are trademarks of Fluke Corporation. This software is an
independent open-source tool and is not affiliated with or endorsed by Fluke Corporation.
"""

from contextlib import suppress
from dataclasses import dataclass, field
from itertools import batched
from pathlib import Path
import re
from typing import Any


# ---------------------------------------------------------------------------
# Shared Data Models & Type Aliases (PEP 695)
# ---------------------------------------------------------------------------

type RGB = tuple[float, float, float]
type ReportItem = FlukeReport | FlukeSummaryReport


@dataclass(slots=True)
class BasicPairRow:
    pair: str
    length_m: str = ""
    length_limit: str = ""
    prop_delay_ns: str = ""
    prop_delay_limit: str = ""
    delay_skew_ns: str = ""
    delay_skew_limit: str = ""
    resistance_ohms: str = ""
    resistance_limit: str = ""
    impedance_ohms: str = ""
    impedance_limit: str = ""
    impedance_anom: str = ""
    atten_res: str = ""
    atten_freq: str = ""
    atten_limit: str = ""


@dataclass(slots=True)
class FreqTestRow:
    pair: str
    main_margin_res: str = ""
    main_margin_freq: str = ""
    main_margin_limit: str = ""
    main_val_res: str = ""
    main_val_freq: str = ""
    main_val_limit: str = ""
    rem_margin_res: str = ""
    rem_margin_freq: str = ""
    rem_margin_limit: str = ""
    rem_val_res: str = ""
    rem_val_freq: str = ""
    rem_val_limit: str = ""


@dataclass(slots=True)
class FlukeReport:
    cable_id: str = ""
    test_summary: str = ""
    company: str = ""
    headroom: str = ""
    site: str = ""
    date_time: str = ""
    operator: str = ""
    test_standard: str = ""
    standards_version: str = ""
    cable_type: str = ""
    software_version: str = ""
    main_unit: str = ""
    nvp: str = ""
    fault_anomaly_threshold: str = ""
    remote_unit: str = ""
    shield_test: str = ""

    wire_map_status: str = ""
    wire_map_top_pins: list[str] = field(default_factory=list)
    wire_map_links: list[str] = field(default_factory=list)
    wire_map_bottom_pins: list[str] = field(default_factory=list)

    basic_pairs: list[BasicPairRow] = field(default_factory=list)
    freq_sections: dict[str, list[FreqTestRow]] = field(default_factory=dict)


@dataclass(slots=True)
class SummaryRow:
    cable_id: str
    date_time: str
    site: str
    length: str
    summary: str


@dataclass(slots=True)
class FlukeSummaryReport:
    company: str = ""
    length_unit: str = "m"
    rows: list[SummaryRow] = field(default_factory=list)
    total_length: str = ""


# ---------------------------------------------------------------------------
# Numeric & String Helpers
# ---------------------------------------------------------------------------

FREQ_SECTION_NAMES = frozenset(
    {
        "RETURN LOSS",
        "PSNEXT",
        "PSACR",
        "NEXT",
        "ACR",
        "ELFEXT",
        "PSELFEXT",
        "ACR-F",
        "PSACR-F",
        "ACR-N",
        "PSACR-N",
    }
)


def parse_numeric_value(val_str: str) -> float:
    """
    Parse a numeric dB value from strings like '34.6', '3,1 F', '-.3 F', '-41.7 F', or '30.5*'.
    Raises ValueError if not parseable.
    """
    cleaned = re.sub(r"[Ff*\s]+$", "", val_str.strip()).replace(",", ".")
    if not cleaned or cleaned == "-":
        raise ValueError("Empty value")
    return float(cleaned)


def format_margin(val: float, *ref_strs: str) -> str:
    """Format a signed margin value, preserving comma decimal separator if used in the input strings."""
    s = f"{val:+.1f}"
    return s.replace(".", ",") if any("," in r for r in ref_strs) else s


def has_fail_flag(val_str: str) -> bool:
    """Return True if a formatted measurement value carries an explicit 'F' fail marker."""
    return bool(re.search(r"(?:^|\s|\d)F$", val_str.strip()))


# ---------------------------------------------------------------------------
# Serial Printout Parser (Detail & Summary Text Streams)
# ---------------------------------------------------------------------------

def clean_control_codes(raw_text: str) -> str:
    """
    Strip printer escape sequences such as ESC @ (\\x1b@), literal E@, or leading @
    at the start of a printout/page.
    """
    text = raw_text.replace("\x1b@", "")
    return re.sub(r"(^|\x0c)(?:E@|@)(?=\s*Cable ID:|\s{2,}\S)", r"\1", text)


def is_summary_chunk(raw_chunk: str) -> bool:
    """
    Detect whether a text chunk is a Summary printout rather than
    a full single-cable certification report.
    """
    return bool(
        re.search(
            r"Cable ID:\s+Date\s*/\s*Time:\s+SITE:\s+Length\([^)]+\)\s+Summary:",
            raw_chunk,
            re.IGNORECASE,
        )
    )


def parse_summary_report(raw_chunk: str) -> FlukeSummaryReport | None:
    lines = [line.rstrip() for line in re.split(r"\r\n|\r|\n", raw_chunk)]

    summary_rep = FlukeSummaryReport()
    in_table = False

    row_regex = re.compile(
        r"^(?P<cable_id>\S(?:.*?\S)?)\s{2,}"
        r"(?P<date_time>\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}:\d{2}\s*(?:[ap]m|[AP]M)?)\s+"
        r"(?:(?P<site>\S(?:.*?\S)?)\s{2,})?"
        r"(?P<length>-?\d+(?:[.,]\d+)?)\s+"
        r"(?P<summary>\*?[A-Za-z]+\*?)\s*$"
    )

    for line in lines:
        if not (stripped := re.sub(r"^(?:E@|@)\s*", "", line.strip())):
            continue

        if m_hdr := re.search(
            r"Cable ID:\s+Date\s*/\s*Time:\s+SITE:\s+Length\((?P<unit>[^)]+)\)\s+Summary:",
            stripped,
            re.IGNORECASE,
        ):
            summary_rep.length_unit = m_hdr.group("unit").strip()
            in_table = True
            continue

        if not in_table:
            if not summary_rep.company and "Total Length:" not in stripped:
                summary_rep.company = stripped
            continue

        if m_total := re.search(r"Total Length:\s*(-?\d+(?:[.,]\d+)?)", stripped, re.IGNORECASE):
            summary_rep.total_length = m_total.group(1).strip()
            continue

        if m_row := row_regex.match(stripped):
            summary_rep.rows.append(
                SummaryRow(
                    cable_id=m_row.group("cable_id").strip(),
                    date_time=re.sub(r"\s+", " ", m_row.group("date_time").strip()),
                    site=(m_row.group("site") or "").strip(),
                    length=m_row.group("length").strip(),
                    summary=m_row.group("summary").strip(),
                )
            )
            continue

        parts = re.split(r"\s{2,}", stripped)
        if len(parts) >= 4 and parts[-1].upper().strip("*") in ("PASS", "FAIL"):
            summary_rep.rows.append(
                SummaryRow(
                    cable_id=parts[0],
                    date_time=" ".join(parts[1:-2]) if len(parts) == 4 else parts[1],
                    site=parts[2] if len(parts) >= 5 else "",
                    length=parts[-2],
                    summary=parts[-1],
                )
            )

    if not summary_rep.rows and not summary_rep.total_length:
        return None

    if not summary_rep.total_length and summary_rep.rows:
        with suppress(ValueError):
            summary_rep.total_length = f"{sum(parse_numeric_value(r.length) for r in summary_rep.rows):.1f}"

    return summary_rep


def split_two_columns(line: str) -> tuple[str, str]:
    """
    Header lines have a left column and a right column separated by >=3 spaces
    (typically aligned around column 48).
    """
    if len(line) >= 48 and not line[45:48].strip():
        return line[:48].strip(), line[48:].strip()
    match re.split(r"\s{3,}", line.strip(), maxsplit=1):
        case [left, right]:
            return left.strip(), right.strip()
        case _:
            return line.strip(), ""


def _parse_val_freq_limit(val_str: str) -> tuple[str, str, str]:
    """
    Parse a cell containing 'Result [F|*] Freq Limit', e.g.:
      '34.6   100.0   0.0'  -> ('34.6', '100.0', '0.0')
      '-.3 F  10.0  19.0'   -> ('-.3 F', '10.0', '19.0')
      '30.5*  86.8  30.4'   -> ('30.5*', '86.8', '30.4')
    """
    match val_str.split():
        case [res, ("F" | "*") as flag, freq, lim, *_]:
            return f"{res} {flag}", freq, lim
        case [res, freq, lim, *_]:
            return res, freq, lim
        case _:
            return "-", "-", "-"


def parse_single_report(raw_chunk: str) -> FlukeReport | None:
    lines = [line.rstrip() for line in re.split(r"\r\n|\r|\n", raw_chunk)]
    if not any("Cable ID:" in l or "Wire Map" in l for l in lines):
        return None

    report = FlukeReport()
    mode = "HEADER"
    current_freq_section: str | None = None

    HEADER_FIELD_MAP = {
        "Cable ID:": "cable_id",
        "Test Summary:": "test_summary",
        "HEADROOM:": "headroom",
        "SITE:": "site",
        "Date / Time:": "date_time",
        "OPERATOR:": "operator",
        "Test Standard:": "test_standard",
        "Standards Version:": "standards_version",
        "Cable Type:": "cable_type",
        "Software Version:": "software_version",
        "SHIELD TEST:": "shield_test",
    }

    for line in lines:
        if not (stripped := line.strip()):
            continue

        if stripped.startswith("Wire Map"):
            mode = "WIREMAP"
            if m := re.match(r"Wire Map\s+(\S+)\s+Result\s+RJ45 PIN:\s+(.*)", stripped):
                report.wire_map_status = m.group(1).strip()
                report.wire_map_top_pins = m.group(2).split()
            elif len(parts := stripped.split()) >= 3:
                report.wire_map_status = parts[2]
            continue

        if mode == "WIREMAP":
            if stripped.startswith(("|Length", "Pair |")):
                mode = "BASIC_TABLE"
            elif m := re.search(r"RJ45 PIN:\s+(.*)", stripped):
                report.wire_map_bottom_pins = m.group(1).split()
                continue
            elif set(stripped).issubset({"|", " ", "X", "-", "?", ".", "o", "O", "s", "S"}):
                report.wire_map_links = stripped.split()
                continue

        if stripped.startswith("|Length") or (mode == "BASIC_TABLE" and stripped.startswith("Pair |")):
            mode = "BASIC_TABLE"
            continue

        if "Main Results" in stripped and "Remote Results" in stripped:
            mode = "FREQ_TABLE"
            continue

        match mode:
            case "HEADER":
                for col in split_two_columns(line):
                    if not col:
                        continue
                    matched_prefix = next((p for p in HEADER_FIELD_MAP if col.startswith(p)), None)
                    if matched_prefix:
                        setattr(report, HEADER_FIELD_MAP[matched_prefix], col.partition(":")[2].strip())
                    elif col.startswith("FLUKE "):
                        if not report.main_unit:
                            report.main_unit = col
                        else:
                            report.remote_unit = col
                    elif col.startswith("NVP:"):
                        if m_nvp := re.search(r"NVP:\s*(\S+)", col):
                            report.nvp = m_nvp.group(1)
                        if m_fat := re.search(r"FAULT ANOMALY THRESHOLD:\s*(\S+)", col):
                            report.fault_anomaly_threshold = m_fat.group(1)
                    elif not report.company and ":" not in col:
                        report.company = col

            case "BASIC_TABLE" if "|" in stripped:
                cells = [c.strip() for c in stripped.split("|")]
                if cells[0] and cells[0][0].isdigit():
                    def get_pair_tokens(idx: int) -> list[str]:
                        return cells[idx].split() if idx < len(cells) and cells[idx] else []

                    len_v, pd_v, sk_v, res_v, imp_v = (get_pair_tokens(k) for k in range(1, 6))
                    att_res, att_freq, att_lim = _parse_val_freq_limit(cells[6] if len(cells) > 6 else "")
                    report.basic_pairs.append(
                        BasicPairRow(
                            pair=cells[0],
                            length_m=len_v[0] if len(len_v) >= 1 else "-",
                            length_limit=len_v[1] if len(len_v) >= 2 else "-",
                            prop_delay_ns=pd_v[0] if len(pd_v) >= 1 else "-",
                            prop_delay_limit=pd_v[1] if len(pd_v) >= 2 else "-",
                            delay_skew_ns=sk_v[0] if len(sk_v) >= 1 else "-",
                            delay_skew_limit=sk_v[1] if len(sk_v) >= 2 else "-",
                            resistance_ohms=res_v[0] if len(res_v) >= 1 else "-",
                            resistance_limit=res_v[1] if len(res_v) >= 2 else "-",
                            impedance_ohms=imp_v[0] if len(imp_v) >= 1 else "-",
                            impedance_limit=imp_v[1] if len(imp_v) >= 2 else "-",
                            impedance_anom=imp_v[2] if len(imp_v) >= 3 else "-",
                            atten_res=att_res,
                            atten_freq=att_freq,
                            atten_limit=att_lim,
                        )
                    )

            case "FREQ_TABLE" if "|" in stripped:
                cells = [c.strip() for c in stripped.split("|")]
                first_col = cells[0]
                base_sec = re.sub(r"\s+(?:FAIL|PASS|\*+)$", "", first_col)
                if base_sec in FREQ_SECTION_NAMES:
                    current_freq_section = first_col
                    report.freq_sections.setdefault(current_freq_section, [])
                elif first_col and first_col[0].isdigit() and current_freq_section:
                    mm, mv, rm, rv = (
                        _parse_val_freq_limit(cells[k] if len(cells) > k else "")
                        for k in range(1, 5)
                    )
                    report.freq_sections[current_freq_section].append(
                        FreqTestRow(
                            pair=first_col,
                            main_margin_res=mm[0],
                            main_margin_freq=mm[1],
                            main_margin_limit=mm[2],
                            main_val_res=mv[0],
                            main_val_freq=mv[1],
                            main_val_limit=mv[2],
                            rem_margin_res=rm[0],
                            rem_margin_freq=rm[1],
                            rem_margin_limit=rm[2],
                            rem_val_res=rv[0],
                            rem_val_freq=rv[1],
                            rem_val_limit=rv[2],
                        )
                    )

    return report


def parse_fluke_stream(raw_text: str) -> list[ReportItem]:
    """
    Automatically detects and parses any combination of Summary Reports and Full
    Certification Reports from a serial printer text stream.
    """
    items: list[ReportItem] = []

    for chunk in clean_control_codes(raw_text).split("\x0c"):
        if not chunk.strip():
            continue
        if is_summary_chunk(chunk):
            if sum_rep := parse_summary_report(chunk):
                if items and isinstance(items[-1], FlukeSummaryReport):
                    prev_sum = items[-1]
                    prev_sum.rows.extend(sum_rep.rows)
                    if sum_rep.total_length:
                        prev_sum.total_length = sum_rep.total_length
                    if not prev_sum.company and sum_rep.company:
                        prev_sum.company = sum_rep.company
                else:
                    items.append(sum_rep)
        elif rep := parse_single_report(chunk):
            items.append(rep)

    return items


# ---------------------------------------------------------------------------
# Converter for Binary Fetched Reports (from `fetch_reports.py`) -> PDF Models
# ---------------------------------------------------------------------------

PAIRS_4 = ("12", "36", "45", "78")
PAIRS_6 = ("12-36", "12-45", "12-78", "36-45", "36-78", "45-78")
PAIRS_12 = (
    "12-36",
    "12-45",
    "12-78",
    "36-12",
    "36-45",
    "36-78",
    "45-12",
    "45-36",
    "45-78",
    "78-12",
    "78-36",
    "78-45",
)


def _fmt_tuple(t: dict[str, Any], mark_fail: bool = False) -> tuple[str, str, str]:
    val = t.get("value")
    lim = t.get("limit")
    freq = t.get("frequency_mhz")
    passed = t.get("passed", True)

    if val is None:
        res_s = "-"
    else:
        res_s = f"{float(val):.1f}{' F' if mark_fail and not passed else ''}"

    freq_s = f"{float(freq):.1f}" if freq is not None else "-"
    lim_s = f"{float(lim):.1f}" if lim is not None else "-"
    return res_s, freq_s, lim_s


def fetched_report_to_fluke_report(
    rep: dict[str, Any], device_info: dict[str, Any] | None = None
) -> FlukeReport:
    """
    Convert a single decoded report dictionary from `fetch_reports.py` into a
    `FlukeReport` object for PDF rendering.
    """
    dev = device_info or {}
    frep = FlukeReport(
        cable_id=str(rep.get("cable_id", "")),
        test_summary=str(rep.get("result", "")),
        company=str(dev.get("company_name", "")),
        site=str(dev.get("client_name", "")),
        operator=str(dev.get("operator", "")),
        date_time=str(rep.get("date_time") or f"Report #{rep.get('index', 0)}"),
        test_standard=str(rep.get("test_standard", "")),
        standards_version=str(dev.get("standards_version", "")),
        cable_type=str(rep.get("cable_type", "")),
        software_version=str(dev.get("firmware_version", "")),
        shield_test=str(rep.get("shield_test", "N/A")),
    )

    main_sn = str(dev.get("serial_number", "")).lstrip(">").strip()
    main_lia = str(rep.get("main_adapter", "LIA011"))
    rem_sn = str(rep.get("remote_serial_number") or main_sn)
    rem_lia = str(rep.get("remote_adapter", "LIA011"))

    if main_sn:
        frep.main_unit = f"FLUKE DSP-4000 S/N: {main_sn} {main_lia}".strip()
    if rem_sn:
        frep.remote_unit = f"FLUKE DSP-4000SR S/N: {rem_sn} {rem_lia}".strip()

    if (nvp_val := rep.get("nvp_percent")) is not None:
        frep.nvp = f"{float(nvp_val):.1f}%"
    fat_val = rep.get("fault_anomaly_threshold_percent", 15)
    frep.fault_anomaly_threshold = f"{fat_val}%" if fat_val is not None else "15%"

    # Wiremap decoding
    frep.wire_map_top_pins = ["1", "2", "3", "4", "5", "6", "7", "8", "S"]
    wm_dict: dict[str, str] = rep.get("wiremap", {})
    pin_keys = [*(f"Pin {p}" for p in range(1, 9)), "Shield"]
    wm_ok = True

    for idx, (pk, top_label) in enumerate(zip(pin_keys, frep.wire_map_top_pins)):
        match wm_dict.get(pk, "").split():
            case ["Open"]:
                frep.wire_map_links.append("o")
                frep.wire_map_bottom_pins.append("?")
                if idx < 8:
                    wm_ok = False
            case ["Shield"]:
                frep.wire_map_links.append("|" if top_label == "S" else "X")
                frep.wire_map_bottom_pins.append("S")
            case ["Pin", pin_num]:
                is_match = pin_num == top_label
                frep.wire_map_links.append("|" if is_match else "X")
                frep.wire_map_bottom_pins.append(pin_num)
                if not is_match:
                    wm_ok = False
            case _:
                frep.wire_map_links.append("|")
                frep.wire_map_bottom_pins.append(top_label)

    frep.wire_map_status = str(rep.get("wiremap_status") or ("PASS" if wm_ok else "FAIL"))

    # Basic Pair Table & Frequency Sections from the 168 measurement tuples
    tuples: list[dict[str, Any]] = rep.get("measurement_tuples", [])
    pair_metrics: dict[str, dict[str, Any]] = rep.get("pair_metrics_raw", {})
    limits_info: dict[str, Any] = rep.get("cable_limits", {})

    len_limit_s = f"{float(limits_info.get('length_limit_m', 90.0)):.1f}"
    delay_limit_s = str(limits_info.get("prop_delay_limit_ns", 498))
    skew_limit_s = str(limits_info.get("delay_skew_limit_ns", 44))

    for i, (p_label, p_raw_key) in enumerate(zip(PAIRS_4, ("1,2", "3,6", "4,5", "7,8"))):
        pm = pair_metrics.get(p_raw_key, {})
        if "length_m" in pm:
            len_m_s = f"{float(pm['length_m']):.1f}"
        else:
            w3e = int(pm.get("word_0x3e", 0) or 0)
            len_m_s = f"{(w3e & 0x07FF) / 10.0:.1f}"

        w46 = pm.get("delay_ns", pm.get("word_0x46"))
        delay_s = "0" if w46 is None else str(int(w46) & 0x3FFF)

        w4e = pm.get("skew_ns", pm.get("word_0x4e"))
        skew_s = "0" if w4e is None else str(int(w4e) & 0x3FFF)

        att_res, att_freq, att_lim = "-", "-", "-"
        if len(tuples) >= 20:
            t_margin = tuples[12 + i]
            t_val = tuples[16 + i]
            att_res, att_freq, att_lim = (
                _fmt_tuple(t_margin, mark_fail=True)
                if not t_margin.get("passed", True)
                else _fmt_tuple(t_val, mark_fail=False)
            )

        frep.basic_pairs.append(
            BasicPairRow(
                pair=p_label,
                length_m=len_m_s,
                length_limit=len_limit_s,
                prop_delay_ns=delay_s,
                prop_delay_limit=delay_limit_s,
                delay_skew_ns=skew_s,
                delay_skew_limit=skew_limit_s,
                resistance_ohms="N/A",
                resistance_limit="-",
                impedance_ohms="N/A",
                impedance_limit="-",
                impedance_anom="-",
                atten_res=att_res,
                atten_freq=att_freq,
                atten_limit=att_lim,
            )
        )

    if len(tuples) >= 168:
        section_specs = (
            ("RETURN LOSS", PAIRS_4, 20, 24, 64, 68),
            ("PSNEXT", PAIRS_4, 72, 76, 80, 84),
            ("PSACR", PAIRS_4, 88, 92, 96, 100),
            ("NEXT", PAIRS_6, 0, 6, 40, 46),
            ("ACR", PAIRS_6, 28, 34, 52, 58),
            ("ELFEXT", PAIRS_12, 104, 116, 128, 140),
            ("PSELFEXT", PAIRS_4, 152, 156, 160, 164),
        )

        worst_next_margin: tuple[float, str, str] | None = None

        for sec_base, pair_list, mm_off, mv_off, rm_off, rv_off in section_specs:
            sec_failed = False
            rows: list[FreqTestRow] = []

            for i, p_name in enumerate(pair_list):
                t_mm, t_mv, t_rm, t_rv = (
                    tuples[mm_off + i],
                    tuples[mv_off + i],
                    tuples[rm_off + i],
                    tuples[rv_off + i],
                )

                if not all(t.get("passed", True) for t in (t_mm, t_mv, t_rm, t_rv)):
                    sec_failed = True

                if sec_base in ("NEXT", "PSNEXT"):
                    for t_cand in (t_mm, t_rm):
                        if (v := t_cand.get("value")) is not None and (l := t_cand.get("limit")) is not None:
                            m_db = round(float(v) - float(l), 1)
                            if worst_next_margin is None or m_db < worst_next_margin[0]:
                                worst_next_margin = (m_db, sec_base, p_name)

                mm_s = _fmt_tuple(t_mm, mark_fail=True)
                mv_s = _fmt_tuple(t_mv, mark_fail=False)
                rm_s = _fmt_tuple(t_rm, mark_fail=True)
                rv_s = _fmt_tuple(t_rv, mark_fail=False)

                rows.append(
                    FreqTestRow(
                        pair=p_name,
                        main_margin_res=mm_s[0],
                        main_margin_freq=mm_s[1],
                        main_margin_limit=mm_s[2],
                        main_val_res=mv_s[0],
                        main_val_freq=mv_s[1],
                        main_val_limit=mv_s[2],
                        rem_margin_res=rm_s[0],
                        rem_margin_freq=rm_s[1],
                        rem_margin_limit=rm_s[2],
                        rem_val_res=rv_s[0],
                        rem_val_freq=rv_s[1],
                        rem_val_limit=rv_s[2],
                    )
                )

            sec_title = f"{sec_base} FAIL" if sec_failed else sec_base
            frep.freq_sections[sec_title] = rows

        if not frep.headroom and worst_next_margin is not None:
            frep.headroom = f"{worst_next_margin[0]:.1f} dB ({worst_next_margin[1]} {worst_next_margin[2]})"

    return frep


def fetched_data_to_pdf_items(
    data: dict[str, Any], include_summary: bool | None = None
) -> list[ReportItem]:
    """
    Convert the full dictionary produced by `fetch_reports.py` into a list of
    `ReportItem` objects (`FlukeSummaryReport` and/or `FlukeReport` pages).
    """
    device_info = data.get("device", {})
    raw_reports = data.get("reports", [])
    raw_summaries = data.get("summaries", [])

    detail_reports = [fetched_report_to_fluke_report(r, device_info) for r in raw_reports]

    if include_summary is None:
        include_summary = len(raw_summaries) > 1 or (bool(raw_summaries) and not detail_reports)

    items: list[ReportItem] = []
    if include_summary and (raw_summaries or detail_reports):
        sum_rep = FlukeSummaryReport(
            company=str(device_info.get("company_name", "")),
            length_unit="m",
        )
        site_name = str(device_info.get("client_name", ""))
        total_len = 0.0

        rep_by_idx = {r.get("index", idx): det for idx, (r, det) in enumerate(zip(raw_reports, detail_reports))}
        source_list = raw_summaries or [
            {"index": r.get("index", i), "cable_id": r.get("cable_id", ""), "result": r.get("result", "")}
            for i, r in enumerate(raw_reports)
        ]

        for s in source_list:
            idx = s.get("index", 0)
            det = rep_by_idx.get(idx)
            cable_len_s = "0.0"
            date_s = f"Report #{idx}"
            if det and det.basic_pairs:
                cable_len_s = det.basic_pairs[0].length_m or "0.0"
                date_s = det.date_time or date_s
            with suppress(ValueError):
                total_len += float(cable_len_s)

            sum_rep.rows.append(
                SummaryRow(
                    cable_id=str(s.get("cable_id", "")),
                    date_time=date_s,
                    site=site_name,
                    length=cable_len_s,
                    summary=str(s.get("result", "")),
                )
            )

        sum_rep.total_length = f"{total_len:.1f}"
        items.append(sum_rep)

    items.extend(detail_reports)
    return items


# ---------------------------------------------------------------------------
# Pure-Python Vector PDF 1.4 Generator (Zero External Dependencies)
# ---------------------------------------------------------------------------

def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class VectorPDFDocument:
    """
    Lightweight, self-contained PDF 1.4 builder supporting multiple pages,
    standard PostScript Type1 fonts (Helvetica, Helvetica-Bold, Courier, Courier-Bold),
    vector rectangles, lines, badges, and aligned text.
    """

    PAGE_W = 595.28  # A4 width in pt
    PAGE_H = 841.89  # A4 height in pt

    def __init__(self) -> None:
        self.pages: list[list[str]] = []
        self._cur: list[str] = []

    def new_page(self) -> None:
        self._cur = []
        self.pages.append(self._cur)

    def _y(self, y_top: float) -> float:
        return self.PAGE_H - y_top

    def set_fill_rgb(self, r: float, g: float, b: float) -> None:
        self._cur.append(f"{r:.3f} {g:.3f} {b:.3f} rg")

    def set_stroke_rgb(self, r: float, g: float, b: float) -> None:
        self._cur.append(f"{r:.3f} {g:.3f} {b:.3f} RG")

    def set_line_width(self, w: float) -> None:
        self._cur.append(f"{w:.2f} w")

    def rect(
        self,
        x: float,
        y_top: float,
        w: float,
        h: float,
        fill: RGB | None = None,
        stroke: RGB | None = None,
        line_width: float = 0.5,
    ) -> None:
        if fill is None and stroke is None:
            return
        self._cur.append("q")
        if fill is not None:
            self.set_fill_rgb(*fill)
        if stroke is not None:
            self.set_stroke_rgb(*stroke)
            self.set_line_width(line_width)
        y_bl = self._y(y_top + h)
        op = "B" if (fill and stroke) else ("f" if fill else "S")
        self._cur.append(f"{x:.2f} {y_bl:.2f} {w:.2f} {h:.2f} re {op}")
        self._cur.append("Q")

    def line(
        self,
        x1: float,
        y1_top: float,
        x2: float,
        y2_top: float,
        stroke: RGB = (0.7, 0.7, 0.7),
        line_width: float = 0.5,
    ) -> None:
        self._cur.append("q")
        self.set_stroke_rgb(*stroke)
        self.set_line_width(line_width)
        self._cur.append(f"{x1:.2f} {self._y(y1_top):.2f} m {x2:.2f} {self._y(y2_top):.2f} l S")
        self._cur.append("Q")

    def text(
        self,
        x: float,
        y_top: float,
        text_str: str,
        font: str = "F1",
        size: float = 8.0,
        color: RGB = (0.1, 0.1, 0.1),
        align: str = "left",
    ) -> None:
        if not text_str:
            return
        match font:
            case "F3" | "F4":  # Courier
                char_w = size * 0.60
            case "F2":  # Helvetica-Bold
                char_w = size * 0.54
            case _:  # Helvetica
                char_w = size * 0.50

        est_w = len(text_str) * char_w
        match align:
            case "center":
                draw_x = x - est_w / 2.0
            case "right":
                draw_x = x - est_w
            case _:
                draw_x = x

        y_bl = self._y(float(y_top))
        self._cur.append("q")
        self.set_fill_rgb(*color)
        self._cur.append(
            f"BT /{font} {size:.2f} Tf {draw_x:.2f} {y_bl:.2f} Td ({_pdf_escape(text_str)}) Tj ET"
        )
        self._cur.append("Q")

    def to_bytes(self) -> bytes:
        """Compile all pages and font resources into a valid PDF 1.4 binary."""
        objects: list[bytes] = []

        def add_obj(data: bytes) -> int:
            objects.append(data)
            return len(objects)

        f1_id = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        f2_id = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        f3_id = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")
        f4_id = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold /Encoding /WinAnsiEncoding >>")

        font_dict_str = f"<< /F1 {f1_id} 0 R /F2 {f2_id} 0 R /F3 {f3_id} 0 R /F4 {f4_id} 0 R >>"
        pages_id = add_obj(b"")
        page_ids: list[int] = []

        for page_cmds in self.pages:
            stream_bytes = "\n".join(page_cmds).encode("latin-1", errors="replace")
            content_id = add_obj(
                f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii")
                + stream_bytes
                + b"\nendstream"
            )
            page_ids.append(
                add_obj(
                    (
                        f"<< /Type /Page /Parent {pages_id} 0 R "
                        f"/MediaBox [0 0 {self.PAGE_W:.2f} {self.PAGE_H:.2f}] "
                        f"/Resources << /Font {font_dict_str} >> "
                        f"/Contents {content_id} 0 R >>"
                    ).encode("ascii")
                )
            )

        kids_str = " ".join(f"{pid} 0 R" for pid in page_ids)
        objects[pages_id - 1] = f"<< /Type /Pages /Kids [{kids_str}] /Count {len(page_ids)} >>".encode("ascii")
        catalog_id = add_obj(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii"))

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: list[int] = []
        for idx, obj_bytes in enumerate(objects, start=1):
            offsets.append(len(out))
            out.extend(f"{idx} 0 obj\n".encode("ascii"))
            out.extend(obj_bytes)
            out.extend(b"\nendobj\n")

        xref_pos = len(out)
        out.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
        for off in offsets:
            out.extend(f"{off:010d} 00000 n \n".encode("ascii"))

        out.extend(
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n".encode("ascii")
        )
        return bytes(out)


# ---------------------------------------------------------------------------
# Report Layout & Rendering
# ---------------------------------------------------------------------------

C_NAVY: RGB = (0.11, 0.16, 0.24)
C_GOLD: RGB = (0.95, 0.73, 0.08)
C_SLATE_HDR: RGB = (0.20, 0.27, 0.37)
C_SUB_HDR: RGB = (0.88, 0.91, 0.95)
C_ROW_ALT: RGB = (0.96, 0.97, 0.99)
C_WHITE: RGB = (1.0, 1.0, 1.0)
C_BORDER: RGB = (0.78, 0.82, 0.87)
C_TEXT: RGB = (0.12, 0.15, 0.20)
C_MUTED: RGB = (0.42, 0.47, 0.53)
C_PASS_BG: RGB = (0.13, 0.58, 0.33)
C_FAIL_BG: RGB = (0.80, 0.18, 0.18)
C_PASS_LIGHT: RGB = (0.90, 0.97, 0.92)
C_FAIL_LIGHT: RGB = (0.99, 0.91, 0.91)


def render_detail_report_page(pdf: VectorPDFDocument, rep: FlukeReport) -> None:
    pdf.new_page()
    margin_x = 24.0
    content_w = pdf.PAGE_W - 2 * margin_x
    y = 22.0

    # 1. Top Banner
    banner_h = 38.0
    pdf.rect(margin_x, y, content_w, banner_h, fill=C_NAVY)
    pdf.rect(margin_x, y, 6.0, banner_h, fill=C_GOLD)

    pdf.text(margin_x + 16.0, y + 15.0, "CABLE CERTIFICATION REPORT", font="F2", size=12.5, color=C_WHITE)
    subtitle = (
        f"Cable ID: {rep.cable_id or 'N/A'}    |    "
        f"Standard: {rep.test_standard or 'N/A'}    |    "
        f"Date: {rep.date_time or 'N/A'}"
    )
    pdf.text(margin_x + 16.0, y + 29.0, subtitle, font="F1", size=8.0, color=(0.82, 0.86, 0.92))

    # PASS / FAIL Badge inside banner
    is_pass = "PASS" in rep.test_summary.upper() and "FAIL" not in rep.test_summary.upper()
    badge_bg = C_PASS_BG if is_pass else C_FAIL_BG
    badge_w, badge_h = 88.0, 24.0
    badge_x = margin_x + content_w - badge_w - 8.0
    badge_y = y + (banner_h - badge_h) / 2.0
    pdf.rect(badge_x, badge_y, badge_w, badge_h, fill=badge_bg)
    pdf.text(
        badge_x + badge_w / 2.0,
        badge_y + 15.5,
        f"RESULT: {rep.test_summary or 'N/A'}",
        font="F2",
        size=9.5,
        color=C_WHITE,
        align="center",
    )

    y += banner_h + 6.0

    # 2. Metadata Grid (3 columns x 5 rows)
    meta_h = 62.0
    pdf.rect(margin_x, y, content_w, meta_h, fill=C_ROW_ALT, stroke=C_BORDER, line_width=0.6)

    col_splits = (0.0, 168.0, 344.0, content_w)
    pdf.line(margin_x + col_splits[1], y, margin_x + col_splits[1], y + meta_h, stroke=C_BORDER, line_width=0.5)
    pdf.line(margin_x + col_splits[2], y, margin_x + col_splits[2], y + meta_h, stroke=C_BORDER, line_width=0.5)

    columns = (
        (
            ("Cable ID:", rep.cable_id),
            ("Company:", rep.company),
            ("Site:", rep.site),
            ("Operator:", rep.operator),
            ("Date / Time:", rep.date_time),
        ),
        (
            ("Test Summary:", rep.test_summary),
            ("Headroom:", rep.headroom),
            ("Test Standard:", rep.test_standard),
            ("Cable Type:", rep.cable_type),
            ("Shield Test:", rep.shield_test),
        ),
        (
            ("Main Tester:", rep.main_unit),
            ("Remote Tester:", rep.remote_unit),
            ("SW / Std Ver:", f"{rep.software_version} / {rep.standards_version}".strip(" /")),
            ("NVP:", rep.nvp),
            ("Anomaly Thresh:", rep.fault_anomaly_threshold),
        ),
    )

    for c_idx, (items, val_offset) in enumerate(zip(columns, (54.0, 60.0, 62.0))):
        cx = margin_x + col_splits[c_idx] + 6.0
        for r_idx, (label, val) in enumerate(items):
            ry = y + 10.5 + r_idx * 11.5
            pdf.text(cx, ry, label, font="F2", size=6.8, color=C_MUTED)
            val_color = (C_PASS_BG if is_pass else C_FAIL_BG) if label == "Test Summary:" else C_TEXT
            val_font = "F2" if label in ("Cable ID:", "Test Summary:", "Headroom:") else "F1"
            pdf.text(cx + val_offset, ry, val or "-", font=val_font, size=6.8, color=val_color)

    y += meta_h + 6.0

    # 3. Wire Map bar
    wm_h = 34.0
    wm_pass = "PASS" in rep.wire_map_status.upper() and "FAIL" not in rep.wire_map_status.upper()
    pdf.rect(margin_x, y, content_w, wm_h, fill=C_WHITE, stroke=C_BORDER, line_width=0.6)
    pdf.rect(margin_x, y, 115.0, wm_h, fill=C_SLATE_HDR)
    pdf.text(margin_x + 8.0, y + 14.0, "WIRE MAP", font="F2", size=8.5, color=C_WHITE)
    pdf.rect(margin_x + 8.0, y + 18.0, 52.0, 11.5, fill=C_PASS_BG if wm_pass else C_FAIL_BG)
    pdf.text(
        margin_x + 34.0,
        y + 26.5,
        rep.wire_map_status or "N/A",
        font="F2",
        size=7.0,
        color=C_WHITE,
        align="center",
    )

    top_pins = rep.wire_map_top_pins or ["1", "2", "3", "4", "5", "6", "7", "8", "S"]
    bot_pins = rep.wire_map_bottom_pins or top_pins
    links = rep.wire_map_links or ["|"] * len(top_pins)

    pdf.text(margin_x + 125.0, y + 11.5, "MAIN RJ45 PIN:", font="F2", size=6.8, color=C_MUTED)
    pdf.text(margin_x + 125.0, y + 27.5, "REMOTE RJ45 PIN:", font="F2", size=6.8, color=C_MUTED)

    pin_start_x = margin_x + 215.0
    for idx, p_top in enumerate(top_pins):
        px = pin_start_x + idx * 34.0
        p_bot = bot_pins[idx] if idx < len(bot_pins) else p_top
        lnk = links[idx] if idx < len(links) else "|"
        ok_pin = lnk == "|" and p_top == p_bot
        pin_fill = C_PASS_LIGHT if ok_pin else C_FAIL_LIGHT
        pin_stroke = C_PASS_BG if ok_pin else C_FAIL_BG

        pdf.rect(px - 8.0, y + 3.5, 16.0, 10.0, fill=pin_fill, stroke=pin_stroke, line_width=0.6)
        pdf.text(px, y + 10.8, p_top, font="F2", size=7.0, color=C_TEXT, align="center")
        if ok_pin:
            pdf.line(px, y + 13.5, px, y + 20.5, stroke=pin_stroke, line_width=1.2)
        else:
            pdf.line(px, y + 13.5, px, y + 15.8, stroke=pin_stroke, line_width=1.0)
            pdf.line(px, y + 18.2, px, y + 20.5, stroke=pin_stroke, line_width=1.0)
        pdf.rect(px - 8.0, y + 20.5, 16.0, 10.0, fill=pin_fill, stroke=pin_stroke, line_width=0.6)
        pdf.text(px, y + 27.8, p_bot, font="F2", size=7.0, color=C_TEXT if ok_pin else C_FAIL_BG, align="center")

    y += wm_h + 6.0

    # 4. Basic Pair Parameters Table
    pdf.rect(margin_x, y, content_w, 13.0, fill=C_SLATE_HDR)
    pdf.text(
        margin_x + 6.0,
        y + 9.2,
        "CABLE PAIR PARAMETERS (LENGTH, DELAY, RESISTANCE, IMPEDANCE, ATTENUATION)",
        font="F2",
        size=7.5,
        color=C_WHITE,
    )
    y += 13.0

    b_cols = (
        ("Pair", 32.0),
        ("Len (m)", 38.0),
        ("Lim (m)", 38.0),
        ("Delay (ns)", 44.0),
        ("Lim (ns)", 42.0),
        ("Skew (ns)", 42.0),
        ("Lim (ns)", 40.0),
        ("Res (Ohm)", 46.0),
        ("Imp (Ohm)", 46.0),
        ("Atten (dB)", 46.0),
        ("Freq (MHz)", 45.0),
        ("Limit (dB)", 44.0),
        ("Margin (dB)", content_w - 503.0),
    )

    sub_h = 12.0
    pdf.rect(margin_x, y, content_w, sub_h, fill=C_SUB_HDR, stroke=C_BORDER, line_width=0.5)
    cx = margin_x
    for col_title, cw in b_cols:
        pdf.text(cx + cw / 2.0, y + 8.5, col_title, font="F2", size=6.3, color=C_TEXT, align="center")
        cx += cw
        pdf.line(cx, y, cx, y + sub_h, stroke=C_BORDER, line_width=0.4)
    y += sub_h

    row_h = 11.0
    for r_idx, row in enumerate(rep.basic_pairs):
        bg = C_ROW_ALT if r_idx % 2 == 1 else C_WHITE
        pdf.rect(margin_x, y, content_w, row_h, fill=bg, stroke=C_BORDER, line_width=0.4)

        margin_str = "-"
        att_ok = not has_fail_flag(row.atten_res)
        with suppress(ValueError):
            margin_val = parse_numeric_value(row.atten_limit) - parse_numeric_value(row.atten_res)
            margin_str = format_margin(margin_val, row.atten_limit, row.atten_res)
            if margin_val < 0:
                att_ok = False

        vals = (
            row.pair,
            row.length_m,
            row.length_limit,
            row.prop_delay_ns,
            row.prop_delay_limit,
            row.delay_skew_ns,
            row.delay_skew_limit,
            row.resistance_ohms if row.resistance_ohms != "-" else "N/A",
            row.impedance_ohms if row.impedance_ohms != "-" else "N/A",
            row.atten_res,
            row.atten_freq,
            row.atten_limit,
            margin_str,
        )
        cx = margin_x
        for c_i, ((_, cw), val) in enumerate(zip(b_cols, vals)):
            fnt = "F2" if c_i in (0, 12) else "F3"
            if c_i == 12 and margin_str != "-":
                clr = C_PASS_BG if att_ok else C_FAIL_BG
            elif c_i == 9 and not att_ok:
                clr = C_FAIL_BG
            else:
                clr = C_TEXT
            pdf.text(cx + cw / 2.0, y + 8.0, val, font=fnt, size=6.6, color=clr, align="center")
            cx += cw
            if c_i < len(b_cols) - 1:
                pdf.line(cx, y, cx, y + row_h, stroke=C_BORDER, line_width=0.3)
        y += row_h

    y += 6.0

    # 5. Frequency-Domain Test Results
    grp_h = 12.5
    pdf.rect(margin_x, y, content_w, grp_h, fill=C_SLATE_HDR)

    pair_col_w = 55.0
    quad_w = (content_w - pair_col_w) / 4.0
    pdf.text(margin_x + pair_col_w / 2.0, y + 8.8, "TEST / PAIR", font="F2", size=6.8, color=C_WHITE, align="center")

    quad_titles = (
        "MAIN - WORST MARGIN",
        "MAIN - WORST VALUE",
        "REMOTE - WORST MARGIN",
        "REMOTE - WORST VALUE",
    )
    for q_i, q_title in enumerate(quad_titles):
        qx = margin_x + pair_col_w + q_i * quad_w
        pdf.line(qx, y, qx, y + grp_h, stroke=C_WHITE, line_width=0.5)
        pdf.text(qx + quad_w / 2.0, y + 8.8, q_title, font="F2", size=6.8, color=C_WHITE, align="center")
    y += grp_h

    sub_h2 = 11.0
    pdf.rect(margin_x, y, content_w, sub_h2, fill=C_SUB_HDR, stroke=C_BORDER, line_width=0.5)
    pdf.text(margin_x + pair_col_w / 2.0, y + 8.0, "Pair", font="F2", size=6.2, color=C_TEXT, align="center")

    sub_cols = ("Result(dB)", "Freq(MHz)", "Limit(dB)", "Margin")
    sub_w = quad_w / 4.0
    for q_i in range(4):
        qx = margin_x + pair_col_w + q_i * quad_w
        pdf.line(qx, y, qx, y + sub_h2, stroke=C_BORDER, line_width=0.5)
        for s_i, s_title in enumerate(sub_cols):
            sx = qx + s_i * sub_w
            pdf.text(sx + sub_w / 2.0, y + 8.0, s_title, font="F2", size=5.8, color=C_TEXT, align="center")
            if s_i > 0:
                pdf.line(sx, y, sx, y + sub_h2, stroke=C_BORDER, line_width=0.3)
    y += sub_h2

    sec_hdr_h = 10.2
    freq_row_h = 9.4

    for sec_name, rows in rep.freq_sections.items():
        sec_failed = "FAIL" in sec_name.upper()
        sec_bg = C_FAIL_LIGHT if sec_failed else (0.82, 0.86, 0.92)
        sec_clr = C_FAIL_BG if sec_failed else C_NAVY
        pdf.rect(margin_x, y, content_w, sec_hdr_h, fill=sec_bg, stroke=C_BORDER, line_width=0.4)
        pdf.text(margin_x + 5.0, y + 7.4, sec_name, font="F2", size=6.6, color=sec_clr)
        y += sec_hdr_h

        for r_idx, frow in enumerate(rows):
            bg = C_ROW_ALT if r_idx % 2 == 1 else C_WHITE
            pdf.rect(margin_x, y, content_w, freq_row_h, fill=bg, stroke=C_BORDER, line_width=0.3)
            pdf.text(margin_x + pair_col_w / 2.0, y + 6.8, frow.pair, font="F2", size=6.3, color=C_TEXT, align="center")

            quad_data = (
                (frow.main_margin_res, frow.main_margin_freq, frow.main_margin_limit),
                (frow.main_val_res, frow.main_val_freq, frow.main_val_limit),
                (frow.rem_margin_res, frow.rem_margin_freq, frow.rem_margin_limit),
                (frow.rem_val_res, frow.rem_val_freq, frow.rem_val_limit),
            )

            for q_i, (res_s, freq_s, lim_s) in enumerate(quad_data):
                qx = margin_x + pair_col_w + q_i * quad_w
                pdf.line(qx, y, qx, y + freq_row_h, stroke=C_BORDER, line_width=0.5)

                m_str = "-"
                m_ok = not has_fail_flag(res_s)
                with suppress(ValueError):
                    m_val = parse_numeric_value(res_s) - parse_numeric_value(lim_s)
                    m_str = format_margin(m_val, res_s, lim_s)
                    if m_val < 0:
                        m_ok = False

                for s_i, cell_val in enumerate((res_s, freq_s, lim_s, m_str)):
                    sx = qx + s_i * sub_w
                    if s_i > 0:
                        pdf.line(sx, y, sx, y + freq_row_h, stroke=C_BORDER, line_width=0.25)
                    is_margin_col = s_i == 3
                    fnt = "F4" if is_margin_col else "F3"
                    if is_margin_col and m_str != "-":
                        clr = C_PASS_BG if m_ok else C_FAIL_BG
                    elif s_i == 0 and has_fail_flag(res_s):
                        clr = C_FAIL_BG
                    else:
                        clr = C_TEXT
                    pdf.text(sx + sub_w / 2.0, y + 6.8, cell_val, font=fnt, size=6.0, color=clr, align="center")

            y += freq_row_h


def render_summary_report_pages(pdf: VectorPDFDocument, sum_rep: FlukeSummaryReport) -> None:
    """
    Renders a Summary Report with KPI cards and a paginated table of all tested cables.
    """
    margin_x = 28.0
    content_w = pdf.PAGE_W - 2 * margin_x

    total_cables = len(sum_rep.rows)
    pass_count = sum("PASS" in r.summary.upper() and "FAIL" not in r.summary.upper() for r in sum_rep.rows)
    fail_count = total_cables - pass_count
    pass_rate = f"{(pass_count / total_cables * 100.0):.1f}%" if total_cables > 0 else "N/A"
    all_passed = fail_count == 0 and total_cables > 0

    sites = sorted({r.site for r in sum_rep.rows if r.site})
    site_summary = ", ".join(sites) if sites else "N/A"

    rows_first_page = 34
    rows_next_page = 40
    indexed_rows = list(enumerate(sum_rep.rows, start=1))

    if not indexed_rows:
        row_chunks: list[tuple[tuple[int, SummaryRow], ...]] = [()]
    else:
        row_chunks = [
            tuple(indexed_rows[:rows_first_page]),
            *batched(indexed_rows[rows_first_page:], rows_next_page),
        ]

    for chunk_idx, chunk_rows in enumerate(row_chunks):
        pdf.new_page()
        y = 26.0

        # 1. Top Banner
        banner_h = 42.0
        pdf.rect(margin_x, y, content_w, banner_h, fill=C_NAVY)
        pdf.rect(margin_x, y, 6.0, banner_h, fill=C_GOLD)

        pdf.text(margin_x + 16.0, y + 16.5, "CABLE TEST SUMMARY REPORT", font="F2", size=13.0, color=C_WHITE)
        sub_text = (
            f"Organization: {sum_rep.company or 'N/A'}    |    "
            f"Site: {site_summary}    |    "
            f"Total Cables: {total_cables}"
        )
        pdf.text(margin_x + 16.0, y + 31.5, sub_text, font="F1", size=8.2, color=(0.82, 0.86, 0.92))

        badge_bg = C_PASS_BG if all_passed else C_FAIL_BG
        badge_label = "ALL PASS" if all_passed else f"{fail_count} FAILED"
        badge_w, badge_h = 96.0, 24.0
        badge_x = margin_x + content_w - badge_w - 10.0
        badge_y = y + (banner_h - badge_h) / 2.0
        pdf.rect(badge_x, badge_y, badge_w, badge_h, fill=badge_bg)
        pdf.text(
            badge_x + badge_w / 2.0,
            badge_y + 15.5,
            badge_label,
            font="F2",
            size=9.5,
            color=C_WHITE,
            align="center",
        )

        y += banner_h + 10.0

        # 2. Summary KPI Cards (on first page of the summary)
        if chunk_idx == 0:
            kpi_h = 44.0
            kpis = (
                ("ORGANIZATION", sum_rep.company or "N/A", C_NAVY),
                ("TOTAL TESTED", str(total_cables), C_NAVY),
                ("PASSED", f"{pass_count} ({pass_rate})", C_PASS_BG),
                ("FAILED", str(fail_count), C_FAIL_BG if fail_count > 0 else C_MUTED),
                ("TOTAL LENGTH", f"{sum_rep.total_length or '0.0'} {sum_rep.length_unit}", C_NAVY),
            )
            gap = 8.0
            card_w = (content_w - gap * (len(kpis) - 1)) / len(kpis)
            for k_i, (k_label, k_val, k_color) in enumerate(kpis):
                kx = margin_x + k_i * (card_w + gap)
                pdf.rect(kx, y, card_w, kpi_h, fill=C_ROW_ALT, stroke=C_BORDER, line_width=0.6)
                pdf.rect(kx, y, card_w, 3.0, fill=k_color)
                pdf.text(kx + card_w / 2.0, y + 15.0, k_label, font="F2", size=6.5, color=C_MUTED, align="center")
                val_size = 9.5 if len(k_val) <= 14 else 8.0
                pdf.text(kx + card_w / 2.0, y + 31.5, k_val, font="F2", size=val_size, color=k_color, align="center")

            y += kpi_h + 12.0

        # 3. Summary Table Header
        cols = (
            ("#", 32.0, "center"),
            ("Cable ID", 115.0, "left"),
            ("Date / Time", 130.0, "center"),
            ("Site", 125.0, "left"),
            (f"Length ({sum_rep.length_unit})", 68.0, "right"),
            ("Summary", content_w - 470.0, "center"),
        )

        def aligned_x(cx_pos: float, width: float, align_mode: str) -> float:
            match align_mode:
                case "left":
                    return cx_pos + 8.0
                case "right":
                    return cx_pos + width - 8.0
                case _:
                    return cx_pos + width / 2.0

        hdr_h = 18.0
        pdf.rect(margin_x, y, content_w, hdr_h, fill=C_SLATE_HDR)
        cx = margin_x
        for c_i, (c_title, cw, c_align) in enumerate(cols):
            pdf.text(aligned_x(cx, cw, c_align), y + 12.0, c_title.upper(), font="F2", size=7.5, color=C_WHITE, align=c_align)
            cx += cw
            if c_i < len(cols) - 1:
                pdf.line(cx, y, cx, y + hdr_h, stroke=(0.32, 0.40, 0.52), line_width=0.5)
        y += hdr_h

        # 4. Summary Table Rows
        row_h = 17.5
        for r_i, (row_num, srow) in enumerate(chunk_rows):
            row_pass = "PASS" in srow.summary.upper() and "FAIL" not in srow.summary.upper()
            bg = C_ROW_ALT if r_i % 2 == 1 else C_WHITE
            pdf.rect(margin_x, y, content_w, row_h, fill=bg, stroke=C_BORDER, line_width=0.4)

            row_cells = (
                (str(row_num), "F1", C_MUTED),
                (srow.cable_id, "F2", C_TEXT),
                (srow.date_time, "F3", C_TEXT),
                (srow.site or "-", "F1", C_TEXT),
                (srow.length, "F3", C_TEXT),
            )

            cx = margin_x
            for c_i, (cell_text, fnt, clr) in enumerate(row_cells):
                _, cw, c_align = cols[c_i]
                pdf.text(aligned_x(cx, cw, c_align), y + 11.8, cell_text, font=fnt, size=8.0, color=clr, align=c_align)
                cx += cw
                pdf.line(cx, y, cx, y + row_h, stroke=C_BORDER, line_width=0.35)

            _, last_cw, _ = cols[-1]
            pill_w, pill_h = 46.0, 12.0
            pill_x = cx + (last_cw - pill_w) / 2.0
            pill_y = y + (row_h - pill_h) / 2.0
            pdf.rect(pill_x, pill_y, pill_w, pill_h, fill=C_PASS_BG if row_pass else C_FAIL_BG)
            pdf.text(
                pill_x + pill_w / 2.0,
                pill_y + 8.6,
                srow.summary.upper(),
                font="F2",
                size=7.2,
                color=C_WHITE,
                align="center",
            )

            y += row_h

        # 5. Total Length Footer Row (on final page of the summary)
        if chunk_idx == len(row_chunks) - 1:
            tot_h = 19.0
            pdf.rect(margin_x, y, content_w, tot_h, fill=C_SUB_HDR, stroke=C_BORDER, line_width=0.6)
            span_w = sum(c[1] for c in cols[:4])
            len_cw = cols[4][1]
            pdf.text(margin_x + span_w - 8.0, y + 12.8, "TOTAL LENGTH:", font="F2", size=8.2, color=C_NAVY, align="right")
            pdf.line(margin_x + span_w, y, margin_x + span_w, y + tot_h, stroke=C_BORDER, line_width=0.5)
            pdf.text(
                margin_x + span_w + len_cw - 8.0,
                y + 12.8,
                f"{sum_rep.total_length or '0.0'} {sum_rep.length_unit}",
                font="F4",
                size=8.5,
                color=C_NAVY,
                align="right",
            )
            pdf.line(margin_x + span_w + len_cw, y, margin_x + span_w + len_cw, y + tot_h, stroke=C_BORDER, line_width=0.5)
            pdf.text(
                margin_x + span_w + len_cw + cols[5][1] / 2.0,
                y + 12.8,
                f"{pass_count}P / {fail_count}F",
                font="F2",
                size=7.8,
                color=C_PASS_BG if all_passed else C_FAIL_BG,
                align="center",
            )


def add_footers_to_all_pages(pdf: VectorPDFDocument) -> None:
    """Adds a consistent footer with page numbers across all generated pages."""
    total_pages = len(pdf.pages)
    margin_x = 24.0
    content_w = pdf.PAGE_W - 2 * margin_x
    footer_y = pdf.PAGE_H - 14.0

    for idx, page_cmds in enumerate(pdf.pages, start=1):
        pdf._cur = page_cmds
        pdf.line(margin_x, footer_y - 8.0, margin_x + content_w, footer_y - 8.0, stroke=C_BORDER, line_width=0.5)
        pdf.text(
            margin_x,
            footer_y,
            "Generated by scripts-fluke-dsp4000 (compatible with Fluke DSP-4000)",
            font="F1",
            size=6.5,
            color=C_MUTED,
        )
        pdf.text(
            margin_x + content_w,
            footer_y,
            f"Page {idx} of {total_pages}",
            font="F1",
            size=6.5,
            color=C_MUTED,
            align="right",
        )


def generate_pdf(items: list[ReportItem], output_path: str | Path) -> None:
    """Render a list of `FlukeSummaryReport` and/or `FlukeReport` items to a PDF file."""
    pdf = VectorPDFDocument()
    for item in items:
        match item:
            case FlukeSummaryReport():
                render_summary_report_pages(pdf, item)
            case FlukeReport():
                render_detail_report_page(pdf, item)
    add_footers_to_all_pages(pdf)
    Path(output_path).write_bytes(pdf.to_bytes())
