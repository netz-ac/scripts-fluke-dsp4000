#!/usr/bin/env python3
"""
Shared serial communication and framing protocol module for Fluke DSP-4000 tools.

Requires Python 3.13+.

Provides:
- PosixSerial: Zero-dependency POSIX serial port driver (9600..115200 8N1, DTR/RTS pulse, 16ms TX pacing)
- read_print_stream: Passive serial stream capture for the serial printer function (default 115200 baud)
- Framed block transfer reader & parser ('F' full 256B blocks, 'P' partial blocks, 'E' end marker,
  with 8-bit two's complement checksum verification)
- Common session lifecycle helpers (wake_device, init_session, send_ack, close_session)
"""

from contextlib import suppress
import fcntl
import os
import select
import struct
import sys
import termios
import time
from typing import Self


DEFAULT_DEVICE = "/dev/ttyUSB0"
DEFAULT_BAUD = 115200
DEFAULT_PRINT_BAUD = DEFAULT_BAUD
TX_BYTE_DELAY = 0.016  # 16ms inter-character pacing used by host software


class PosixSerial:
    """Zero-dependency POSIX serial port wrapper using termios/fcntl."""

    TIOCMGET = 0x5415
    TIOCMSET = 0x5418
    TIOCM_DTR = 0x002
    TIOCM_RTS = 0x004

    BAUD_MAP = {
        9600: termios.B9600,
        19200: termios.B19200,
        38400: termios.B38400,
        57600: termios.B57600,
        115200: termios.B115200,
    }

    def __init__(
        self,
        port: str = DEFAULT_DEVICE,
        baudrate: int = DEFAULT_BAUD,
        timeout: float = 3.0,
        pulse_dtr_rts: bool = True,
    ) -> None:
        self.port = port
        self.timeout = timeout
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            self._configure(baudrate, pulse_dtr_rts=pulse_dtr_rts)
        except Exception:
            os.close(self.fd)
            raise

    def _configure(self, baudrate: int, pulse_dtr_rts: bool = True) -> None:
        if baudrate not in self.BAUD_MAP:
            raise ValueError(f"Unsupported baud rate: {baudrate}")
        speed = self.BAUD_MAP[baudrate]

        attrs = termios.tcgetattr(self.fd)
        # Raw 8N1, no flow control
        attrs[0] = 0  # iflag
        attrs[1] = 0  # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
        attrs[3] = 0  # lflag
        attrs[4] = speed  # ispeed
        attrs[5] = speed  # ospeed
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

        with suppress(OSError):
            (status,) = struct.unpack("I", fcntl.ioctl(self.fd, self.TIOCMGET, struct.pack("I", 0)))
            if pulse_dtr_rts:
                # Match the USB control sequence from capture: pulse DTR/RTS then deassert
                for mask in (self.TIOCM_DTR | self.TIOCM_RTS, ~self.TIOCM_DTR, ~self.TIOCM_RTS):
                    status = (status | mask) if mask > 0 else (status & mask)
                    fcntl.ioctl(self.fd, self.TIOCMSET, struct.pack("I", status))
                    time.sleep(0.005)
            else:
                # Assert DTR and RTS for passive serial printer reception (CTS/DSR ready)
                status |= self.TIOCM_DTR | self.TIOCM_RTS
                fcntl.ioctl(self.fd, self.TIOCMSET, struct.pack("I", status))

        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def flush_input(self) -> None:
        termios.tcflush(self.fd, termios.TCIFLUSH)

    def write_paced(self, data: bytes, delay: float = TX_BYTE_DELAY) -> None:
        """Send bytes one at a time with inter-byte delay matching the host software."""
        for b in data:
            os.write(self.fd, bytes([b]))
            termios.tcdrain(self.fd)
            if delay > 0:
                time.sleep(delay)

    def read_exact(self, size: int, timeout: float | None = None) -> bytes:
        """Read exactly `size` bytes or raise TimeoutError."""
        timeout = self.timeout if timeout is None else timeout
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while len(buf) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                raise TimeoutError(
                    f"Timed out waiting for {size} bytes (received {len(buf)} bytes: {bytes(buf)!r})"
                )
            if chunk := os.read(self.fd, size - len(buf)):
                buf.extend(chunk)
        return bytes(buf)

    def read_until(self, expected: bytes, timeout: float | None = None) -> bytes:
        """Read until `expected` suffix is seen or raise TimeoutError."""
        timeout = self.timeout if timeout is None else timeout
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while not buf.endswith(expected):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                raise TimeoutError(
                    f"Timed out waiting for {expected!r} (received {bytes(buf)!r})"
                )
            if chunk := os.read(self.fd, 1024):
                buf.extend(chunk)
        return bytes(buf)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()


