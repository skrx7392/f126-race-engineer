"""Convert a verification capture (.cap: <u64 mono_ns, u16 len> + payload records)
into a real .f1raw file so it can drive `f126 replay` and the golden tests.

The .cap format has no wall clock; wall timestamps are synthesized from a fixed
epoch base plus the monotonic delta, which preserves inter-packet spacing.

Usage: uv run python tools/cap2f1raw.py in.cap out_dir/
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from f126.capture.rawlog import RawLogWriter  # noqa: E402

_REC = struct.Struct("<QH")
_WALL_BASE_NS = 1_754_500_000_000_000_000  # fixed, arbitrary 2026 epoch


def convert(cap_path: Path, out_dir: Path) -> Path:
    data = cap_path.read_bytes()
    writer = RawLogWriter(out_dir, session_uid="fixture", segment=0)
    off = 0
    first_mono: int | None = None
    count = 0
    while off + _REC.size <= len(data):
        mono_ns, length = _REC.unpack_from(data, off)
        off += _REC.size
        payload = data[off : off + length]
        if len(payload) < length:
            break
        off += length
        if first_mono is None:
            first_mono = mono_ns
        wall_ns = _WALL_BASE_NS + (mono_ns - first_mono)
        writer.write(payload, mono_ns, wall_ns)
        count += 1
    final = writer.close()
    print(f"{cap_path.name}: {count} packets -> {final}")
    return final


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    convert(Path(sys.argv[1]), Path(sys.argv[2]))
