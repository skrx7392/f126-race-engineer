"""Packet header decode and per-packet format dispatch.

The header is 29 bytes and byte-identical in formats 2025 and 2026, which is
what makes per-packet dispatch possible: the leading uint16 names the format,
so a single parser instance can interleave both without any handshake or
sticky state.
"""

from __future__ import annotations

from ..types import PacketHeader, ParseError
from .spec_2025 import HEADER_FIELDS, HEADER_SIZE, SPEC_2025, FormatSpec, table
from .spec_2026 import SPEC_2026

__all__ = [
    "HEADER_SIZE",
    "SPECS",
    "SUPPORTED_FORMATS",
    "decode_header",
    "peek_packet_format",
    "peek_packet_id",
    "spec_for",
]

_HEADER = table(HEADER_FIELDS)
_UNPACK = _HEADER.one.unpack_from

assert _HEADER.size == HEADER_SIZE, "packet header must be 29 bytes"

#: Wire format id -> layout tables. Keys are the values seen in the leading
#: uint16 of every packet.
SPECS: dict[int, FormatSpec] = {
    SPEC_2025.packet_format: SPEC_2025,
    SPEC_2026.packet_format: SPEC_2026,
}

SUPPORTED_FORMATS = frozenset(SPECS)

#: Byte offset of ``packet_id`` inside the header.
_PACKET_ID_OFFSET = 6


def peek_packet_format(data: bytes) -> int | None:
    """Read the leading uint16 format id without decoding the rest.

    Returns ``None`` when ``data`` is too short to contain a header at all.
    """
    if len(data) < HEADER_SIZE:
        return None
    return int.from_bytes(data[:2], "little")


def peek_packet_id(data: bytes) -> int | None:
    """Read ``packet_id`` without decoding the rest. ``None`` if too short."""
    if len(data) < HEADER_SIZE:
        return None
    return data[_PACKET_ID_OFFSET]


def spec_for(packet_format: int) -> FormatSpec | None:
    """Layout tables for ``packet_format``, or ``None`` if unsupported."""
    return SPECS.get(packet_format)


def decode_header(data: bytes) -> PacketHeader:
    """Decode the 29-byte header.

    Raises :class:`ParseError` if ``data`` is shorter than the header. The
    game-major/minor version bytes are read (they are part of the layout) but
    dropped, since ``types.PacketHeader`` does not carry them.
    """
    if len(data) < HEADER_SIZE:
        raise ParseError(
            packet_id=None,
            reason=f"datagram of {len(data)}B is shorter than the {HEADER_SIZE}B header",
        )
    (
        packet_format,
        game_year,
        _game_major_version,
        _game_minor_version,
        packet_version,
        packet_id,
        session_uid,
        session_time,
        frame_identifier,
        overall_frame_identifier,
        player_car_index,
        secondary_player_car_index,
    ) = _UNPACK(data, 0)
    return PacketHeader(
        packet_format=packet_format,
        game_year=game_year,
        packet_version=packet_version,
        packet_id=packet_id,
        session_uid=session_uid,
        session_time=session_time,
        frame_identifier=frame_identifier,
        overall_frame_identifier=overall_frame_identifier,
        player_car_index=player_car_index,
        secondary_player_car_index=secondary_player_car_index,
    )
