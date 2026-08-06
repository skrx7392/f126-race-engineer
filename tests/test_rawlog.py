"""Tests for the .f1raw write-ahead log.

The format is frozen, so these tests assert bytes on disk, not just round-tripping.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest
import zstandard as zstd

from f126.capture.rawlog import (
    CAPTURE_VERSION,
    MAGIC,
    OPEN_SUFFIX,
    RAW_SUFFIX,
    RECORD_HEADER_SIZE,
    RawLogError,
    RawLogReader,
    RawLogWriter,
    raw_dir,
    recover_orphans,
)

_META_LEN = struct.Struct("<I")
_RECORD = struct.Struct("<QQH")


def synth(n: int, *, size: int = 64, t0: int = 1_000_000_000) -> list[tuple[bytes, int, int]]:
    """N synthetic records with 1 ms spacing and distinguishable payloads."""
    out = []
    for i in range(n):
        payload = bytes([i % 256]) * size + i.to_bytes(4, "little")
        out.append((payload, t0 + i * 1_000_000, 1_700_000_000_000_000_000 + i * 1_000_000))
    return out


class FakeClock:
    """Monotonic clock we drive by hand, for fsync-interval assertions."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


# ---- round trip -----------------------------------------------------------


def test_write_read_roundtrip_exact_bytes(tmp_path: Path) -> None:
    records = synth(500, size=1200)
    writer = RawLogWriter(tmp_path, session_uid=1234567890123456789, segment=2)
    for data, mono, wall in records:
        writer.write(data, mono, wall)
    assert writer.packets_written == 500
    assert writer.bytes_written == sum(len(d) for d, _, _ in records)
    final = writer.close()

    assert final.exists()
    assert final.suffix == RAW_SUFFIX
    assert not final.name.endswith(OPEN_SUFFIX)

    with RawLogReader(final) as reader:
        read_back = list(reader)
        assert reader.truncated is False
    assert read_back == records


def test_records_helper_boxes_fields(tmp_path: Path) -> None:
    records = synth(3)
    with RawLogWriter(tmp_path) as writer:
        for data, mono, wall in records:
            writer.write(data, mono, wall)
        final = writer.final_path
    with RawLogReader(final) as reader:
        boxed = list(reader.records())
    assert [(r.data, r.recv_monotonic_ns, r.recv_wall_ns) for r in boxed] == records


def test_empty_capture_is_readable(tmp_path: Path) -> None:
    writer = RawLogWriter(tmp_path)
    final = writer.close()
    with RawLogReader(final) as reader:
        assert list(reader) == []
        assert reader.truncated is False


def test_zero_length_and_max_payloads(tmp_path: Path) -> None:
    writer = RawLogWriter(tmp_path)
    writer.write(b"", 1, 2)
    writer.write(b"\xff" * 65535, 3, 4)
    with pytest.raises(ValueError, match="exceeds"):
        writer.write(b"\x00" * 65536, 5, 6)
    final = writer.close()
    with RawLogReader(final) as reader:
        got = list(reader)
    assert got == [(b"", 1, 2), (b"\xff" * 65535, 3, 4)]


def test_u64_stamps_survive_full_range(tmp_path: Path) -> None:
    big_mono = 2**64 - 1
    big_wall = 2**63 + 12345
    writer = RawLogWriter(tmp_path)
    writer.write(b"x", big_mono, big_wall)
    final = writer.close()
    with RawLogReader(final) as reader:
        assert list(reader) == [(b"x", big_mono, big_wall)]


# ---- on-disk layout -------------------------------------------------------


