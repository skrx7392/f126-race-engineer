"""The .f1raw write-ahead log: every datagram lands here before anything parses it.

DOCTRINE: the raw log is the source of truth. Parsing, state, and the database are all
derived artefacts that can be rebuilt by replaying a .f1raw. Therefore this module keeps
no schema knowledge of F1 packets whatsoever — it stores opaque byte blobs plus the two
timestamps taken the instant the datagram was received.

FILE LAYOUT (frozen — do not change without bumping ``capture_version``)::

    offset 0   b"F1RAW\\x01"                 6 bytes, magic + format byte
    offset 6   uint32 LE meta_len            length of the JSON metadata blob
    offset 10  meta_len bytes                UTF-8 JSON metadata (see below)
    offset 10+meta_len ...                   zstd stream (level 3) of records

    metadata JSON: {"capture_version": 1, "host": str, "started_wall_ns": int,
                    "started_monotonic_ns": int, "session_uid": str, "segment": int}

``session_uid`` is stored as a *string* — the same token that appears in the filename, so
the two can never disagree — and is ``"unknown"`` until the state layer learns the real
UID from a Session packet. It is a u64 on the wire, which exceeds the exact-integer range
of every JSON reader that uses doubles; ``int(meta["session_uid"])`` recovers it.

    each record inside the zstd stream:
        uint64 LE recv_monotonic_ns
        uint64 LE recv_wall_ns
        uint16 LE payload_len
        payload_len bytes of raw datagram

The zstd stream is written as a sequence of *complete frames*: every periodic flush ends
the current frame (``FLUSH_FRAME``) and the next write opens a new one. That costs a few
bytes of frame header per flush interval and buys the property that matters here — a file
whose tail was lost to a power cut still decodes cleanly up to the last flush, with the
partial trailing frame simply discarded. ``RawLogReader`` walks frames explicitly so it
can report both *where* the salvageable prefix ends and *that* the tail was lost.

FILE NAMING: ``{started_wall_iso}_{session_uid}_{segment}.f1raw``, carrying the suffix
``.open`` while being written and renamed to the final name on ``close()``. The ISO
timestamp uses the ISO 8601 *basic* UTC form (``20260806T142530Z``) rather than the
extended form, because colons are not portable in filenames.

THREADING: this module is synchronous and does blocking I/O (including ``fsync``).
Callers on the asyncio loop must drive it from a worker thread (``asyncio.to_thread``)
or a dedicated writer thread; a single writer instance is not safe to share between
threads without external locking.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

import zstandard as zstd

MAGIC = b"F1RAW\x01"
CAPTURE_VERSION = 1
RAW_SUFFIX = ".f1raw"
OPEN_SUFFIX = ".open"

DEFAULT_LEVEL = 3
DEFAULT_FSYNC_INTERVAL_S = 1.0
DEFAULT_READ_SIZE = 256 * 1024
UNKNOWN_SESSION_UID = "unknown"

#: Largest payload a record can carry (the length field is uint16).
MAX_PAYLOAD = 0xFFFF

#: Sanity bound on the metadata blob, so a corrupt length field cannot force a huge alloc.
MAX_META_BYTES = 1 << 20

_META_LEN = struct.Struct("<I")
_RECORD_HEADER = struct.Struct("<QQH")
RECORD_HEADER_SIZE = _RECORD_HEADER.size  # 18


class RawLogError(Exception):
    """Raised when a file is not a readable .f1raw at all (bad magic / bad metadata)."""


@dataclass(slots=True, frozen=True)
class RawRecord:
    """One captured datagram. Iterating a reader yields plain tuples of these fields."""

    data: bytes
    recv_monotonic_ns: int
    recv_wall_ns: int


def _iso_basic(wall_ns: int) -> str:
    return datetime.fromtimestamp(wall_ns / 1e9, UTC).strftime("%Y%m%dT%H%M%SZ")


def _uid_token(session_uid: int | str | None) -> str:
    """Filename-safe rendering of a session UID (u64, or the boot-time placeholder)."""
    if session_uid is None:
        return UNKNOWN_SESSION_UID
    token = str(session_uid).strip()
    if not token:
        return UNKNOWN_SESSION_UID
    safe = "".join(ch if (ch.isalnum() or ch in "-") else "-" for ch in token)
    return safe or UNKNOWN_SESSION_UID


def _unique_path(path: Path, *, ignore: Path | None = None) -> Path:
    """Return ``path``, or ``path`` with a ``-N`` disambiguator if the name is taken.

    Two segments of the same session can legitimately start inside the same wall-clock
    second (e.g. a flashback right after a rotation); we never overwrite a capture. A
    name is "taken" if either the final file *or* an in-progress ``.open`` file with that
    name exists — otherwise two concurrent writers would race for the same final name.
    ``ignore`` exempts one ``.open`` path: the caller's own file, which is about to be
    renamed onto the name being tested.
    """

    def taken(candidate: Path) -> bool:
        if candidate.exists():
            return True
        sibling = candidate.with_name(candidate.name + OPEN_SUFFIX)
        return sibling != ignore and sibling.exists()

    if not taken(path):
        return path
    stem = path.name.removesuffix(RAW_SUFFIX)
    for n in range(1, 10_000):
        candidate = path.with_name(f"{stem}-{n}{RAW_SUFFIX}")
        if not taken(candidate):
            return candidate
    raise RawLogError(f"cannot find a free filename near {path}")


def raw_dir(data_dir: str | os.PathLike[str]) -> Path:
    """``{data_dir}/raw`` — where every capture lives."""
    return Path(data_dir) / "raw"


class RawLogWriter:
    """Append-only writer for a single .f1raw file, with rotation.

    The writer opens its first file eagerly in ``__init__`` so that a capture started
    before any Session packet has arrived still lands on disk; that first file carries
    ``session_uid="unknown"`` until the state layer calls :meth:`rotate`.
    """

    __slots__ = (
        "_clock",
        "_compressor",
        "_fh",
        "_final_path",
        "_fsync_interval_s",
        "_host",
        "_last_fsync",
        "_level",
        "_meta",
        "_path",
        "_raw_dir",
        "_segment",
        "_session_uid",
        "_stream",
        "bytes_written",
        "packets_written",
        "records_since_fsync",
    )

    def __init__(
        self,
        data_dir: str | os.PathLike[str],
        *,
        session_uid: int | str | None = UNKNOWN_SESSION_UID,
        segment: int = 0,
        level: int = DEFAULT_LEVEL,
        fsync_interval_s: float = DEFAULT_FSYNC_INTERVAL_S,
        host: str | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._raw_dir = raw_dir(data_dir)
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        self._level = level
        self._fsync_interval_s = fsync_interval_s
        self._host = host if host is not None else socket.gethostname()
        self._clock: Callable[[], float] = clock or time.monotonic

        self._fh: BinaryIO | None = None
        self._stream: zstd.ZstdCompressionWriter | None = None
        self._compressor: zstd.ZstdCompressor | None = None
        self._path: Path | None = None
        self._final_path: Path | None = None
        self._meta: dict[str, object] = {}
        self._session_uid: str = _uid_token(session_uid)
        self._segment: int = segment
        self._last_fsync: float = 0.0

        self.bytes_written: int = 0
        self.packets_written: int = 0
        self.records_since_fsync: int = 0

        self._open(session_uid, segment)

    # ---- properties -------------------------------------------------------

    @property
    def path(self) -> Path:
        """Path currently being written (carries the ``.open`` suffix)."""
        assert self._path is not None
        return self._path

    @property
    def final_path(self) -> Path:
        """Path this file will be renamed to on :meth:`close`."""
        assert self._final_path is not None
        return self._final_path

    @property
    def meta(self) -> dict[str, object]:
        return dict(self._meta)

    @property
    def session_uid(self) -> str:
        return self._session_uid

    @property
    def segment(self) -> int:
        return self._segment

    @property
    def closed(self) -> bool:
        return self._fh is None

    @property
    def compressed_bytes(self) -> int:
        """Bytes on disk so far (header + metadata + zstd stream flushed to the file)."""
        if self._fh is None:
            if self._final_path is not None and self._final_path.exists():
                return self._final_path.stat().st_size
            return 0
        return self._fh.tell()

    # ---- lifecycle --------------------------------------------------------

    def _open(self, session_uid: int | str | None, segment: int) -> None:
        started_wall_ns = time.time_ns()
        started_monotonic_ns = time.monotonic_ns()
        self._session_uid = _uid_token(session_uid)
        self._segment = int(segment)

        name = f"{_iso_basic(started_wall_ns)}_{self._session_uid}_{self._segment}{RAW_SUFFIX}"
        final_path = _unique_path(self._raw_dir / name)
        open_path = final_path.with_name(final_path.name + OPEN_SUFFIX)

        self._meta = {
            "capture_version": CAPTURE_VERSION,
            "host": self._host,
            "started_wall_ns": started_wall_ns,
            "started_monotonic_ns": started_monotonic_ns,
            "session_uid": self._session_uid,
            "segment": self._segment,
        }
        meta_blob = json.dumps(self._meta, separators=(",", ":")).encode("utf-8")

        # noqa SIM115: the handle deliberately outlives this call — the writer owns it
        # for the lifetime of the file and closes it in close()/rotate().
        fh = open(open_path, "wb")  # noqa: SIM115
        try:
            fh.write(MAGIC)
            fh.write(_META_LEN.pack(len(meta_blob)))
            fh.write(meta_blob)
            fh.flush()
            os.fsync(fh.fileno())
            self._compressor = zstd.ZstdCompressor(level=self._level)
            self._stream = self._compressor.stream_writer(fh, closefd=False)
        except BaseException:
            fh.close()
            open_path.unlink(missing_ok=True)
            raise

        self._fh = fh
        self._path = open_path
        self._final_path = final_path
        self._last_fsync = self._clock()
        self.records_since_fsync = 0
        _fsync_dir(self._raw_dir)

    def write(self, data: bytes, mono_ns: int, wall_ns: int) -> None:
        """Append one datagram. Flushes + fsyncs if the fsync interval has elapsed."""
        if self._fh is None or self._stream is None:
            raise RawLogError("writer is closed")
        n = len(data)
        if n > MAX_PAYLOAD:
            raise ValueError(f"payload of {n} bytes exceeds the {MAX_PAYLOAD}-byte record limit")
        self._stream.write(_RECORD_HEADER.pack(mono_ns, wall_ns, n))
        self._stream.write(data)
        self.bytes_written += n
        self.packets_written += 1
        self.records_since_fsync += 1
        if self._clock() - self._last_fsync >= self._fsync_interval_s:
            self.flush()

    def flush(self) -> None:
        """End the current zstd frame and fsync, making everything so far recoverable."""
        if self._fh is None or self._stream is None:
            return
        self._stream.flush(zstd.FLUSH_FRAME)
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._last_fsync = self._clock()
        self.records_since_fsync = 0

    def rotate(self, session_uid: int | str | None, segment: int) -> Path:
        """Close the current file and start a new one. Returns the finalized old path."""
        finalized = self.close()
        self._open(session_uid, segment)
        return finalized

    def close(self) -> Path:
        """Finish the zstd stream, fsync, and rename ``.open`` -> final name."""
        if self._fh is None or self._stream is None:
            assert self._final_path is not None
            return self._final_path
        stream, fh = self._stream, self._fh
        path, final_path = self.path, self.final_path
        self._stream = None
        self._fh = None
        try:
            stream.close()
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fh.close()
        final_path = _unique_path(final_path, ignore=path)
        os.replace(path, final_path)
        _fsync_dir(self._raw_dir)
        self._final_path = final_path
        self._path = final_path
        return final_path

    def __enter__(self) -> RawLogWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a rename/create survives a crash. Best effort."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class RawLogReader:
    """Streaming reader for a .f1raw file (``.open`` or finalized).

    Iterating yields ``(data, recv_monotonic_ns, recv_wall_ns)`` tuples — the exact shape
    the UDP listener puts on the pipeline queue. A truncated tail (partial zstd frame
    and/or partial record) stops iteration cleanly and sets :attr:`truncated`.
    """

    __slots__ = (
        "_data_start",
        "_dctx",
        "_fh",
        "_read_size",
        "compressed_pos",
        "meta",
        "path",
        "salvage_offset",
        "size",
        "truncated",
    )

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        read_size: int = DEFAULT_READ_SIZE,
    ) -> None:
        self.path = Path(path)
        self._read_size = read_size
        self.truncated: bool = False
        self.size: int = self.path.stat().st_size

        # noqa SIM115: streaming reader — the handle is owned until close()/__exit__.
        fh = open(self.path, "rb")  # noqa: SIM115
        try:
            magic = fh.read(len(MAGIC))
            if magic != MAGIC:
                raise RawLogError(f"{self.path}: bad magic {magic!r} (expected {MAGIC!r})")
            raw_len = fh.read(_META_LEN.size)
            if len(raw_len) != _META_LEN.size:
                raise RawLogError(f"{self.path}: truncated before metadata length")
            (meta_len,) = _META_LEN.unpack(raw_len)
            if meta_len > MAX_META_BYTES:
                # Corrupt length field: refuse rather than trying to allocate 4 GiB.
                raise RawLogError(f"{self.path}: implausible metadata length {meta_len}")
            meta_blob = fh.read(meta_len)
            if len(meta_blob) != meta_len:
                raise RawLogError(f"{self.path}: truncated metadata blob")
            try:
                meta = json.loads(meta_blob.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RawLogError(f"{self.path}: unreadable metadata: {exc}") from exc
            if not isinstance(meta, dict):
                raise RawLogError(f"{self.path}: metadata is not a JSON object")
        except BaseException:
            fh.close()
            raise

        self.meta: dict[str, object] = meta
        self._fh: BinaryIO = fh
        self._dctx = zstd.ZstdDecompressor()
        self._data_start = fh.tell()
        #: Absolute file offset of the end of the last *complete* zstd frame.
        self.salvage_offset: int = self._data_start
        #: Compressed bytes fed to the decompressor so far (for progress reporting).
        self.compressed_pos: int = self._data_start

    @property
    def capture_version(self) -> int:
        value = self.meta.get("capture_version", 0)
        return value if isinstance(value, int) else 0

    def _chunks(self) -> Iterator[bytes]:
        """Yield decompressed chunks, walking zstd frames so boundaries stay visible."""
        obj = self._dctx.decompressobj()
        pending = b""
        frame_bytes = 0  # compressed bytes fed into the current (incomplete) frame
        while True:
            if not pending:
                chunk = self._fh.read(self._read_size)
                if not chunk:
                    break
                pending = chunk
            try:
                out = obj.decompress(pending)
            except zstd.ZstdError:
                self.truncated = True
                break
            if obj.eof:
                used = len(pending) - len(obj.unused_data)
                frame_bytes += used
                self.salvage_offset += frame_bytes
                self.compressed_pos = self.salvage_offset
                frame_bytes = 0
                pending = obj.unused_data
                obj = self._dctx.decompressobj()
            else:
                frame_bytes += len(pending)
                self.compressed_pos = self.salvage_offset + frame_bytes
                pending = b""
            if out:
                yield out
        if frame_bytes or pending:
            # Bytes fed into a frame that never terminated: the tail was lost.
            self.truncated = True

    def __iter__(self) -> Iterator[tuple[bytes, int, int]]:
        buf = bytearray()
        for chunk in self._chunks():
            buf += chunk
            pos = 0
            end = len(buf)
            while end - pos >= RECORD_HEADER_SIZE:
                mono_ns, wall_ns, n = _RECORD_HEADER.unpack_from(buf, pos)
                if end - pos - RECORD_HEADER_SIZE < n:
                    break
                start = pos + RECORD_HEADER_SIZE
                yield bytes(buf[start : start + n]), mono_ns, wall_ns
                pos = start + n
            if pos:
                del buf[:pos]
        if buf:
            # A record header or payload was cut in half by the lost tail.
            self.truncated = True

    def records(self) -> Iterator[RawRecord]:
        """Same iteration, boxed into :class:`RawRecord` for readability."""
        for data, mono_ns, wall_ns in self:
            yield RawRecord(data, mono_ns, wall_ns)

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> RawLogReader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def recover_orphans(data_dir: str | os.PathLike[str]) -> list[Path]:
    """Finalize any ``*.f1raw.open`` files left behind by a crash. Call this at boot.

    For each orphan: decode as far as the zstd stream allows, truncate the file to the
    end of the last complete frame if the tail was lost, then rename to the final name.
    Zero-length orphans (crash between create and first write) are deleted. Files whose
    header is unreadable are left alone — they are evidence, not data.

    Returns the finalized paths, oldest first.
    """
    directory = raw_dir(data_dir)
    if not directory.is_dir():
        return []

    recovered: list[Path] = []
    for orphan in sorted(directory.glob(f"*{RAW_SUFFIX}{OPEN_SUFFIX}")):
        try:
            if orphan.stat().st_size == 0:
                orphan.unlink(missing_ok=True)
                continue
        except OSError:
            continue

        try:
            with RawLogReader(orphan) as reader:
                for _ in reader:
                    pass
                truncated = reader.truncated
                salvage_offset = reader.salvage_offset
        except RawLogError:
            continue  # not a readable capture; leave it for a human
        except OSError:
            continue

        try:
            if truncated and salvage_offset < orphan.stat().st_size:
                with open(orphan, "r+b") as fh:
                    fh.truncate(salvage_offset)
                    fh.flush()
                    os.fsync(fh.fileno())
            final_path = _unique_path(
                orphan.with_name(orphan.name.removesuffix(OPEN_SUFFIX)), ignore=orphan
            )
            os.replace(orphan, final_path)
        except OSError:
            continue
        recovered.append(final_path)

    _fsync_dir(directory)
    return recovered
