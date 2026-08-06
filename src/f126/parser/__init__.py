"""Packet parser: raw UDP datagrams -> the normalized views in ``types.py``.

Public API is :class:`PacketParser`. One instance handles both wire formats
simultaneously -- dispatch is per packet, on the leading uint16, so a 2025
datagram and a 2026 datagram can arrive back to back.

The parser never raises out of :meth:`PacketParser.parse`. A malformed,
truncated, unknown or hostile datagram increments a counter and yields
``None``; capture must not stop because the game shipped a new packet id.
"""

from __future__ import annotations

from typing import Any

from ..types import PacketId, ParsedPacket, ParseError
from .decoders import (
    Extras2026,
    decode_car_telemetry_2,
    decode_classification,
    decode_damage,
    decode_event,
    decode_history,
    decode_lap,
    decode_motion,
    decode_participants,
    decode_session,
    decode_status,
    decode_telemetry,
    decode_time_trial,
)
from .enums import track_name
from .header import HEADER_SIZE, SUPPORTED_FORMATS, decode_header, peek_packet_id, spec_for
from .spec_2025 import SPEC_2025
from .spec_2026 import SPEC_2026

__all__ = [
    "HEADER_SIZE",
    "SKIPPED_PACKET_IDS",
    "SPEC_2025",
    "SPEC_2026",
    "SUPPORTED_FORMATS",
    "PacketParser",
    "track_name",
]

#: Packet types decoded far enough to be counted, then deliberately dropped.
#: Nothing downstream consumes them and they are a large share of the byte
#: volume (CarSetups and LapPositions especially).
SKIPPED_PACKET_IDS = frozenset(
    {
        PacketId.CAR_SETUPS,  # 5
        PacketId.LOBBY_INFO,  # 9
        PacketId.TYRE_SETS,  # 12
        PacketId.MOTION_EX,  # 13
        PacketId.LAP_POSITIONS,  # 15
    }
)


class PacketParser:
    """Stateful decoder for one UDP stream.

    The only state is a small per-session merge cache: on format 2026 the
    ``TelemetryView`` fields ``aero_mode``, ``energy_store_j`` and
    ``energy_deploy_mode`` do not live in the CarTelemetry packet -- they
    arrive on CarTelemetry2 (id 16) and CarStatus (id 7). Those two packets
    update the cache and CarTelemetry (id 6) reads it, so the view stays whole.
    The cache is cleared whenever ``session_uid`` changes.
    """

    __slots__ = ("_extras", "_session_uid", "counters")

    def __init__(self) -> None:
        self.counters: dict[str, Any] = {
            "parsed_total": 0,
            "skipped_total": 0,
            "errors_total": 0,
            "unknown_packet_id_total": 0,
            "size_mismatch_total": 0,
            "unknown_format_total": 0,
            "by_packet_id": {
                "parsed": {},
                "skipped": {},
                "errors": {},
            },
        }
        self._extras = Extras2026()
        self._session_uid: int | None = None

    # -- counter helpers ---------------------------------------------------

    def _bump(self, bucket: str, packet_id: int | None) -> None:
        self.counters[f"{bucket}_total"] += 1
        if packet_id is not None:
            by_id: dict[int, int] = self.counters["by_packet_id"][bucket]
            by_id[packet_id] = by_id.get(packet_id, 0) + 1

    def _error(self, packet_id: int | None, kind: str | None = None) -> None:
        self._bump("errors", packet_id)
        if kind is not None:
            self.counters[f"{kind}_total"] += 1

    # -- main entry point --------------------------------------------------

    def parse(
        self, data: bytes, recv_monotonic_ns: int, recv_wall_ns: int
    ) -> ParsedPacket | None:
        """Decode one datagram.

        Returns ``None`` for a deliberately skipped packet type and for any
        datagram that could not be decoded; the counters distinguish the two.
        Never raises.
        """
        try:
            return self._parse(data, recv_monotonic_ns, recv_wall_ns)
        except Exception:
            # Last-resort net: a decoder bug or an unforeseen struct edge case
            # must degrade to a counted error, never take down the capture loop.
            self._error(peek_packet_id(data))
            return None

    def _parse(
        self, data: bytes, recv_monotonic_ns: int, recv_wall_ns: int
    ) -> ParsedPacket | None:
        try:
            header = decode_header(data)
        except ParseError:
            self._error(None)
            return None

        packet_id = header.packet_id
        spec = spec_for(header.packet_format)
        if spec is None:
            self._error(packet_id, "unknown_format")
            return None

        # Skipped types are counted before any size check: they are known-good
        # packets we choose not to decode, so a layout surprise in one of them
        # must not show up as an error.
        if packet_id in SKIPPED_PACKET_IDS:
            self._bump("skipped", packet_id)
            return None

        expected = spec.sizes.get(packet_id)
        if expected is None:
            self._error(packet_id, "unknown_packet_id")
            return None
        if len(data) != expected:
            self._error(packet_id, "size_mismatch")
            return None

        if self._session_uid != header.session_uid:
            self._session_uid = header.session_uid
            self._extras = Extras2026()

        try:
            view = self._decode(spec, data, header, packet_id)
        except ParseError:
            self._error(packet_id)
            return None

        self._bump("parsed", packet_id)
        return ParsedPacket(
            header=header,
            view=view,
            recv_monotonic_ns=recv_monotonic_ns,
            recv_wall_ns=recv_wall_ns,
        )

    def _decode(self, spec, data: bytes, header, packet_id: int):
        """Route to the right decoder, applying the 2026 cross-packet merges."""
        match packet_id:
            case PacketId.MOTION:
                return decode_motion(spec, data, header)
            case PacketId.SESSION:
                return decode_session(spec, data, header)
            case PacketId.LAP_DATA:
                return decode_lap(spec, data, header)
            case PacketId.EVENT:
                return decode_event(spec, data, header)
            case PacketId.PARTICIPANTS:
                return decode_participants(spec, data, header)
            case PacketId.CAR_TELEMETRY:
                return decode_telemetry(spec, data, header, self._extras)
            case PacketId.CAR_STATUS:
                result = decode_status(spec, data, header)
                if spec.packet_format == 2026:
                    self._extras.energy_store_j = result.ers_store_j
                    self._extras.energy_deploy_mode = result.ers_deploy_mode
                return result.view
            case PacketId.FINAL_CLASSIFICATION:
                return decode_classification(spec, data, header)
            case PacketId.CAR_DAMAGE:
                return decode_damage(spec, data, header)
            case PacketId.SESSION_HISTORY:
                return decode_history(spec, data, header)
            case PacketId.TIME_TRIAL:
                return decode_time_trial(spec, data, header)
            case PacketId.CAR_TELEMETRY_2:
                # Feeds TelemetryView via the merge cache; emits no view of its
                # own so the telemetry stream stays at one view per frame.
                self._extras.aero_mode = decode_car_telemetry_2(
                    spec, data, header
                ).aero_mode
                return None
            case _:  # pragma: no cover - guarded by the sizes lookup above
                return None
