#!/usr/bin/env python3
"""Is the PS5 actually sending? — a zero-dependency UDP telemetry sniffer.

Binds the telemetry port and prints, once per second, how many datagrams arrived of each
packet id plus total throughput. It parses nothing but the packet header's first 7 bytes,
imports nothing from f126, and deliberately duplicates the packet-id table so that it
keeps working when the parser is mid-refactor or the venv is broken.

    uv run python tools/sniff.py [--port 20777] [--host 0.0.0.0] [--interval 1.0]

Reading the output:
  * nothing at all      -> game not sending, wrong IP, or a firewall between you and it
  * only id 1 at 2/s    -> UDP is on but you are sitting in a menu, not on track
  * id 6 near 20-60/s   -> car telemetry is flowing; you are good to capture
  * "format 2026"       -> the 2026 Season Pack layout (id 16 CarTelemetry2 may appear)

Header layout (F1 23/24/25, 29 bytes, little-endian, packed):
    0  uint16 packetFormat        6  uint8  packetId
    2  uint8  gameYear            7  uint64 sessionUID
    3  uint8  gameMajorVersion   15  float  sessionTime
    4  uint8  gameMinorVersion   ...
    5  uint8  packetVersion
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

HEADER_SIZE = 29
_U16 = struct.Struct("<H")
_U64 = struct.Struct("<Q")

PACKET_NAMES: dict[int, str] = {
    0: "Motion",
    1: "Session",
    2: "LapData",
    3: "Event",
    4: "Participants",
    5: "CarSetups",
    6: "CarTelemetry",
    7: "CarStatus",
    8: "FinalClassification",
    9: "LobbyInfo",
    10: "CarDamage",
    11: "SessionHistory",
    12: "TyreSets",
    13: "MotionEx",
    14: "TimeTrial",
    15: "LapPositions",
    16: "CarTelemetry2",
}


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sniff.py",
        description="Print per-second F1 UDP packet counts by packet id.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=20777, help="UDP port (default: 20777)")
    parser.add_argument(
        "--interval", type=float, default=1.0, help="seconds between reports (default: 1.0)"
    )
    parser.add_argument(
        "--rcvbuf",
        type=int,
        default=4 * 1024 * 1024,
        help="SO_RCVBUF to request in bytes (default: 4 MiB)",
    )
    parser.add_argument(
        "--seconds", type=float, default=0.0, help="stop after N seconds (default: run forever)"
    )
    args = parser.parse_args(argv)

    if args.interval <= 0:
        parser.error("--interval must be > 0")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    requested = args.rcvbuf
    while requested > 0:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, requested)
            break
        except OSError:
            requested //= 2
    try:
        sock.bind((args.host, args.port))
    except OSError as exc:
        print(f"cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        sock.close()
        return 1
    granted = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    sock.settimeout(0.2)

    print(
        f"listening on {args.host}:{args.port} (rcvbuf {_fmt_bytes(granted)}) — Ctrl-C to stop",
        flush=True,
    )

    counts: dict[int, int] = {}
    window_bytes = 0
    window_packets = 0
    short = 0
    formats: set[int] = set()
    senders: set[str] = set()
    session_uids: set[int] = set()

    grand_packets = 0
    grand_bytes = 0
    started = time.monotonic()
    next_report = started + args.interval
    deadline = started + args.seconds if args.seconds > 0 else None
    silent_windows = 0

    try:
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except TimeoutError:
                data = b""
                addr = ("", 0)
            except OSError as exc:
                print(f"recv error: {exc}", file=sys.stderr)
                break

            if data:
                window_packets += 1
                window_bytes += len(data)
                senders.add(addr[0])
                if len(data) >= 7:
                    formats.add(_U16.unpack_from(data, 0)[0])
                    pid = data[6]
                    counts[pid] = counts.get(pid, 0) + 1
                    if len(data) >= 15:
                        session_uids.add(_U64.unpack_from(data, 7)[0])
                else:
                    short += 1

            now = time.monotonic()
            if now >= next_report:
                elapsed = now - (next_report - args.interval)
                grand_packets += window_packets
                grand_bytes += window_bytes
                stamp = time.strftime("%H:%M:%S")
                if window_packets == 0:
                    silent_windows += 1
                    hint = "  <- nothing arriving" if silent_windows in (3, 10, 30) else ""
                    print(f"[{stamp}] no packets{hint}", flush=True)
                else:
                    silent_windows = 0
                    breakdown = "  ".join(
                        f"{PACKET_NAMES.get(pid, f'id{pid}')}({pid})={n / elapsed:.0f}/s"
                        for pid, n in sorted(counts.items())
                    )
                    fmt = ",".join(str(f) for f in sorted(formats)) or "?"
                    print(
                        f"[{stamp}] {window_packets / elapsed:.0f} pkt/s  "
                        f"{_fmt_bytes(window_bytes / elapsed)}/s  format {fmt}\n"
                        f"           {breakdown}",
                        flush=True,
                    )
                counts.clear()
                formats.clear()
                window_packets = 0
                window_bytes = 0
                next_report = now + args.interval

            if deadline is not None and now >= deadline:
                break
    except KeyboardInterrupt:
        print()
    finally:
        sock.close()

    grand_packets += window_packets
    grand_bytes += window_bytes
    elapsed = max(time.monotonic() - started, 1e-9)
    print(
        f"total {grand_packets} packets / {_fmt_bytes(grand_bytes)} in {elapsed:.1f}s "
        f"({grand_packets / elapsed:.0f} pkt/s avg)"
    )
    if short:
        print(f"{short} datagram(s) shorter than a packet header — not F1 traffic?")
    if senders:
        print(f"sender(s): {', '.join(sorted(senders))}")
    if session_uids:
        print(f"session uid(s) seen: {', '.join(str(u) for u in sorted(session_uids))}")
    if grand_packets == 0:
        print(
            "\nNo packets. Check: game UDP telemetry ON, IP set to this machine, "
            f"port {args.port}, same network, firewall allows inbound UDP.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
