"""Enum tables for the F1 25 UDP protocol (formats 2025 and 2026).

Values are transcribed from the two authoritative references:

* format 2025 -- MacManley/f1-25-udp C headers (``src/*.h``)
* format 2026 -- volodymyr-fed/F1Game.UDP v26 C# enums (``F1Game.UDP/Enums/*.cs``)

Both formats share these id spaces; where the 2026 pack *added* members
(e.g. new teams, the C6/C0 compounds, the Madrid track) the extra ids are
simply absent from a 2025 stream and harmless to carry.

Every lookup goes through a ``*_name`` helper that degrades to a synthetic
``"<kind>_<id>"`` string instead of raising, because a live UDP stream from a
patched game will happily contain ids that post-date this table.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = [
    "EVENT_CODES",
    "ActualCompound",
    "DrsDisabledReason",
    "ErsDeployMode",
    "EventCode",
    "FiaFlag",
    "PenaltyType",
    "PitStatus",
    "Platform",
    "ResultReason",
    "ResultStatus",
    "SafetyCarEventType",
    "SafetyCarType",
    "SessionType",
    "Surface",
    "Team",
    "Track",
    "VisualCompound",
    "Weather",
    "compound_name",
    "ers_deploy_mode_name",
    "fia_flag_name",
    "pit_status_name",
    "result_status_name",
    "safety_car_status_name",
    "session_type_name",
    "surface_name",
    "team_name",
    "track_name",
    "visual_compound_name",
    "weather_name",
]


class Track(IntEnum):
    """Track ids. Wire type is int8 -- ``-1`` (0xFF) means unknown."""

    UNKNOWN = -1
    MELBOURNE = 0
    SHANGHAI = 2
    BAHRAIN = 3
    CATALUNYA = 4
    MONACO = 5
    MONTREAL = 6
    SILVERSTONE = 7
    HUNGARORING = 9
    SPA = 10
    MONZA = 11
    SINGAPORE = 12
    SUZUKA = 13
    ABU_DHABI = 14
    TEXAS = 15
    BRAZIL = 16
    AUSTRIA = 17
    MEXICO = 19
    AZERBAIJAN = 20
    ZANDVOORT = 26
    IMOLA = 27
    JEDDAH = 29
    MIAMI = 30
    LAS_VEGAS = 31
    QATAR = 32
    SILVERSTONE_REVERSE = 39
    AUSTRIA_REVERSE = 40
    ZANDVOORT_REVERSE = 41
    MADRID = 42  # added by the 2026 Season Pack


_TRACK_NAMES: dict[int, str] = {
    Track.UNKNOWN: "unknown",
    Track.MELBOURNE: "Melbourne",
    Track.SHANGHAI: "Shanghai",
    Track.BAHRAIN: "Bahrain",
    Track.CATALUNYA: "Catalunya",
    Track.MONACO: "Monaco",
    Track.MONTREAL: "Montreal",
    Track.SILVERSTONE: "Silverstone",
    Track.HUNGARORING: "Hungaroring",
    Track.SPA: "Spa",
    Track.MONZA: "Monza",
    Track.SINGAPORE: "Singapore",
    Track.SUZUKA: "Suzuka",
    Track.ABU_DHABI: "Abu Dhabi",
    Track.TEXAS: "Texas",
    Track.BRAZIL: "Brazil",
    Track.AUSTRIA: "Austria",
    Track.MEXICO: "Mexico",
    Track.AZERBAIJAN: "Azerbaijan",
    Track.ZANDVOORT: "Zandvoort",
    Track.IMOLA: "Imola",
    Track.JEDDAH: "Jeddah",
    Track.MIAMI: "Miami",
    Track.LAS_VEGAS: "Las Vegas",
    Track.QATAR: "Qatar",
    Track.SILVERSTONE_REVERSE: "Silverstone (reverse)",
    Track.AUSTRIA_REVERSE: "Austria (reverse)",
    Track.ZANDVOORT_REVERSE: "Zandvoort (reverse)",
    Track.MADRID: "Madrid",
}


def track_name(track_id: int) -> str:
    """Human-readable track name, or ``"track_<id>"`` for unknown ids."""
    return _TRACK_NAMES.get(track_id, f"track_{track_id}")


class Team(IntEnum):
    """Team ids. Wire type is uint8 on 2025 and uint16 on 2026."""

    MERCEDES = 0
    FERRARI = 1
    RED_BULL_RACING = 2
    WILLIAMS = 3
    ASTON_MARTIN = 4
    ALPINE = 5
    RACING_BULLS = 6
    HAAS = 7
    MCLAREN = 8
    SAUBER = 9
    F1_GENERIC = 41
    F1_CUSTOM_TEAM = 104
    KONNERSPORT = 129
    APXGP_24 = 142
    APXGP_25 = 154
    KONNERSPORT_24 = 155
    ART_GP_24 = 158
    CAMPOS_24 = 159
    RODIN_24 = 160
    AIX_RACING_24 = 161
    DAMS_24 = 162
    HITECH_24 = 163
    MP_MOTORSPORT_24 = 164
    PREMA_24 = 165
    TRIDENT_24 = 166
    VAN_AMERSFOORT_RACING_24 = 167
    INVICTA_24 = 168
    MERCEDES_24 = 185
    FERRARI_24 = 186
    RED_BULL_RACING_24 = 187
    WILLIAMS_24 = 188
    ASTON_MARTIN_24 = 189
    ALPINE_24 = 190
    RACING_BULLS_24 = 191
    HAAS_24 = 192
    MCLAREN_24 = 193
    SAUBER_24 = 194
    # The 2026 Season Pack widened the field to uint16 to fit these.
    ART_GP_25 = 465
    CAMPOS_25 = 466
    RODIN_MOTORSPORT_25 = 467
    AIX_RACING_25 = 468
    DAMS_25 = 469
    HITECH_25 = 470
    MP_MOTORSPORT_25 = 471
    PREMA_25 = 472
    TRIDENT_25 = 473
    VAN_AMERSFOORT_RACING_25 = 474
    INVICTA_25 = 475
    MERCEDES_26 = 476
    FERRARI_26 = 477
    RED_BULL_RACING_26 = 478
    WILLIAMS_26 = 479
    ASTON_MARTIN_26 = 480
    ALPINE_26 = 481
    RACING_BULLS_26 = 482
    HAAS_26 = 483
    MCLAREN_26 = 484
    AUDI_26 = 485
    CADILLAC_26 = 486


def team_name(team_id: int) -> str:
    """Team name, or ``"team_<id>"`` for unknown ids."""
    try:
        return Team(team_id).name.replace("_", " ").title()
    except ValueError:
        return f"team_{team_id}"


class SessionType(IntEnum):
    UNKNOWN = 0
    PRACTICE_1 = 1
    PRACTICE_2 = 2
    PRACTICE_3 = 3
    SHORT_PRACTICE = 4
    QUALIFYING_1 = 5
    QUALIFYING_2 = 6
    QUALIFYING_3 = 7
    SHORT_QUALIFYING = 8
    ONE_SHOT_QUALIFYING = 9
    SPRINT_SHOOTOUT_1 = 10
    SPRINT_SHOOTOUT_2 = 11
    SPRINT_SHOOTOUT_3 = 12
    SHORT_SPRINT_SHOOTOUT = 13
    ONE_SHOT_SPRINT_SHOOTOUT = 14
    RACE = 15
    RACE_2 = 16
    RACE_3 = 17
    TIME_TRIAL = 18


def session_type_name(session_type: int) -> str:
    """Session type name, or ``"session_type_<id>"`` for unknown ids."""
    try:
        return SessionType(session_type).name.replace("_", " ").title()
    except ValueError:
        return f"session_type_{session_type}"


class ActualCompound(IntEnum):
    """Physical compound. Note 16..22 collide numerically with VisualCompound."""

    F1_INTER = 7
    F1_WET = 8
    F1_CLASSIC_DRY = 9
    F1_CLASSIC_WET = 10
    F2_SUPER_SOFT = 11
    F2_SOFT = 12
    F2_MEDIUM = 13
    F2_HARD = 14
    F2_WET = 15
    F1_C5 = 16
    F1_C4 = 17
    F1_C3 = 18
    F1_C2 = 19
    F1_C1 = 20
    F1_C0 = 21
    F1_C6 = 22  # added by the 2026 Season Pack


def compound_name(compound: int) -> str:
    """Actual compound name, or ``"compound_<id>"`` for unknown ids."""
    try:
        return ActualCompound(compound).name
    except ValueError:
        return f"compound_{compound}"


class VisualCompound(IntEnum):
    """What the broadcast graphic shows. Distinct id space from ActualCompound."""

    F1_INTER = 7
    F1_WET = 8
    F1_CLASSIC_DRY = 9
    F1_CLASSIC_WET = 10
    F2_WET = 15
    F1_SOFT = 16
    F1_MEDIUM = 17
    F1_HARD = 18
    F2_SUPER_SOFT = 19
    F2_SOFT = 20
    F2_MEDIUM = 21
    F2_HARD = 22


def visual_compound_name(compound: int) -> str:
    """Visual compound name, or ``"visual_compound_<id>"`` for unknown ids."""
    try:
        return VisualCompound(compound).name
    except ValueError:
        return f"visual_compound_{compound}"


class Weather(IntEnum):
    CLEAR = 0
    LIGHT_CLOUD = 1
    OVERCAST = 2
    LIGHT_RAIN = 3
    HEAVY_RAIN = 4
    STORM = 5


def weather_name(weather: int) -> str:
    """Weather name, or ``"weather_<id>"`` for unknown ids."""
    try:
        return Weather(weather).name.replace("_", " ").title()
    except ValueError:
        return f"weather_{weather}"


class SafetyCarType(IntEnum):
    NONE = 0
    FULL = 1
    VIRTUAL = 2
    FORMATION_LAP = 3


def safety_car_status_name(status: int) -> str:
    """Safety car status name, or ``"safety_car_<id>"`` for unknown ids."""
    try:
        return SafetyCarType(status).name.replace("_", " ").title()
    except ValueError:
        return f"safety_car_{status}"


class ResultStatus(IntEnum):
    INVALID = 0
    INACTIVE = 1
    ACTIVE = 2
    FINISHED = 3
    DID_NOT_FINISH = 4
    DISQUALIFIED = 5
    NOT_CLASSIFIED = 6
    RETIRED = 7


def result_status_name(status: int) -> str:
    """Result status name, or ``"result_status_<id>"`` for unknown ids."""
    try:
        return ResultStatus(status).name.replace("_", " ").title()
    except ValueError:
        return f"result_status_{status}"


class PitStatus(IntEnum):
    NONE = 0
    PITTING = 1
    IN_PIT_AREA = 2


def pit_status_name(status: int) -> str:
    """Pit status name, or ``"pit_status_<id>"`` for unknown ids."""
    try:
        return PitStatus(status).name.replace("_", " ").title()
    except ValueError:
        return f"pit_status_{status}"


class ErsDeployMode(IntEnum):
    NONE = 0
    MEDIUM = 1
    HOTLAP = 2
    OVERTAKE = 3


def ers_deploy_mode_name(mode: int) -> str:
    """ERS deploy mode name, or ``"ers_mode_<id>"`` for unknown ids."""
    try:
        return ErsDeployMode(mode).name.title()
    except ValueError:
        return f"ers_mode_{mode}"


class FiaFlag(IntEnum):
    """Wire type is int8; ``-1`` (0xFF) means invalid/unknown.

    F1Game.UDP's ``FiaFlag`` omits ``RED = 4``; the 2025 C header documents it
    (``PacketCarStatusData.h``) and ``types.py`` names it, so it is kept here.
    """

    UNKNOWN = -1
    NONE = 0
    GREEN = 1
    BLUE = 2
    YELLOW = 3
    RED = 4


def fia_flag_name(flag: int) -> str:
    """FIA flag name, or ``"flag_<id>"`` for unknown ids."""
    try:
        return FiaFlag(flag).name.title()
    except ValueError:
        return f"flag_{flag}"


class Surface(IntEnum):
    TARMAC = 0
    RUMBLE_STRIP = 1
    CONCRETE = 2
    ROCK = 3
    GRAVEL = 4
    MUD = 5
    SAND = 6
    GRASS = 7
    WATER = 8
    COBBLESTONE = 9
    METAL = 10
    RIDGED = 11


def surface_name(surface: int) -> str:
    """Driving surface name, or ``"surface_<id>"`` for unknown ids."""
    try:
        return Surface(surface).name.replace("_", " ").title()
    except ValueError:
        return f"surface_{surface}"


class Platform(IntEnum):
    STEAM = 1
    PLAYSTATION = 3
    XBOX = 4
    ORIGIN = 6
    UNKNOWN = 255


class ResultReason(IntEnum):
    INVALID = 0
    RETIRED = 1
    FINISHED = 2
    TERMINAL_DAMAGE = 3
    INACTIVE = 4
    NOT_ENOUGH_LAPS_COMPLETED = 5
    BLACK_FLAGGED = 6
    RED_FLAGGED = 7
    MECHANICAL_FAILURE = 8
    SESSION_SKIPPED = 9
    SESSION_SIMULATED = 10


class SafetyCarEventType(IntEnum):
    DEPLOYED = 0
    RETURNING = 1
    RETURNED = 2
    RESUME_RACE = 3


class DrsDisabledReason(IntEnum):
    WET_TRACK = 0
    SAFETY_CAR_DEPLOYED = 1
    RED_FLAG = 2
    MIN_LAP_NOT_REACHED = 3


class PenaltyType(IntEnum):
    DRIVE_THROUGH = 0
    STOP_GO = 1
    GRID_PENALTY = 2
    PENALTY_REMINDER = 3
    TIME_PENALTY = 4
    WARNING = 5
    DISQUALIFIED = 6
    REMOVED_FROM_FORMATION_LAP = 7
    PARKED_TOO_LONG_TIMER = 8
    TYRE_REGULATIONS = 9
    THIS_LAP_INVALIDATED = 10
    THIS_AND_NEXT_LAP_INVALIDATED = 11
    THIS_LAP_INVALIDATED_WITHOUT_REASON = 12
    THIS_AND_NEXT_LAP_INVALIDATED_WITHOUT_REASON = 13
    THIS_AND_PREVIOUS_LAP_INVALIDATED = 14
    THIS_AND_PREVIOUS_LAP_INVALIDATED_WITHOUT_REASON = 15
    RETIRED = 16
    BLACK_FLAG_TIMER = 17


class EventCode:
    """The 21 four-character event string codes, as ASCII ``str``.

    ``types.py`` stores ``EventView.code`` as the raw spec code, so these are
    plain string constants rather than an enum -- an unrecognised code is
    passed through verbatim rather than being dropped.
    """

    SESSION_STARTED = "SSTA"
    SESSION_ENDED = "SEND"
    FASTEST_LAP = "FTLP"
    RETIREMENT = "RTMT"
    DRS_ENABLED = "DRSE"
    DRS_DISABLED = "DRSD"
    TEAM_MATE_IN_PITS = "TMPT"
    CHEQUERED_FLAG = "CHQF"
    RACE_WINNER = "RCWN"
    PENALTY_ISSUED = "PENA"
    SPEED_TRAP_TRIGGERED = "SPTP"
    START_LIGHTS = "STLG"
    LIGHTS_OUT = "LGOT"
    DRIVE_THROUGH_SERVED = "DTSV"
    STOP_GO_SERVED = "SGSV"
    FLASHBACK = "FLBK"
    BUTTON_STATUS = "BUTN"
    RED_FLAG = "RDFL"
    OVERTAKE = "OVTK"
    SAFETY_CAR = "SCAR"
    COLLISION = "COLL"


EVENT_CODES: frozenset[str] = frozenset(
    value
    for name, value in vars(EventCode).items()
    if not name.startswith("_") and isinstance(value, str)
)