def test_file_layout_is_the_frozen_format(tmp_path: Path) -> None:
    records = synth(10)
    writer = RawLogWriter(tmp_path, session_uid="42", segment=7, host="pitwall")
    for data, mono, wall in records:
        writer.write(data, mono, wall)
    final = writer.close()

    blob = final.read_bytes()
    assert blob[:6] == MAGIC == b"F1RAW\x01"
    (meta_len,) = _META_LEN.unpack_from(blob, 6)
    meta = json.loads(blob[10 : 10 + meta_len].decode("utf-8"))
    assert meta["capture_version"] == CAPTURE_VERSION
    assert meta["host"] == "pitwall"
    assert meta["session_uid"] == "42"
    assert meta["segment"] == 7
    assert isinstance(meta["started_wall_ns"], int)
    assert isinstance(meta["started_monotonic_ns"], int)

    # The remainder is a zstd stream of packed records, independent of our reader.
    raw = zstd.ZstdDecompressor().decompressobj().decompress(blob[10 + meta_len :])
    mono, wall, n = _RECORD.unpack_from(raw, 0)
    assert (mono, wall, n) == (records[0][1], records[0][2], len(records[0][0]))
    assert raw[RECORD_HEADER_SIZE : RECORD_HEADER_SIZE + n] == records[0][0]


def test_meta_json_round_trip(tmp_path: Path) -> None:
    writer = RawLogWriter(tmp_path, session_uid=987654321, segment=3, host="host-a")
    written_meta = writer.meta
    final = writer.close()
    with RawLogReader(final) as reader:
        assert reader.meta == written_meta
        assert reader.capture_version == CAPTURE_VERSION
        assert reader.meta["session_uid"] == "987654321"
        assert reader.meta["segment"] == 3
        assert reader.meta["host"] == "host-a"


def test_compression_actually_happens(tmp_path: Path) -> None:
    # Highly compressible payloads: on-disk size must be far below raw volume.
    writer = RawLogWriter(tmp_path)
    for i in range(2000):
        writer.write(b"\x00" * 1000, i, i)
    final = writer.close()
    assert final.stat().st_size < writer.bytes_written // 10


