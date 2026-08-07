-- f126 Postgres schema, version 2.
--
-- Postgres is a DERIVED INDEX: the .f1raw capture logs are the source of truth and
-- every table here can be rebuilt with `f126 backfill`. That is why sample tables are
-- append-only and why nothing here is expensive to recreate.
--
-- This file is applied in full at every boot (see db.apply_schema) so it MUST stay
-- idempotent: CREATE ... IF NOT EXISTS only, no destructive statements, no parameters
-- (it is executed as a single multi-statement query).

CREATE TABLE IF NOT EXISTS schema_meta (
    version INT NOT NULL
);

INSERT INTO schema_meta (version)
SELECT 1
WHERE NOT EXISTS (SELECT 1 FROM schema_meta);

-- One row per (session_uid, segment). "segment" separates the parts of a session that
-- the game reports under one UID but that are logically distinct captures (rejoin after
-- a crash, flashback past our rewind window, capture restart). started_at_wall is part
-- of the natural key so a genuinely new run of the same UID+segment gets its own row;
-- NULLS NOT DISTINCT keeps ON CONFLICT idempotent for placeholder rows created before
-- the session packet has been seen (requires Postgres 15+).
CREATE TABLE IF NOT EXISTS sessions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_uid TEXT NOT NULL,
    segment INT NOT NULL DEFAULT 0,
    packet_format INT,
    session_type INT,
    session_type_name TEXT,
    track_id INT,
    track_name TEXT,
    started_at_wall DOUBLE PRECISION,
    ended_at_wall DOUBLE PRECISION,
    ended_reason TEXT,
    joined_in_progress BOOLEAN DEFAULT FALSE,
    player_car_index INT,
    total_laps INT,
    session_duration_s INT,
    weather_json JSONB,
    final_classification_json JSONB,
    raw_file TEXT,
    UNIQUE NULLS NOT DISTINCT (session_uid, segment, started_at_wall)
);

