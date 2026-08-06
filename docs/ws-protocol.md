# WebSocket protocol — `/ws`

Single endpoint. JSON text frames, each with a `"type"` discriminator. This document is the
contract between the backend broadcaster (`web/broadcast.py`), the state layer (`state/live.py`,
which produces these payloads as dicts), and the Svelte frontend. Field names are frozen;
additive changes only.

All times ms unless suffixed. `null` = unknown/not applicable. Wheel arrays are `[rl, rr, fl, fr]`.

## `snapshot` — sent once, immediately on connect
```json
{
  "type": "snapshot",
  "protocol_version": 1,
  "session": { /* same shape as "slow".session */ },
  "fast": { /* latest fast frame or null if no telemetry yet */ },
  "slow": { /* latest slow frame or null */ },
  "recent_events": [ /* last 20 event messages, oldest first */ ]
}
```

## `fast` — 10 Hz while telemetry is flowing
```json
{
  "type": "fast",
  "t": 123.456,              // game sessionTime, seconds
  "speed_kmh": 287.0,
  "gear": 7,                 // -1 R, 0 N
  "rpm": 11450,
  "throttle": 1.0,           // 0..1
  "brake": 0.0,              // 0..1
  "steer": -0.12,            // -1..1
  "drs_open": false,         // null on 2026 format
  "aero_mode": 1,            // null on 2025 format
  "ers_deploy_mode": 2,      // format-appropriate deploy/override mode, null if n/a
  "rev_lights_percent": 60,
  "lap_number": 12,
  "lap_distance_m": 3120.5,
  "current_lap_ms": 61234,
  "delta_best_ms": -142,     // live delta vs reference lap; null if no reference
  "delta_kind": "session_best" // "session_best" | "personal_best" | "race_best" | null
}
```

## `slow` — 1 Hz
```json
{
  "type": "slow",
  "session": {
    "session_uid": "1234567890",   // stringified u64
    "segment": 0,
    "packet_format": 2026,
    "session_type": 10,            // raw enum
    "session_kind": "race",        // "practice" | "quali" | "race" | "time_trial" | "other"
    "track_id": 3,
    "track_name": "Suzuka",
    "time_left_s": 1740,
    "duration_s": 3600,
    "total_laps": 53,
    "safety_car": 0,               // 0 none, 1 full, 2 virtual, 3 formation
    "fia_flag": 0,                 // -1 unknown, 0 none, 1 green, 2 blue, 3 yellow, 4 red
    "weather": 1,
    "track_temp_c": 31,
    "air_temp_c": 24,
    "forecast": [ {"offset_min": 5, "weather": 2, "rain_pct": 30}, ... ],
    "stalled": false,              // true when >5 s without packets
    "joined_in_progress": false
  },
  "tower": [
    {
      "car_index": 4, "position": 1, "name": "VERSTAPPEN", "team_id": 2,
      "is_player": false, "lap_number": 13, "last_lap_ms": 92345,
      "gap_ahead_ms": null, "gap_leader_ms": 0,
      "compound_visual": 16, "tyre_age_laps": 8,
      "pit_status": 0, "penalties_s": 0, "result_status": 2
    }, ...
  ],
  "tyres": {
    "surface_temp_c": [98, 97, 92, 94],
    "inner_temp_c": [102, 101, 96, 97],
    "pressure_psi": [21.5, 21.6, 23.0, 23.1],
    "wear_pct": [12.5, 13.1, 8.2, 8.9],
    "wear_rate_pct_per_lap": [0.9, 1.0, 0.6, 0.7],  // rolling estimate, null early
    "projected_wear_end_pct": [31.0, 33.5, 20.1, 22.0], // race only, null otherwise
    "compound_actual": 18, "compound_visual": 16, "age_laps": 8
  },
  "fuel": {
    "in_tank_kg": 43.2,
    "remaining_laps": 22.4,        // game's own estimate
    "laps_left_in_session": 21,    // null in timed sessions
    "delta_laps": 1.4,             // remaining_laps - laps_left; null if unknown
    "burn_last_lap_kg": 1.9
  },
  "energy": {
    "store_j": 2800000.0,          // ERS store (2025) or energy store (2026)
    "store_pct": 70.0,
    "deploy_mode": 2,
    "harvested_lap_j": 120000.0,   // null on 2026
    "deployed_lap_j": 350000.0     // null on 2026
  },
  "damage": {
    "front_left_wing_pct": 0, "front_right_wing_pct": 5, "rear_wing_pct": 0,
    "floor_pct": 0, "diffuser_pct": 0, "sidepod_pct": 0,
    "gearbox_pct": 3, "engine_pct": 7
  },
  "pace": {
    "last_3_avg_ms": 92800,          // player rolling avg
    "ahead_last_3_avg_ms": 92500,    // car directly ahead, null if none
    "behind_last_3_avg_ms": 93400
  },
  "sectors": {
    "current_lap": [21345, null, null],       // s1..s3 of the lap in progress
    "best_lap": [21001, 35400, 35900],        // reference lap sectors, nulls if none
    "session_best": [20950, 35310, 35800],    // overall session best per sector
    "last_lap_valid": true
  },
  "timetrial": {                    // null unless session_kind == "time_trial"
    "pb_ms": 91200, "rival_ms": 91550,
    "pb_sectors": [20900, 35200, 35100], "rival_sectors": [21000, 35300, 35250]
  },
  "health": {
    "packets_per_sec": 480,
    "parse_errors_total": 0,
    "kernel_drops_total": 0,
    "last_packet_age_ms": 40,
    "ws_clients": 2
  }
}
```

## `event` — immediate
```json
{
  "type": "event",
  "t": 123.456,
  "code": "SECTOR",   // see list below
  "data": { ... }     // per-code payload
}
```
Codes and payloads:
- `SECTOR` `{sector: 1, time_ms, color: "purple"|"green"|"yellow"}` — player sector completed
- `LAP` `{lap_number, time_ms, valid, color, sectors: [s1,s2,s3]}` — player lap completed
- `LAP_INVALID` `{lap_number}` — current lap invalidated
- `PIT_IN` / `PIT_OUT` `{lap_number}`
- `PENALTY` `{penalty_type, infringement_type, time_s, other_car}`
- `FLAG` `{flag: "yellow"|"red"|"blue"|"green"|"clear"}`
- `SC` `{status: "deployed"|"virtual"|"ending"|"in"}`
- `FLASHBACK` `{to_session_time_s}` — generation bumped, delta reference reset
- `SESSION_START` `{}` / `SESSION_END` `{}` / `CHEQUERED` `{}`
- `FASTEST_LAP` `{car_index, name, time_ms}`
- `DRS` `{enabled: true|false}` — DRS enabled/disabled (2025 format sessions only)
- `STALLED` `{stalled: true|false}` — capture health transition

## Client behavior requirements
- Reconnect with exponential backoff (1 s → 30 s cap, jitter); a fresh `snapshot` arrives on
  reconnect, so clients hold no cross-connection state.
- Animated elements (delta bar, input bars, rev strip) tween toward new `fast` values over
  ~100 ms via requestAnimationFrame; numeric readouts render values as-is on arrival.
- Server enforces a max-clients cap (config, default 20); connection N+1 is closed with code 1013.
