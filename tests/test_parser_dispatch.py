"""Per-packet format dispatch, counter bookkeeping, and never-crash guarantees.

``PacketParser.parse`` is the only thing standing between a hostile or
malformed datagram and the capture loop, so the contract is absolute: it
returns ``None`` and increments a counter, and it never raises.
"""

from __future__ import annotations

import struct

import pytest
from builders import (
    BUILDERS,
    build_car_status,
    build_car_telemetry,
    build_event,
    build_motion,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from f126.parser import SKIPPED_PACKET_IDS, PacketParser
from f126.parser.header import HEADER_SIZE
from f126.parser.spec_2025 import SPEC_2025
from f126.parser.spec_2026 import SPEC_2026

RECV_MONO = 1_000
RECV_WALL = 2_000

FORMATS = (2025, 2026)
SPECS = {2025: SPEC_2025, 2026: SPEC_2026}
DECODED_IDS = {
    fmt: sorted(set(SPECS[fmt].sizes) - SKIPPED_PACKET_IDS) for fmt in FORMATS
}


@pytest.fixture
def parser() -> PacketParser:
    return PacketParser()


def build(packet_format: int, packet_id: int, **kwargs) -> bytes:
    return BUILDERS[packet_id](packet_format, **kwargs)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def test_2025_then_2026_decode_in_one_parser(parser: PacketParser) -> None:
    """The two formats interleave with no handshake and no sticky state."""
    packet_2025 = parser.parse(
        build_car_telemetry(2025, player_car_index=3, cars={3: {"speed": 111, "drs": 1}}),
        RECV_MONO,
        RECV_WALL,
    )
    packet_2026 = parser.parse(
        build_car_telemetry(2026, player_car_index=21, cars={21: {"speed": 222}}),
        RECV_MONO,
        RECV_WALL,
    )
    assert packet_2025 is not None and packet_2026 is not None

    assert packet_2025.header.packet_format == 2025
    assert packet_2025.view.speed_kmh == pytest.approx(111.0)
    assert len(packet_2025.view.opponent_speeds_kmh) == 22
    assert packet_2025.view.drs_open is True
    assert packet_2025.view.aero_mode is None

    assert packet_2026.header.packet_format == 2026
    assert packet_2026.view.speed_kmh == pytest.approx(222.0)
    assert len(packet_2026.view.opponent_speeds_kmh) == 24
    assert packet_2026.view.drs_open is None

    assert parser.counters["errors_total"] == 0
    assert parser.counters["parsed_total"] == 2


def test_alternating_formats_stay_correct(parser: PacketParser) -> None:
    for _ in range(5):
        for fmt, expected_cars in ((2025, 22), (2026, 24)):
            packet = parser.parse(
                build_car_telemetry(fmt, player_car_index=0, cars={0: {"speed": 100}}),
                RECV_MONO,
                RECV_WALL,
            )
            assert packet is not None
            assert len(packet.view.opponent_speeds_kmh) == expected_cars
    assert parser.counters["errors_total"] == 0
    assert parser.counters["parsed_total"] == 10


@pytest.mark.parametrize("packet_format", FORMATS)
def test_every_decoded_packet_id_round_trips(
    parser: PacketParser, packet_format: int
) -> None:
    for packet_id in DECODED_IDS[packet_format]:
        data = build(packet_format, packet_id)
        assert len(data) == SPECS[packet_format].sizes[packet_id]
        packet = parser.parse(data, RECV_MONO, RECV_WALL)
        assert packet is not None, f"format {packet_format} id {packet_id} failed"
        assert packet.header.packet_id == packet_id
    assert parser.counters["errors_total"] == 0


# --------------------------------------------------------------------------
# Skipped packet types
# --------------------------------------------------------------------------


@pytest.mark.parametrize("packet_format", FORMATS)
@pytest.mark.parametrize("packet_id", sorted(SKIPPED_PACKET_IDS))
def test_skipped_types_return_none_and_are_counted(
    parser: PacketParser, packet_format: int, packet_id: int
) -> None:
    data = build(packet_format, packet_id)
    assert parser.parse(data, RECV_MONO, RECV_WALL) is None
    assert parser.counters["skipped_total"] == 1
    assert parser.counters["errors_total"] == 0
    assert parser.counters["by_packet_id"]["skipped"] == {packet_id: 1}


def test_skipped_ids_are_exactly_the_documented_set() -> None:
    assert set(SKIPPED_PACKET_IDS) == {5, 9, 12, 13, 15}


@pytest.mark.parametrize("packet_id", sorted(SKIPPED_PACKET_IDS))
def test_skipped_types_never_error_even_at_wrong_size(
    parser: PacketParser, packet_id: int
) -> None:
    """Skipping is decided before the size check on purpose: a layout surprise
    in a packet we do not read must not be reported as a decode failure."""
    data = build(2026, packet_id)[:-7]
    assert parser.parse(data, RECV_MONO, RECV_WALL) is None
    assert parser.counters["skipped_total"] == 1
    assert parser.counters["errors_total"] == 0


# --------------------------------------------------------------------------
# Unknown ids and formats
# --------------------------------------------------------------------------


@pytest.mark.parametrize("packet_id", [17, 20, 99, 255])
@pytest.mark.parametrize("packet_format", FORMATS)
def test_unknown_packet_id_is_counted(
    parser: PacketParser, packet_format: int, packet_id: int
) -> None:
    data = bytearray(build(packet_format, 0))
    data[6] = packet_id
    assert parser.parse(bytes(data), RECV_MONO, RECV_WALL) is None
    assert parser.counters["unknown_packet_id_total"] == 1
    assert parser.counters["errors_total"] == 1
    assert parser.counters["by_packet_id"]["errors"] == {packet_id: 1}


def test_car_telemetry_2_is_unknown_on_2025(parser: PacketParser) -> None:
    """id 16 only exists in the 2026 Season Pack."""
    data = build(2025, 16)
    assert parser.parse(data, RECV_MONO, RECV_WALL) is None
    assert parser.counters["unknown_packet_id_total"] == 1
    assert parser.counters["errors_total"] == 1


@pytest.mark.parametrize("packet_format", [1999, 2024, 2027, 0, 65535])
def test_unknown_packet_format_is_counted(
    parser: PacketParser, packet_format: int
) -> None:
    data = bytearray(build_motion(2026))
    struct.pack_into("<H", data, 0, packet_format)
    assert parser.parse(bytes(data), RECV_MONO, RECV_WALL) is None
    assert parser.counters["unknown_format_total"] == 1
    assert parser.counters["errors_total"] == 1
    assert parser.counters["parsed_total"] == 0


# --------------------------------------------------------------------------
# Truncation and wrong lengths
# --------------------------------------------------------------------------


@pytest.mark.parametrize("packet_format", FORMATS)
def test_truncation_at_every_8_byte_boundary(
    parser: PacketParser, packet_format: int
) -> None:
    for packet_id in DECODED_IDS[packet_format]:
        data = build(packet_format, packet_id)
        for cut in range(0, len(data), 8):
            truncated = data[:cut]
            assert parser.parse(truncated, RECV_MONO, RECV_WALL) is None, (
                f"format {packet_format} id {packet_id} decoded at {cut}B"
            )
    assert parser.counters["parsed_total"] == 0
    assert parser.counters["errors_total"] > 0


@pytest.mark.parametrize("packet_format", FORMATS)
def test_short_datagrams_below_header_size(
    parser: PacketParser, packet_format: int
) -> None:
    for length in range(HEADER_SIZE):
        assert parser.parse(b"\x00" * length, RECV_MONO, RECV_WALL) is None
    assert parser.counters["errors_total"] == HEADER_SIZE
    # Nothing can be attributed to a packet id when the header is incomplete.
    assert parser.counters["by_packet_id"]["errors"] == {}


@pytest.mark.parametrize("packet_format", FORMATS)
@pytest.mark.parametrize("delta", [-100, -8, -1, 1, 8, 100])
def test_wrong_length_counts_as_size_mismatch(
    parser: PacketParser, packet_format: int, delta: int
) -> None:
    expected_mismatch = 0
    for packet_id in DECODED_IDS[packet_format]:
        data = build(packet_format, packet_id)
        wrong = data[:delta] if delta < 0 else data + b"\x00" * delta
        assert parser.parse(wrong, RECV_MONO, RECV_WALL) is None
        # Trimming 100 B takes the small packets (Event 45 B, TimeTrial ~101 B)
        # below the header, where there is no packet id to blame it on.
        if len(wrong) >= HEADER_SIZE:
            expected_mismatch += 1
    assert parser.counters["size_mismatch_total"] == expected_mismatch
    assert parser.counters["errors_total"] == len(DECODED_IDS[packet_format])
    assert parser.counters["parsed_total"] == 0


def test_packet_of_another_formats_size_is_rejected(parser: PacketParser) -> None:
    """A 2026-sized Motion packet claiming format 2025 must not be decoded."""
    data = bytearray(build_motion(2026))
    struct.pack_into("<H", data, 0, 2025)
    assert parser.parse(bytes(data), RECV_MONO, RECV_WALL) is None
    assert parser.counters["size_mismatch_total"] == 1


@pytest.mark.parametrize("packet_format", FORMATS)
def test_out_of_range_player_car_index_is_an_error(
    parser: PacketParser, packet_format: int
) -> None:
    data = bytearray(build_car_telemetry(packet_format))
    data[27] = 200  # player_car_index
    assert parser.parse(bytes(data), RECV_MONO, RECV_WALL) is None
    assert parser.counters["errors_total"] == 1
    assert parser.counters["parsed_total"] == 0


# --------------------------------------------------------------------------
# Never-crash
# --------------------------------------------------------------------------


@settings(max_examples=800, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.binary(min_size=0, max_size=2000))
def test_arbitrary_bytes_never_raise(data: bytes) -> None:
    parser = PacketParser()
    result = parser.parse(data, RECV_MONO, RECV_WALL)
    assert result is None or result.header.packet_format in (2025, 2026)
    counters = parser.counters
    assert (
        counters["parsed_total"] + counters["skipped_total"] + counters["errors_total"]
        == 1
    )


@settings(max_examples=300)
@given(
    packet_format=st.sampled_from(FORMATS),
    packet_id=st.integers(min_value=0, max_value=16),
    corruption=st.binary(min_size=1, max_size=64),
    offset=st.integers(min_value=0, max_value=1400),
)
def test_corrupted_valid_packets_never_raise(
    packet_format: int, packet_id: int, corruption: bytes, offset: int
) -> None:
    """Start from a structurally valid packet and smash bytes into it."""
    if packet_id not in SPECS[packet_format].sizes:
        return
    data = bytearray(build(packet_format, packet_id))
    at = offset % len(data)
    data[at : at + len(corruption)] = corruption[: len(data) - at]
    parser = PacketParser()
    parser.parse(bytes(data), RECV_MONO, RECV_WALL)  # must not raise
    counters = parser.counters
    assert (
        counters["parsed_total"] + counters["skipped_total"] + counters["errors_total"]
        == 1
    )


@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    stream=st.lists(
        st.one_of(
            st.binary(min_size=0, max_size=200),
            st.sampled_from(FORMATS).map(lambda f: build_motion(f)),
            st.sampled_from(FORMATS).map(lambda f: build_event(f, "SSTA")),
        ),
        max_size=30,
    )
)
def test_mixed_stream_never_raises_and_counts_every_datagram(
    stream: list[bytes],
) -> None:
    parser = PacketParser()
    for datagram in stream:
        parser.parse(datagram, RECV_MONO, RECV_WALL)
    counters = parser.counters
    assert (
        counters["parsed_total"] + counters["skipped_total"] + counters["errors_total"]
        == len(stream)
    )