def read_print_stream(
    port: str = DEFAULT_DEVICE,
    baudrate: int = DEFAULT_PRINT_BAUD,
    idle_timeout: float = 3.0,
    max_wait: float = 120.0,
) -> str:
    """
    Listen passively on `port` for a serial printout stream (e.g. from the tester's
    Print function) and return the decoded text once `idle_timeout` seconds of
    silence have elapsed after receiving data.
    """
    print(
        f"[*] Listening on {port} at {baudrate} baud (waiting for serial printout for Fluke DSP-4000)...",
        file=sys.stderr,
    )

    with PosixSerial(port=port, baudrate=baudrate, timeout=0.25, pulse_dtr_rts=False) as ser:
        buf = bytearray()
        start_time = time.monotonic()
        last_rx: float | None = None
        while True:
            rlist, _, _ = select.select([ser.fd], [], [], 0.25)
            now = time.monotonic()
            if rlist and (chunk := os.read(ser.fd, 4096)):
                buf.extend(chunk)
                last_rx = now
                print(f"\r[*] Received {len(buf)} bytes...", end="", file=sys.stderr, flush=True)
            elif last_rx is not None and (now - last_rx) >= idle_timeout:
                print(f"\n[*] Print transmission complete ({len(buf)} bytes).", file=sys.stderr)
                return buf.decode("latin-1", errors="replace")
            elif last_rx is None and (now - start_time) >= max_wait:
                raise TimeoutError(f"No data received on {port} within {max_wait} seconds.")


def verify_block_checksum(chunk: bytes, csum: int, block_type: str, block_idx: int) -> None:
    """Verify 8-bit two's complement checksum: csum == (-sum(chunk)) & 0xFF."""
    expected_csum = (-sum(chunk)) & 0xFF
    if csum != expected_csum:
        raise ValueError(
            f"Checksum mismatch in '{block_type}' block {block_idx}: "
            f"got 0x{csum:02x}, expected 0x{expected_csum:02x}"
        )


def read_framed_response(ser: PosixSerial) -> bytes:
    """Read 'F' / 'P' / 'E' framed binary blocks from the serial port and verify checksums."""
    payload = bytearray()
    block_idx = 0

    while True:
        match ser.read_exact(1)[0]:
            case 0x46:  # 'F' - Full 256-byte block
                csum = ser.read_exact(1)[0]
                chunk = ser.read_exact(256)
                verify_block_checksum(chunk, csum, "F", block_idx)
                payload.extend(chunk)
                block_idx += 1
            case 0x50:  # 'P' - Partial block
                csum, plen = struct.unpack(">BH", ser.read_exact(3))
                chunk = ser.read_exact(plen)
                verify_block_checksum(chunk, csum, "P", block_idx)
                payload.extend(chunk)
                block_idx += 1
            case 0x45:  # 'E' - End of transfer
                return bytes(payload)
            case marker:
                raise ValueError(
                    f"Unexpected block marker 0x{marker:02x} ({chr(marker) if 32 <= marker < 127 else '?'})"
                )


def parse_framed_bytes(raw_resp: bytes) -> bytes:
    """Unpack a complete 'F' / 'P' / 'E' framed binary buffer in memory and verify checksums."""
    pos = 0
    payload = bytearray()
    block_idx = 0

    while pos < len(raw_resp):
        match raw_resp[pos]:
            case 0x46:  # 'F' - Full 256-byte block
                if pos + 258 > len(raw_resp):
                    raise ValueError(f"Truncated 'F' block at offset {pos}")
                csum = raw_resp[pos + 1]
                chunk = raw_resp[pos + 2 : pos + 258]
                verify_block_checksum(chunk, csum, "F", block_idx)
                payload.extend(chunk)
                pos += 258
                block_idx += 1
            case 0x50:  # 'P' - Partial block
                if pos + 4 > len(raw_resp):
                    raise ValueError(f"Truncated 'P' block header at offset {pos}")
                csum, plen = struct.unpack_from(">BH", raw_resp, pos + 1)
                if pos + 4 + plen > len(raw_resp):
                    raise ValueError(f"Truncated 'P' block payload at offset {pos}")
                chunk = raw_resp[pos + 4 : pos + 4 + plen]
                verify_block_checksum(chunk, csum, "P", block_idx)
                payload.extend(chunk)
                pos += 4 + plen
                block_idx += 1
            case 0x45:  # 'E' - End of transfer
                break
            case marker:
                raise ValueError(f"Unexpected block marker 0x{marker:02x} at offset {pos}")

    return bytes(payload)


def wake_device(ser: PosixSerial) -> None:
    """Send wake sequence (\\x12o) and verify 'o' echo."""
    ser.flush_input()
    ser.write_paced(b"\x12o")
    ser.read_until(b"o")


def init_session(ser: PosixSerial) -> str:
    """Enable remote session (\\x14\\non\\ni\\n -> g\\r\\n) and query instrument type (it\\n -> 300\\r\\n)."""
    ser.write_paced(b"\x14\non\ni\n")
    ser.read_until(b"g\r\n")

    ser.write_paced(b"it\n")
    return ser.read_until(b"\r\n").decode("ascii", errors="replace").strip()


def send_ack(ser: PosixSerial) -> None:
    """Send 0x06 (ACK) after receiving a framed binary response."""
    ser.write_paced(b"\x06", delay=0.0)


def close_session(ser: PosixSerial, disable_dc: bool = False) -> None:
    """Cleanly exit remote session on the device (optionally sending dc0\\n first)."""
    with suppress(Exception):
        if disable_dc:
            ser.write_paced(b"dc0\n")
        ser.write_paced(b"\x12off\n")
        ser.read_until(b"\r\n", timeout=1.0)