CREATE INDEX IF NOT EXISTS sessions_started_idx ON sessions (started_at_wall DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS sessions_uid_idx ON sessions (session_uid, segment);

CREATE TABLE IF NOT EXISTS participants (
    session_id BIGINT REFERENCES sessions(id) ON DELETE CASCADE,
    car_index INT,
    name TEXT,
    team_id INT,
    race_number INT,
    is_ai BOOLEAN,
    is_player BOOLEAN,
    PRIMARY KEY (session_id, car_index)
);

-- generation bumps when history reconciliation supersedes a lap we had already written
-- with different sector/validity data; readers take the highest generation per lap.
CREATE TABLE IF NOT EXISTS laps (
    session_id BIGINT REFERENCES sessions(id) ON DELETE CASCADE,
    car_index INT,
    lap_number INT,
    generation INT DEFAULT 0,
    lap_time_ms INT,
    s1_ms INT,
    s2_ms INT,
    s3_ms INT,
    valid BOOLEAN,
    compound_actual INT,
    compound_visual INT,
    tyre_age_laps INT,
    fuel_start_kg REAL,
    fuel_end_kg REAL,
    ers_deployed_j REAL,
    ers_harvested_j REAL,
    top_speed_kmh REAL,
    penalties_s INT,
    wall_ts DOUBLE PRECISION,
    PRIMARY KEY (session_id, car_index, lap_number, generation)
);

CREATE INDEX IF NOT EXISTS laps_session_car_idx ON laps (session_id, car_index, lap_number);

-- Player-car time series, downsampled to cfg.telemetry_hz. Append-only.
CREATE TABLE IF NOT EXISTS telemetry_samples (
    session_id BIGINT REFERENCES sessions(id) ON DELETE CASCADE,
    segment INT,
    generation INT,
    session_time_s DOUBLE PRECISION,
    frame_id BIGINT,
    overall_frame_id BIGINT,
    lap_number INT,
    lap_distance_m REAL,
    speed_kmh REAL,
    throttle REAL,
    brake REAL,
    steer REAL,
    gear SMALLINT,
    rpm INT,
    drs_or_aero SMALLINT,
    tyre_surface_temp REAL[],
    tyre_inner_temp REAL[],
    tyre_pressure REAL[],
    brake_temp REAL[],
    fuel_kg REAL,
    energy_store_j REAL,
    energy_deploy_mode SMALLINT,
    world_x REAL,
    world_z REAL
);

CREATE INDEX IF NOT EXISTS telemetry_samples_lap_idx
    ON telemetry_samples (session_id, lap_number, lap_distance_m);
CREATE INDEX IF NOT EXISTS telemetry_samples_time_idx
    ON telemetry_samples (session_id, session_time_s);

-- Wear/damage time series, downsampled to cfg.wear_hz. Append-only.
CREATE TABLE IF NOT EXISTS wear_samples (
    session_id BIGINT REFERENCES sessions(id) ON DELETE CASCADE,
    segment INT,
    session_time_s DOUBLE PRECISION,
    lap_number INT,
    tyre_wear_pct REAL[],
    damage_json JSONB
);

CREATE INDEX IF NOT EXISTS wear_samples_time_idx ON wear_samples (session_id, session_time_s);

-- Game events (SSTA, FTLP, PENA, FLBK, ...). Append-only.
CREATE TABLE IF NOT EXISTS events (
    session_id BIGINT REFERENCES sessions(id) ON DELETE CASCADE,
    segment INT,
    session_time_s DOUBLE PRECISION,
    frame_id BIGINT,
    wall_ts DOUBLE PRECISION,
    code TEXT,
    details_json JSONB
);

CREATE INDEX IF NOT EXISTS events_time_idx ON events (session_id, session_time_s);
CREATE INDEX IF NOT EXISTS events_code_idx ON events (session_id, code);

CREATE TABLE IF NOT EXISTS tyre_stints (
    session_id BIGINT REFERENCES sessions(id) ON DELETE CASCADE,
    segment INT,
    car_index INT,
    stint_no INT,
    compound_actual INT,
    compound_visual INT,
    lap_start INT,
    lap_end INT,
    wear_at_end_json JSONB,
    end_reason TEXT,
    PRIMARY KEY (session_id, car_index, stint_no)
);

-- Inventory of the raw capture files. session_id is SET NULL rather than CASCADE: the
-- file outlives any derived session row we might delete and re-derive.
CREATE TABLE IF NOT EXISTS raw_captures (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id BIGINT REFERENCES sessions(id) ON DELETE SET NULL,
    path TEXT UNIQUE,
    bytes BIGINT,
    packets BIGINT,
    first_wall DOUBLE PRECISION,
    last_wall DOUBLE PRECISION,
    packet_format INT
);

CREATE INDEX IF NOT EXISTS raw_captures_session_idx ON raw_captures (session_id);

-- Post-session debrief (schema v2). `fact_sheet` is the deterministic input:
-- f126.analysis.factsheet computed every number in it from the tables above, and `text` is
-- prose an LLM wrote FROM that sheet and nothing else. Storing both is what makes a debrief
-- auditable — any claim in the text can be checked against the numbers it was given.
--
-- Append-only, like the sample tables: regenerating inserts a new row and readers take the
-- newest, so a bad generation never destroys the one before it. `prompt_version` records
-- which prompt produced the text, so a prompt change is visible rather than retroactive.
CREATE TABLE IF NOT EXISTS debriefs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id BIGINT REFERENCES sessions(id) ON DELETE CASCADE,
    created_at DOUBLE PRECISION,
    model TEXT,
    prompt_version INT,
    fact_sheet JSONB,
    text TEXT
);

CREATE INDEX IF NOT EXISTS debriefs_session_idx
    ON debriefs (session_id, created_at DESC NULLS LAST, id DESC);