def test_event_packet_with_non_ascii_code_does_not_raise(parser: PacketParser) -> None:
    data = bytearray(build_event(2026, "SSTA"))
    data[HEADER_SIZE : HEADER_SIZE + 4] = b"\xff\xfe\x00\x80"
    packet = parser.parse(bytes(data), RECV_MONO, RECV_WALL)
    assert packet is not None
    assert isinstance(packet.view.code, str)
    assert packet.view.details == {}


def test_participant_name_with_invalid_utf8_does_not_raise(
    parser: PacketParser,
) -> None:
    data = bytearray(build(2026, 4))
    # Splatter invalid UTF-8 across the first participant's name field.
    start = HEADER_SIZE + 1 + 9
    data[start : start + 32] = b"\xff\xfe\xfd" + bytes(29)
    packet = parser.parse(bytes(data), RECV_MONO, RECV_WALL)
    assert packet is not None
    assert isinstance(packet.view.cars[0].name, str)


# --------------------------------------------------------------------------
# Counter bookkeeping
# --------------------------------------------------------------------------


def test_counters_shape(parser: PacketParser) -> None:
    counters = parser.counters
    for key in (
        "parsed_total",
        "skipped_total",
        "errors_total",
        "unknown_packet_id_total",
        "size_mismatch_total",
        "unknown_format_total",
    ):
        assert counters[key] == 0
    assert counters["by_packet_id"] == {"parsed": {}, "skipped": {}, "errors": {}}


