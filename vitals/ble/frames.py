"""WHOOP 5.0 / MG ("puffin") frame envelope.

Layout, from community reverse engineering (b-nnett/goose, NOOP, dofek) and
checked against the published CLIENT_HELLO test vector in tests/:

    0      1      2..3        4      5      6..7         8..N-4    N-4..N
    0xAA   0x01   length(LE)  role1  role2  crc16(LE)    payload   crc32(LE)

    length = len(payload) + 4
    crc16  = CRC16-Modbus over bytes 0..5
    crc32  = CRC32 (zlib / IEEE) over payload

Payload (commands):  [packet_type][sequence][command][data...]
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

SOF = 0xAA
VERSION = 0x01
HEADER_LEN = 8


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def build_frame(payload: bytes, role1: int = 0x00, role2: int = 0x01) -> bytes:
    head = struct.pack("<BBHBB", SOF, VERSION, len(payload) + 4, role1, role2)
    return head + struct.pack("<H", crc16_modbus(head)) + payload + struct.pack("<I", crc32(payload))


@dataclass
class Frame:
    role1: int
    role2: int
    payload: bytes

    @property
    def packet_type(self) -> int:
        return self.payload[0] if self.payload else -1


class FrameAssembler:
    """Reassembles MTU-sized notification fragments into CRC-valid frames.

    Bytes that don't form a valid frame are skipped one at a time until the
    stream re-synchronises, so a dropped fragment costs one frame, not the session.
    """

    MAX_LEN = 4096

    def __init__(self) -> None:
        self.buf = bytearray()
        self.bad_frames = 0

    def feed(self, chunk: bytes) -> list[Frame]:
        self.buf.extend(chunk)
        out: list[Frame] = []
        while True:
            start = self.buf.find(bytes([SOF, VERSION]))
            if start < 0:
                # keep a trailing 0xAA in case its 0x01 arrives next
                self.buf = self.buf[-1:] if self.buf[-1:] == bytes([SOF]) else bytearray()
                return out
            if start:
                del self.buf[:start]
            if len(self.buf) < HEADER_LEN:
                return out
            length, role1, role2, hcrc = struct.unpack_from("<HBBH", self.buf, 2)
            if crc16_modbus(bytes(self.buf[:6])) != hcrc or not (4 <= length <= self.MAX_LEN):
                self.bad_frames += 1
                del self.buf[:1]
                continue
            total = HEADER_LEN + length
            if len(self.buf) < total:
                return out
            payload = bytes(self.buf[HEADER_LEN:total - 4])
            (pcrc,) = struct.unpack_from("<I", self.buf, total - 4)
            if crc32(payload) != pcrc:
                self.bad_frames += 1
                del self.buf[:1]
                continue
            out.append(Frame(role1, role2, payload))
            del self.buf[:total]


def parse_standard_hr(data: bytes) -> tuple[int, list[float]]:
    """Bluetooth SIG Heart Rate Measurement (0x2A37): returns (bpm, rr_ms list)."""
    flags = data[0]
    i = 1
    if flags & 0x01:
        bpm = struct.unpack_from("<H", data, i)[0]
        i += 2
    else:
        bpm = data[i]
        i += 1
    if flags & 0x08:  # energy expended present
        i += 2
    rr = []
    if flags & 0x10:
        while i + 1 < len(data):
            rr.append(round(struct.unpack_from("<H", data, i)[0] * 1000 / 1024, 1))
            i += 2
    return bpm, rr