def test_bad_magic_rejected(tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.f1raw"
    bogus.write_bytes(b"NOPE!!" + b"\x00" * 32)
    with pytest.raises(RawLogError, match="bad magic"):
        RawLogReader(bogus)


def test_truncated_metadata_rejected(tmp_path: Path) -> None:
    partial = tmp_path / "partial.f1raw"
    partial.write_bytes(MAGIC + _META_LEN.pack(500) + b"{}")
    with pytest.raises(RawLogError, match="truncated metadata"):
        RawLogReader(partial)


def test_implausible_metadata_length_rejected(tmp_path: Path) -> None:
    """A corrupt length field must not turn into a multi-gigabyte allocation."""
    bad = tmp_path / "huge-meta.f1raw"
    bad.write_bytes(MAGIC + _META_LEN.pack(0xFFFFFFFF) + b"{}")
    with pytest.raises(RawLogError, match="implausible metadata length"):
        RawLogReader(bad)


def test_malformed_metadata_json_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad-json.f1raw"
    blob = b"not json at all"
    bad.write_bytes(MAGIC + _META_LEN.pack(len(blob)) + blob)
    with pytest.raises(RawLogError, match="unreadable metadata"):
        RawLogReader(bad)


# ---- naming and rotation --------------------------------------------------


def test_files_live_under_raw_and_are_open_while_writing(tmp_path: Path) -> None:
    writer = RawLogWriter(tmp_path, session_uid="unknown", segment=0)
    assert writer.path.parent == raw_dir(tmp_path)
    assert writer.path.name.endswith(RAW_SUFFIX + OPEN_SUFFIX)
    assert writer.path.exists()
    final = writer.close()
    assert not writer.path.name.endswith(OPEN_SUFFIX)
    assert final.exists()


def test_naming_encodes_iso_uid_and_segment(tmp_path: Path) -> None:
    writer = RawLogWriter(tmp_path, session_uid=1122334455, segment=4)
    name = writer.final_path.name
    writer.close()
    stem = name.removesuffix(RAW_SUFFIX)
    iso, uid, segment = stem.split("_")
    assert uid == "1122334455"
    assert segment == "4"
    assert len(iso) == 16 and iso[8] == "T" and iso.endswith("Z")


def test_rotate_finalizes_and_opens_new_file(tmp_path: Path) -> None:
    writer = RawLogWriter(tmp_path, session_uid="unknown", segment=0)
    writer.write(b"before", 1, 2)
    first_open_path = writer.path
    finalized = writer.rotate(555, 1)

    assert finalized.exists()
    assert not first_open_path.exists()
    assert not finalized.name.endswith(OPEN_SUFFIX)
    assert "_unknown_0" in finalized.name

    assert writer.session_uid == "555"
    assert writer.segment == 1
    assert "_555_1" in writer.path.name
    # Counters are cumulative across rotation; the file is new.
    writer.write(b"after", 3, 4)
    second = writer.close()
    assert writer.packets_written == 2

    with RawLogReader(finalized) as r1:
        assert list(r1) == [(b"before", 1, 2)]
    with RawLogReader(second) as r2:
        assert list(r2) == [(b"after", 3, 4)]
    assert r1.meta["session_uid"] == "unknown"
    assert r2.meta["session_uid"] == "555"


def test_name_collision_gets_disambiguated(tmp_path: Path) -> None:
    a = RawLogWriter(tmp_path, session_uid="same", segment=0)
    b = RawLogWriter(tmp_path, session_uid="same", segment=0)
    assert a.final_path != b.final_path
    pa, pb = a.close(), b.close()
    assert pa != pb
    assert pa.exists() and pb.exists()


def test_write_after_close_raises(tmp_path: Path) -> None:
    writer = RawLogWriter(tmp_path)
    writer.close()
    assert writer.closed
    with pytest.raises(RawLogError, match="closed"):
        writer.write(b"x", 1, 2)
    # close() is idempotent and keeps returning the final path.
    assert writer.close() == writer.final_path


# ---- fsync interval -------------------------------------------------------


def test_fsync_happens_at_most_once_per_interval(tmp_path: Path, monkeypatch) -> None:
    clock = FakeClock()
    calls: list[int] = []
    real_fsync = os.fsync

    def counting_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    writer = RawLogWriter(tmp_path, fsync_interval_s=1.0, clock=clock)
    monkeypatch.setattr("f126.capture.rawlog.os.fsync", counting_fsync)

    for i in range(100):
        writer.write(b"payload", i, i)
    assert calls == []  # no time has passed: no fsync
    assert writer.records_since_fsync == 100

    clock.advance(1.0)
    writer.write(b"payload", 100, 100)
    assert len(calls) == 1
    assert writer.records_since_fsync == 0

    for i in range(101, 200):
        writer.write(b"payload", i, i)
    assert len(calls) == 1  # still inside the same interval

    clock.advance(2.5)
    writer.write(b"payload", 200, 200)
    assert len(calls) == 2
    writer.close()


def test_flushed_prefix_is_readable_before_close(tmp_path: Path) -> None:
    """The whole point of the fsync interval: data survives without a clean close."""
    clock = FakeClock()
    writer = RawLogWriter(tmp_path, fsync_interval_s=1.0, clock=clock)
    records = synth(50)
    for data, mono, wall in records:
        writer.write(data, mono, wall)
    clock.advance(1.0)
    writer.write(*synth(1, t0=99_000_000_000)[0])

    # Read the still-.open file without touching the writer.
    with RawLogReader(writer.path) as reader:
        got = list(reader)
    assert got[: len(records)] == records
    assert len(got) >= len(records)
    writer.close()


# ---- truncation and orphan recovery ---------------------------------------


def _abandon(writer: RawLogWriter) -> Path:
    """Simulate a crash: drop the file handle without close(), keeping the .open name."""
    path = writer.path
    writer._stream = None  # noqa: SLF001
    writer._fh.close()  # noqa: SLF001
    writer._fh = None  # noqa: SLF001
    return path


Records = list[tuple[bytes, int, int]]


def _make_open_capture(tmp_path: Path, n: int = 400) -> tuple[Path, Records, Records]:
    """A crashed .open capture with **two** flushed zstd frames plus a lost tail.

    Returns ``(path, flushed, first_frame)`` where ``flushed`` is everything that reached
    disk (both frames) and ``first_frame`` is what survives if the tail bytes are then
    mangled. That gap is the point of the fsync interval: at most one interval of data is
    ever at risk, and the loss is bounded at a frame boundary rather than arbitrary.
    """
    clock = FakeClock()
    writer = RawLogWriter(
        tmp_path, session_uid="crash", segment=0, fsync_interval_s=1.0, clock=clock
    )
    records = synth(n, size=200)
    a, b = n // 4, n // 2

    for data, mono, wall in records[:a]:
        writer.write(data, mono, wall)
    clock.advance(1.0)
    writer.write(*records[a])  # crosses the interval -> frame 1 = records[: a + 1]

    for data, mono, wall in records[a + 1 : b]:
        writer.write(data, mono, wall)
    clock.advance(1.0)
    writer.write(*records[b])  # frame 2 = records[a + 1 : b + 1]

    # Never flushed: still inside zstd's buffer when the process dies.
    for data, mono, wall in records[b + 1 :]:
        writer.write(data, mono, wall)

    return _abandon(writer), records[: b + 1], records[: a + 1]


def _handcraft(path: Path, records: list[tuple[bytes, int, int]], *, drop_tail: int) -> None:
    """Write a structurally perfect .f1raw whose final record is cut short by N bytes.

    The zstd stream is complete and well-formed; only the *record* stream inside it ends
    mid-way. This is the failure mode a truncated-then-salvaged file leaves behind, and
    it must not be confused with zstd corruption.
    """
    body = bytearray()
    for data, mono, wall in records:
        body += _RECORD.pack(mono, wall, len(data))
        body += data
    del body[len(body) - drop_tail :]
    meta = json.dumps({"capture_version": CAPTURE_VERSION, "host": "t"}).encode()
    stream = zstd.ZstdCompressor(level=3).compress(bytes(body))
    path.write_bytes(MAGIC + _META_LEN.pack(len(meta)) + meta + stream)


def test_reader_tolerates_truncation_mid_zstd_block(tmp_path: Path) -> None:
    path, flushed, first_frame = _make_open_capture(tmp_path)
    # Cut inside the second (last) frame: frame 1 must still decode in full.
    with open(path, "r+b") as fh:
        fh.truncate(path.stat().st_size - 5)
    with RawLogReader(path) as reader:
        got = list(reader)
        assert reader.truncated is True
    assert got == first_frame
    assert len(got) < len(flushed)


@pytest.mark.parametrize(
    ("drop_tail", "what"),
    [(150, "mid-payload"), (301, "mid-header"), (300, "record-boundary-minus-payload")],
)
def test_reader_tolerates_truncation_mid_record(tmp_path: Path, drop_tail: int, what: str) -> None:
    """Intact zstd, half a record: yield every whole record, then stop and flag it."""
    records = synth(20, size=300)
    path = tmp_path / f"cut-{what}.f1raw"
    _handcraft(path, records, drop_tail=drop_tail)
    with RawLogReader(path) as reader:
        got = list(reader)
        assert reader.truncated is True
    assert got == records[:19]


def test_reader_of_intact_handcrafted_file_is_not_truncated(tmp_path: Path) -> None:
    """Control for the test above: nothing dropped means no truncation flag."""
    records = synth(20, size=300)
    path = tmp_path / "whole.f1raw"
    _handcraft(path, records, drop_tail=0)
    with RawLogReader(path) as reader:
        assert list(reader) == records
        assert reader.truncated is False


def test_reader_tolerates_truncation_at_frame_boundary(tmp_path: Path) -> None:
    """Cut a finalized multi-frame file: the reader stops at the last intact record."""
    records = synth(200, size=300)
    writer = RawLogWriter(tmp_path, fsync_interval_s=0.0)  # flush -> a frame per write
    for data, mono, wall in records:
        writer.write(data, mono, wall)
    final = writer.close()

    full = final.read_bytes()
    cut = final.with_name("cut.f1raw")
    cut.write_bytes(full[: len(full) - 40])
    with RawLogReader(cut) as reader:
        got = list(reader)
        assert reader.truncated is True
    assert got == records[: len(got)]
    assert 0 < len(got) < len(records)


def test_reader_reads_open_files(tmp_path: Path) -> None:
    """The .open file of a live capture is readable while it is still being written."""
    path, flushed, _ = _make_open_capture(tmp_path, n=100)
    assert path.name.endswith(OPEN_SUFFIX)
    with RawLogReader(path) as reader:
        got = list(reader)
        assert reader.truncated is False
    assert got == flushed


def test_recover_orphans_finalizes_and_salvages(tmp_path: Path) -> None:
    path, flushed, first_frame = _make_open_capture(tmp_path, n=400)
    with open(path, "r+b") as fh:
        fh.truncate(path.stat().st_size - 7)  # mangle the trailing frame

    recovered = recover_orphans(tmp_path)
    assert len(recovered) == 1
    final = recovered[0]
    assert final.name.endswith(RAW_SUFFIX)
    assert not final.name.endswith(OPEN_SUFFIX)
    assert not path.exists()
    assert "_crash_0" in final.name

    # Salvage trims to the last complete frame, so the recovered file is clean:
    # re-reading it must not raise, and must not be flagged truncated.
    with RawLogReader(final) as reader:
        got = list(reader)
        assert reader.truncated is False
    assert got == first_frame
    assert len(got) < len(flushed)  # the mangled frame's records are genuinely gone

    # Idempotent: nothing left to recover on the next boot.
    assert recover_orphans(tmp_path) == []


def test_recover_orphans_handles_empty_dir_and_missing_dir(tmp_path: Path) -> None:
    assert recover_orphans(tmp_path / "nope") == []
    raw_dir(tmp_path).mkdir(parents=True)
    assert recover_orphans(tmp_path) == []


def test_recover_orphans_deletes_zero_byte_and_keeps_garbage(tmp_path: Path) -> None:
    directory = raw_dir(tmp_path)
    directory.mkdir(parents=True)
    empty = directory / "20260101T000000Z_x_0.f1raw.open"
    empty.write_bytes(b"")
    garbage = directory / "20260101T000001Z_x_0.f1raw.open"
    garbage.write_bytes(b"not a capture at all")

    assert recover_orphans(tmp_path) == []
    assert not empty.exists()
    assert garbage.exists()  # evidence, not data: left for a human


def test_recover_orphans_recovers_multiple(tmp_path: Path) -> None:
    for _ in range(3):
        _make_open_capture(tmp_path, n=120)
    recovered = recover_orphans(tmp_path)
    assert len(recovered) == 3
    assert all(p.exists() for p in recovered)
    assert not list(raw_dir(tmp_path).glob(f"*{OPEN_SUFFIX}"))


def test_recover_orphans_of_cleanly_closed_open_file(tmp_path: Path) -> None:
    """A .open file that happens to be complete recovers with zero loss."""
    writer = RawLogWriter(tmp_path, session_uid="clean", segment=0)
    records = synth(50)
    for data, mono, wall in records:
        writer.write(data, mono, wall)
    writer.flush()
    open_path = _abandon(writer)  # crash after a clean flush
    assert open_path.exists()

    recovered = recover_orphans(tmp_path)
    assert len(recovered) == 1
    with RawLogReader(recovered[0]) as reader:
        assert list(reader) == records
        assert reader.truncated is False