def test_counters_accumulate_across_a_mixed_stream(parser: PacketParser) -> None:
    stream = [
        build_motion(2025),  # parsed
        build_motion(2026),  # parsed
        build(2026, 5),  # skipped (CarSetups)
        build(2026, 13),  # skipped (MotionEx)
        b"\x00" * 10,  # error: shorter than the header
        build_motion(2026)[:100],  # error: size mismatch
    ]
    bad_format = bytearray(build_motion(2026))
    struct.pack_into("<H", bad_format, 0, 2027)
    stream.append(bytes(bad_format))  # error: unknown format

    for datagram in stream:
        parser.parse(datagram, RECV_MONO, RECV_WALL)

    counters = parser.counters
    assert counters["parsed_total"] == 2
    assert counters["skipped_total"] == 2
    assert counters["errors_total"] == 3
    assert counters["size_mismatch_total"] == 1
    assert counters["unknown_format_total"] == 1
    assert counters["unknown_packet_id_total"] == 0
    assert counters["by_packet_id"]["parsed"] == {0: 2}
    assert counters["by_packet_id"]["skipped"] == {5: 1, 13: 1}
    assert counters["by_packet_id"]["errors"] == {0: 2}


def test_parsed_packet_carries_receive_timestamps(parser: PacketParser) -> None:
    packet = parser.parse(build_motion(2026), 111, 222)
    assert packet is not None
    assert packet.recv_monotonic_ns == 111
    assert packet.recv_wall_ns == 222


