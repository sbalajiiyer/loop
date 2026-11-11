"""Collector for a WHOOP 5.0 / MG strap, run on the Pi or a laptop.

What it does on every connection:
  1. Starts the session with the static CLIENT_HELLO frame (also bonds).
  2. Subscribes to the standard heart-rate profile (0x2A37): beat-level HR and
     R-R intervals while you're in range. This part is reliable today and is
     what overnight HRV, resting HR and sleep timing are computed from.
  3. Subscribes to the strap's custom channels and stores every frame verbatim.
  4. Asks the strap to offload its stored history (up to 14 days), and only
     acknowledges a batch after it has been written to storage, so the strap
     never discards data we haven't kept.

Usage:
  python -m loop.ble.whoop_mg scan                 # find your strap's address
  python -m loop.ble.whoop_mg run                  # on the Pi (writes locally)
  python -m loop.ble.whoop_mg run --upload https://loop.<tailnet>.ts.net
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import struct
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

from . import protocol as P
from .frames import FrameAssembler, build_frame, parse_standard_hr
from .history import decode as decode_history, load_layout
from .sinks import HttpSink, LocalSink

log = logging.getLogger("loop.whoop")
DEVICE = "whoop"
FLUSH_SECONDS = 15
HISTORY_EVERY_SECONDS = 30 * 60
RECONNECT_SECONDS = 20
META_TRIM_OFFSET = 10  # VERIFY: byte offset of the trim cursor inside a HISTORY_END record


class WhoopSession:
    def __init__(self, address: str, sink, want_history: bool = True) -> None:
        self.address = address
        self.sink = sink
        self.want_history = want_history
        self.asm = {name: FrameAssembler() for name in P.NOTIFY_CHARS}
        self.samples: dict[int, tuple] = {}
        self.frames: list[tuple] = []
        self.seq = 0
        self.client: BleakClient | None = None
        self.pending_ack: int | None = None
        self.last_history = 0.0
        self.history_records = 0
        self.history_samples: list[tuple] = []
        self.layout = load_layout()  # set once `python -m loop.ble.history probe --save` has run

    # ---- outgoing -------------------------------------------------------
    async def send(self, command: int, data: bytes = b"") -> None:
        if command not in P.ALLOWED_COMMANDS:
            raise ValueError(f"command {command} is not on the allow-list")
        self.seq = (self.seq + 1) & 0xFF
        frame = build_frame(bytes([P.PT_COMMAND, self.seq, command]) + data)
        await self.client.write_gatt_char(P.CHAR_CMD_TO_STRAP, frame, response=True)

    # ---- incoming -------------------------------------------------------
    def on_standard_hr(self, _char, data: bytearray) -> None:
        try:
            bpm, rr = parse_standard_hr(bytes(data))
        except Exception:  # noqa: BLE001
            return
        ts = int(time.time())
        prev = self.samples.get(ts)
        if prev and prev[2]:
            rr = list(prev[2]) + rr
        self.samples[ts] = (ts, bpm, rr or None, None)

    def make_custom_handler(self, name: str):
        def handler(_char, data: bytearray) -> None:
            now = int(time.time())
            self.frames.append((now, name, bytes(data).hex()))
            for frame in self.asm[name].feed(bytes(data)):
                self.on_frame(frame)
        return handler

    def on_frame(self, frame) -> None:
        pt = frame.packet_type
        if pt == P.PT_HISTORICAL:
            self.history_records += 1
            if self.layout and (s := decode_history(frame.payload, self.layout)):
                self.history_samples.append(s)
        elif pt in (P.PT_METADATA, P.PT_PUFFIN_METADATA) and len(frame.payload) > 2:
            meta = frame.payload[2]
            if meta == P.META_HISTORY_END and len(frame.payload) >= META_TRIM_OFFSET + 4:
                (trim,) = struct.unpack_from("<I", frame.payload, META_TRIM_OFFSET)
                self.pending_ack = trim  # acked in the main loop after flushing
            elif meta == P.META_HISTORY_COMPLETE:
                log.info("history offload complete (%d records this pass)", self.history_records)
                self.history_records = 0

    # ---- storage --------------------------------------------------------
    def flush(self) -> None:
        samples = sorted(self.samples.values())
        frames, self.frames = self.frames, []
        self.samples = {}
        if samples or frames:
            self.sink.write(DEVICE, samples, frames)
            log.info("stored %d HR seconds, %d raw frames", len(samples), len(frames))
        if self.history_samples:
            hist, self.history_samples = self.history_samples, []
            write_history = getattr(self.sink, "write_history", None)
            if write_history:
                write_history(DEVICE, hist)
            log.info("decoded %d history samples", len(hist))

    # ---- main loop ------------------------------------------------------
    async def run_once(self) -> None:
        async with BleakClient(self.address, timeout=30) as client:
            self.client = client
            log.info("connected to %s", self.address)
            await client.write_gatt_char(P.CHAR_CMD_TO_STRAP, P.CLIENT_HELLO, response=True)
            for name, uuid in P.NOTIFY_CHARS.items():
                try:
                    await client.start_notify(uuid, self.make_custom_handler(name))
                except Exception as e:  # noqa: BLE001
                    log.debug("no %s channel: %s", name, e)
            await self.send(P.CMD_TOGGLE_GENERIC_HR_PROFILE, b"\x01")
            await client.start_notify(P.HR_MEASUREMENT, self.on_standard_hr)

            last_flush = time.monotonic()
            while client.is_connected:
                await asyncio.sleep(1)
                if self.want_history and time.time() - self.last_history > HISTORY_EVERY_SECONDS:
                    self.last_history = time.time()
                    log.info("requesting stored history")
                    await self.send(P.CMD_SEND_HISTORICAL_DATA)
                if self.pending_ack is not None:
                    self.flush()                      # durable first...
                    trim, self.pending_ack = self.pending_ack, None
                    await self.send(P.CMD_HISTORICAL_DATA_RESULT, struct.pack("<BII", 1, trim, 0))  # ...then ack
                if time.monotonic() - last_flush > FLUSH_SECONDS:
                    self.flush()
                    last_flush = time.monotonic()
        self.flush()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
                log.info("disconnected")
            except Exception as e:  # noqa: BLE001
                log.info("strap not reachable (%s); retrying in %ss", type(e).__name__, RECONNECT_SECONDS)
            self.flush()
            await asyncio.sleep(RECONNECT_SECONDS)


async def scan(seconds: float) -> None:
    print(f"Scanning {seconds:.0f}s. Put the strap in pairing mode if nothing shows up.")
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    for dev, adv in found.values():
        uuids = [u.lower() for u in adv.service_uuids or []]
        if (dev.name and "whoop" in dev.name.lower()) or P.SERVICE in uuids:
            print(f"  WHOOP  {dev.address}  {dev.name}  rssi={adv.rssi}")
    print("Put the address in .env as WHOOP_ADDRESS=...")


def main() -> None:
    from .. import config
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--seconds", type=float, default=20)
    r = sub.add_parser("run")
    r.add_argument("--address", default=config.get("WHOOP_ADDRESS"))
    r.add_argument("--upload", help="Pi dashboard URL; when set, data is uploaded instead of stored locally")
    r.add_argument("--no-history", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")

    if args.cmd == "scan":
        asyncio.run(scan(args.seconds))
        return
    if not args.address:
        raise SystemExit("Set WHOOP_ADDRESS in .env (run the scan command first)")
    sink = (HttpSink(args.upload, config.INGEST_TOKEN, Path.home() / ".loop-spool")
            if args.upload else LocalSink())
    asyncio.run(WhoopSession(args.address, sink, not args.no_history).run_forever())


if __name__ == "__main__":
    main()
