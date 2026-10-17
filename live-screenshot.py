#!/usr/bin/env python3
"""
Live Display Screenshot Capture Tool for Fluke DSP-4000 Cable Analyzers.

Requires Python 3.13+.

Connects to a Fluke DSP-4000 over serial (default: /dev/ttyUSB0 @ 115200 8N1),
requests the display buffer ("db\\ng"), verifies block checksums, decompresses
the RLE 1-bpp framebuffer (240x200), and writes a PNG image.
Supports one-shot capture or continuous refresh at a configurable interval.

Trademark Notice:
    "Fluke" and "DSP-4000" are trademarks of Fluke Corporation. This tool is an
    independent, unofficial third-party utility and is not affiliated with,
    endorsed by, or sponsored by Fluke Corporation.
"""

import argparse
from itertools import batched
import os
from pathlib import Path
import struct
import time
import zlib

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


WIDTH_BYTES = 30
WIDTH_PX = WIDTH_BYTES * 8  # 240
HEIGHT_PX = 200
EXPECTED_DECOMP_BYTES = WIDTH_BYTES * HEIGHT_PX  # 6000
HALF_BLOCKS = ((" ", "▄"), ("▀", "█"))


def write_png_atomic(filename: Path, width: int, height: int, pixels_2d: list[list[int]]) -> None:
    """Write an 8-bit grayscale PNG atomically so image viewers never see a partial file."""
    raw_data = bytearray()
    for row in pixels_2d:
        raw_data.append(0)  # PNG filter type 0 (None)
        raw_data.extend(row)

    def make_chunk(ctype: bytes, cdata: bytes) -> bytes:
        crc = zlib.crc32(ctype + cdata) & 0xFFFFFFFF
        return struct.pack(">I", len(cdata)) + ctype + cdata + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + make_chunk(b"IHDR", ihdr)
        + make_chunk(b"IDAT", zlib.compress(bytes(raw_data), 9))
        + make_chunk(b"IEND", b"")
    )
    tmp_path = filename.with_name(f".{filename.name}.tmp")
    tmp_path.write_bytes(png)
    os.replace(tmp_path, filename)


def request_screenshot_payload(ser: PosixSerial) -> bytes:
    """Perform the screenshot request cycle (\\x14\\non\\ni\\n -> it\\n -> db\\ng -> ACK)."""
    init_session(ser)
    ser.write_paced(b"db\ng")
    payload = read_framed_response(ser)
    send_ack(ser)
    return payload


def decompress_rle(payload: bytes) -> bytes:
    """Decompress 2-byte RLE pairs (byte_value, count - 1)."""
    out = bytearray()
    for val, count_minus_1 in batched(payload, 2, strict=True):
        out.extend([val] * (count_minus_1 + 1))
    return bytes(out)


def bitmap_to_pixels(decomp: bytes, invert: bool = False) -> list[list[int]]:
    """Convert 6000-byte 1-bpp MSB-first buffer into 200x240 8-bit grayscale rows."""
    if len(decomp) < EXPECTED_DECOMP_BYTES:
        raise ValueError(f"Expected {EXPECTED_DECOMP_BYTES} bytes, got {len(decomp)}")
    fg, bg = (255, 0) if invert else (0, 255)
    return [
        [fg if (b >> bit) & 1 else bg for b in row_bytes for bit in range(7, -1, -1)]
        for row_bytes in batched(decomp[:EXPECTED_DECOMP_BYTES], WIDTH_BYTES, strict=True)
    ]


def render_ascii_art(decomp: bytes) -> str:
    """Render a compact ASCII preview using Unicode half-blocks (2 vertical pixels per char)."""
    rows = list(batched(decomp[:EXPECTED_DECOMP_BYTES], WIDTH_BYTES, strict=True))
    return "\n".join(
        "".join(
            HALF_BLOCKS[(tb >> bit) & 1][(bb >> bit) & 1]
            for tb, bb in zip(top_row, bot_row)
            for bit in range(7, -1, -1)
        )
        for top_row, bot_row in batched(rows, 2, strict=True)
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture screenshots from a Fluke DSP-4000 over serial (/dev/ttyUSB0)."
    )
    parser.add_argument("-d", "--device", default=DEFAULT_DEVICE, help=f"Serial port device (default: {DEFAULT_DEVICE})")
    parser.add_argument("-b", "--baud", type=int, default=DEFAULT_BAUD, help=f"Serial baud rate (default: {DEFAULT_BAUD})")
    parser.add_argument("-t", "--timeout", type=float, default=3.0, help="Serial read timeout in seconds (default: 3.0)")
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=0.0,
        help="Refresh interval in seconds (e.g. 2.0). If 0 or omitted, captures once and exits.",
    )
    parser.add_argument("-o", "--output", default="DSP_Display_screenshot.png", help="Output PNG path (default: DSP_Display_screenshot.png)")
    parser.add_argument("--invert", action="store_true", help="Invert colors (white pixels on black background)")
    parser.add_argument("--ascii", action="store_true", help="Print an ASCII/Unicode preview of the screen to the terminal on each capture")
    args = parser.parse_args()

    out_path = Path(args.output)
    print(f"Opening {args.device} @ {args.baud} 8N1...")

    with PosixSerial(args.device, baudrate=args.baud, timeout=args.timeout) as ser:
        wake_device(ser)
        frame_num = 0
        try:
            while True:
                t_start = time.monotonic()
                payload = request_screenshot_payload(ser)
                decomp = decompress_rle(payload)
                pixels = bitmap_to_pixels(decomp, invert=args.invert)
                write_png_atomic(out_path, WIDTH_PX, HEIGHT_PX, pixels)
                frame_num += 1

                if args.ascii:
                    if args.interval > 0:
                        print("\033[2J\033[H", end="")  # Clear screen for live refresh
                    print(render_ascii_art(decomp))

                print(
                    f"[{time.strftime('%H:%M:%S')}] Frame #{frame_num}: "
                    f"{len(payload)}B RLE -> {len(decomp)}B ({WIDTH_PX}x{HEIGHT_PX}) saved to {out_path}"
                )

                if args.interval <= 0:
                    break

                if (sleep_time := args.interval - (time.monotonic() - t_start)) > 0:
                    time.sleep(sleep_time)
        except KeyboardInterrupt:
            print("\nStopping capture...")
        finally:
            close_session(ser)


if __name__ == "__main__":
    main()