# --------------------------------------------------------------------------
# 2026 energy merge cache
# --------------------------------------------------------------------------


ENERGY_STATUS = {
    "ers_store_energy": 3_500_000.0,
    "ers_deploy_mode": 3,
    "ers_harvested_this_lap_mguk": 900_000.0,
    "ers_harvested_this_lap_mguh": 100_000.0,
    "ers_deployed_this_lap": 2_400_000.0,
}


def test_2026_energy_reaches_telemetry_view_via_the_merge_cache(
    parser: PacketParser,
) -> None:
    """CarStatus (id 7) feeds the whole 2026 energy quartet onto TelemetryView."""
    parser.parse(
        build_car_status(2026, player_car_index=0, cars={0: ENERGY_STATUS}),
        RECV_MONO,
        RECV_WALL,
    )
    packet = parser.parse(
        build_car_telemetry(2026, player_car_index=0, cars={0: {"speed": 300}}),
        RECV_MONO,
        RECV_WALL,
    )
    assert packet is not None
    view = packet.view
    assert view.energy_store_j == pytest.approx(3_500_000.0)
    assert view.energy_deploy_mode == 3
    # Harvest is the MGU-K + MGU-H sum, matching the 2025 StatusView convention.
    assert view.energy_harvested_lap_j == pytest.approx(1_000_000.0)
    assert view.energy_deployed_lap_j == pytest.approx(2_400_000.0)


def test_menu_packets_do_not_wipe_the_2026_merge_cache(parser: PacketParser) -> None:
    """A pause-menu bounce (sessionUID 0) must not blank the energy readouts.

    Menus emit sessionUID 0; the lifecycle in state/session.py ignores those
    packets outright. The merge cache has to agree, otherwise every
    CarTelemetry decoded between the menu and the next CarStatus carries
    energy_store_j=None and the pit-wall energy panel goes dark.
    """
    real_uid = 0xF126_0BADC0DE
    parser.parse(
        build_car_status(2026, session_uid=real_uid, player_car_index=0, cars={0: ENERGY_STATUS}),
        RECV_MONO,
        RECV_WALL,
    )
    # A burst of menu traffic on uid 0, then straight back to the session.
    for _ in range(5):
        parser.parse(
            build_car_telemetry(2026, session_uid=0, player_car_index=0, cars={0: {"speed": 0}}),
            RECV_MONO,
            RECV_WALL,
        )
    packet = parser.parse(
        build_car_telemetry(
            2026, session_uid=real_uid, player_car_index=0, cars={0: {"speed": 300}}
        ),
        RECV_MONO,
        RECV_WALL,
    )
    assert packet is not None
    assert packet.view.energy_store_j == pytest.approx(3_500_000.0)
    assert packet.view.energy_deploy_mode == 3


def test_a_real_session_change_still_clears_the_merge_cache(parser: PacketParser) -> None:
    """Only uid 0 is exempt -- a genuine new session must start from nothing."""
    parser.parse(
        build_car_status(2026, session_uid=1111, player_car_index=0, cars={0: ENERGY_STATUS}),
        RECV_MONO,
        RECV_WALL,
    )
    packet = parser.parse(
        build_car_telemetry(2026, session_uid=2222, player_car_index=0, cars={0: {"speed": 300}}),
        RECV_MONO,
        RECV_WALL,
    )
    assert packet is not None
    assert packet.view.energy_store_j is None
    assert packet.view.energy_deploy_mode is None
