import { describe, it, expect } from 'vitest';
import {
  BAHRAIN_LENGTH_M,
  BAHRAIN_TRACK_ID,
  BAHRAIN_TT_SESSION_ID,
  HEADLINE_SESSION_ID,
  MIAMI_GP_SESSION_ID,
  MIAMI_SPRINT_SESSION_ID,
  MIAMI_S2_RACE_SESSION_ID,
  MIAMI_TRACK_ID,
  MONTREAL_TRACK_ID,
  PLAYER_CAR_INDEX,
  handleMockRequest,
  installAnalysisMock,
  lapTimeMs,
  mockCareerOverview,
  mockCareerTrack,
  mockCompare,
  mockCorners,
  mockLaps,
  mockDebrief,
  mockSessionDetail,
  mockSessions,
  mockStints,
  mockTelemetry
} from '../lib/analysis-mock';
import type {
  CareerTotals,
  CareerTrackResponse,
  CareerWeekend,
  CompareResponse,
  CornersResponse,
  StintsResponse
} from '../lib/analysis-api';

/**
 * The mock is the only thing the analysis pages see during development, so a
 * bug in it looks exactly like a bug in a page. These assertions are about
 * plausibility, not exact values: an F1 lap at Bahrain is around 90 seconds,
 * speed is bounded, distance increases, and the derived quantities agree with
 * the trace they were derived from.
 */

const isCompare = (v: unknown): v is CompareResponse =>
  typeof v === 'object' && v !== null && 'grid_m' in v;
const isCorners = (v: unknown): v is CornersResponse =>
  typeof v === 'object' && v !== null && 'corners' in v;
const isStints = (v: unknown): v is StintsResponse =>
  typeof v === 'object' && v !== null && 'stints' in v;

describe('session fixtures', () => {
  it('lists sessions newest first, with a Bahrain race at the top', () => {
    const sessions = mockSessions();
    expect(sessions.length).toBeGreaterThanOrEqual(3);

    const times = sessions.map((s) => s.started_at_wall ?? 0);
    expect([...times].sort((a, b) => b - a)).toEqual(times);

    const headline = sessions.find((s) => s.id === HEADLINE_SESSION_ID);
    expect(headline?.track_name).toBe('Bahrain');
    expect(headline?.track_id).toBe(BAHRAIN_TRACK_ID);
    expect(headline?.session_type_name).toBe('Race');
    expect(headline?.lap_count).toBeGreaterThan(0);
  });

  it('offers a second session at the same track, so cross-session compare works', () => {
    const bahrain = mockSessions().filter((s) => s.track_id === BAHRAIN_TRACK_ID);
    expect(bahrain.length).toBeGreaterThanOrEqual(2);
  });

  it('offers a session at another track, so the 422 mismatch path is reachable', () => {
    const other = mockSessions().find((s) => s.track_id !== BAHRAIN_TRACK_ID);
    expect(other).toBeDefined();
    const result = mockCompare(HEADLINE_SESSION_ID, 5, other?.id ?? 0, 5);
    expect(isCompare(result)).toBe(false);
    expect((result as { status: number }).status).toBe(422);
  });

  it('names a player car and returns it among the participants', () => {
    const detail = mockSessionDetail(HEADLINE_SESSION_ID);
    expect(detail).not.toBeNull();
    expect(detail?.player_car_index).toBe(PLAYER_CAR_INDEX);
    const player = detail?.participants.find((p) => p.is_player);
    expect(player?.car_index).toBe(PLAYER_CAR_INDEX);
    expect(detail?.best_lap_ms).toBeGreaterThan(0);
  });

  it('returns null for an unknown session', () => {
    expect(mockSessionDetail(999)).toBeNull();
  });
});

describe('lap fixtures', () => {
  it('produces 20 laps per car with plausible Bahrain lap times', () => {
    const laps = mockLaps(HEADLINE_SESSION_ID, PLAYER_CAR_INDEX);
    expect(laps).toHaveLength(20);

    for (const lap of laps) {
      expect(lap.lap_time_ms).toBeGreaterThan(85_000);
      expect(lap.lap_time_ms).toBeLessThan(125_000);
      expect(lap.car_index).toBe(PLAYER_CAR_INDEX);
    }
  });

  it('splits each lap into three sectors that sum to the lap time', () => {
    for (const lap of mockLaps(HEADLINE_SESSION_ID, PLAYER_CAR_INDEX)) {
      const sum = (lap.s1_ms ?? 0) + (lap.s2_ms ?? 0) + (lap.s3_ms ?? 0);
      // Rounding to whole milliseconds three times can drift by a couple.
      expect(Math.abs(sum - (lap.lap_time_ms ?? 0))).toBeLessThanOrEqual(3);
    }
  });

  it('flags deleted laps, so the invalid styling has something to render', () => {
    const laps = mockLaps(HEADLINE_SESSION_ID, PLAYER_CAR_INDEX);
    expect(laps.filter((l) => l.valid === false).length).toBeGreaterThan(0);
  });

  it('returns both cars when no car_index is given', () => {
    const cars = new Set(mockLaps(HEADLINE_SESSION_ID).map((l) => l.car_index));
    expect(cars.size).toBe(2);
    expect(cars.has(PLAYER_CAR_INDEX)).toBe(true);
  });

  it('is deterministic across calls', () => {
    expect(mockLaps(HEADLINE_SESSION_ID, PLAYER_CAR_INDEX)).toEqual(
      mockLaps(HEADLINE_SESSION_ID, PLAYER_CAR_INDEX)
    );
  });
});

describe('telemetry', () => {
  it('returns parallel arrays ordered by distance, covering the lap', () => {
    const t = mockTelemetry(HEADLINE_SESSION_ID, 12);
    expect(t).not.toBeNull();
    if (!t) return;

    const n = t.points;
    expect(n).toBeGreaterThan(500);
    for (const arr of [
      t.distance_m,
      t.speed_kmh,
      t.throttle,
      t.brake,
      t.steer,
      t.gear,
      t.rpm,
      t.drs_or_aero,
      t.session_time_s
    ]) {
      expect(arr).toHaveLength(n);
    }

    for (let i = 1; i < t.distance_m.length; i++) {
      expect(t.distance_m[i]).toBeGreaterThanOrEqual(t.distance_m[i - 1] as number);
    }
    expect(t.distance_m[n - 1]).toBeGreaterThan(BAHRAIN_LENGTH_M * 0.97);
  });

  it('keeps every channel inside its physical range', () => {
    const t = mockTelemetry(HEADLINE_SESSION_ID, 7);
    if (!t) throw new Error('no telemetry');

    expect(Math.min(...t.speed_kmh)).toBeGreaterThan(60);
    expect(Math.max(...t.speed_kmh)).toBeLessThanOrEqual(340);
    expect(Math.min(...t.throttle)).toBeGreaterThanOrEqual(0);
    expect(Math.max(...t.throttle)).toBeLessThanOrEqual(1);
    expect(Math.min(...t.brake)).toBeGreaterThanOrEqual(0);
    expect(Math.max(...t.brake)).toBeLessThanOrEqual(1);
    expect(Math.min(...t.gear)).toBeGreaterThanOrEqual(1);
    expect(Math.max(...t.gear)).toBeLessThanOrEqual(8);
  });

  it('refuses a lap the session does not have', () => {
    expect(mockTelemetry(HEADLINE_SESSION_ID, 99)).toBeNull();
  });
});

describe('compare', () => {
  it('resamples both laps onto one ~5 m grid', () => {
    const r = mockCompare(HEADLINE_SESSION_ID, 12, HEADLINE_SESSION_ID, 9);
    expect(isCompare(r)).toBe(true);
    if (!isCompare(r)) return;

    expect(r.grid_m[0]).toBe(0);
    expect(r.grid_m[r.grid_m.length - 1]).toBeGreaterThanOrEqual(BAHRAIN_LENGTH_M - 5);
    expect(r.a.speed_kmh).toHaveLength(r.grid_m.length);
    expect(r.b.speed_kmh).toHaveLength(r.grid_m.length);
    expect(r.delta_ms).toHaveLength(r.grid_m.length);
    expect(r.track_name).toBe('Bahrain');
  });

  it('starts the cumulative delta at zero and ends at the lap-time difference', () => {
    const r = mockCompare(HEADLINE_SESSION_ID, 7, HEADLINE_SESSION_ID, 2);
    if (!isCompare(r)) throw new Error('expected a comparison');

    expect(Math.abs(r.delta_ms[0] ?? 0)).toBeLessThan(5);

    // The final delta must agree with the two lap times it came from.
    const expected = lapTimeMs(7) - lapTimeMs(2);
    const actual = r.delta_ms[r.delta_ms.length - 1] ?? 0;
    expect(Math.abs(actual - expected)).toBeLessThan(60);
  });

  it('reports a negative delta when lap A is genuinely quicker', () => {
    const quick = 2;
    const slow = 7;
    const r = mockCompare(HEADLINE_SESSION_ID, quick, HEADLINE_SESSION_ID, slow);
    if (!isCompare(r)) throw new Error('expected a comparison');
    expect(lapTimeMs(quick)).toBeLessThan(lapTimeMs(slow));
    expect(r.delta_ms[r.delta_ms.length - 1]).toBeLessThan(0);
  });
});

describe('corners', () => {
  it('segments the lap into 15 corners with ordered, non-overlapping bounds', () => {
    const r = mockCorners(HEADLINE_SESSION_ID, 12, 'best');
    expect(isCorners(r)).toBe(true);
    if (!isCorners(r)) return;

    expect(r.corners).toHaveLength(15);
    r.corners.forEach((c, i) => {
      expect(c.n).toBe(i + 1);
      expect(c.entry_m).toBeLessThan(c.apex_m);
      expect(c.apex_m).toBeLessThan(c.exit_m);
      expect(c.exit_m).toBeLessThanOrEqual(BAHRAIN_LENGTH_M);
      expect(['slow', 'medium', 'fast']).toContain(c.kind);
      if (i > 0) {
        expect(c.entry_m).toBeGreaterThan(r.corners[i - 1]?.exit_m as number);
      }
    });
  });

  it('accounts for the whole lap: corners plus straights equals the total', () => {
    const r = mockCorners(HEADLINE_SESSION_ID, 7, 'best');
    if (!isCorners(r)) throw new Error('expected corners');

    const sum = r.corners.reduce((a, c) => a + c.time_loss_ms, 0) + r.straights_time_loss_ms;
    expect(sum).toBe(r.total_delta_ms);
  });

  it('blames the corner the lap was actually botched in', () => {
    // Lap 7 carries a scripted late brake into turn 10.
    const r = mockCorners(HEADLINE_SESSION_ID, 7, 'best');
    if (!isCorners(r)) throw new Error('expected corners');

    const worst = [...r.corners].sort((a, b) => b.time_loss_ms - a.time_loss_ms)[0];
    expect(worst?.n).toBe(10);
    expect(worst?.time_loss_ms).toBeGreaterThan(100);
    // Braked measurably later than the reference into it.
    expect((worst?.brake_point_m ?? 0) - (worst?.ref_brake_point_m ?? 0)).toBeGreaterThan(5);
    // And the min speed through it must be down on the reference.
    expect(worst?.min_speed_kmh).toBeLessThan(worst?.ref_min_speed_kmh as number);
  });

  it('resolves ref=best to a valid lap, and rejects a nonsense ref', () => {
    const r = mockCorners(HEADLINE_SESSION_ID, 12, 'best');
    if (!isCorners(r)) throw new Error('expected corners');
    expect(r.ref_lap_number).toBeGreaterThan(0);

    const bad = mockCorners(HEADLINE_SESSION_ID, 12, 999);
    expect((bad as { status: number }).status).toBe(422);
  });
});

describe('stints', () => {
  it('splits the race into two stints on different compounds', () => {
    const r = mockStints(HEADLINE_SESSION_ID);
    expect(isStints(r)).toBe(true);
    if (!isStints(r)) return;

    expect(r.stints).toHaveLength(2);
    expect(r.stints[0]?.compound_visual).not.toBe(r.stints[1]?.compound_visual);
    expect(r.stints[0]?.lap_start).toBe(1);
    expect(r.stints[1]?.lap_end).toBe(20);
  });

  it('covers every lap exactly once across the stints', () => {
    const r = mockStints(HEADLINE_SESSION_ID);
    if (!isStints(r)) throw new Error('expected stints');

    const seen = r.stints.flatMap((s) => s.laps.map((l) => l.lap_number));
    expect(new Set(seen).size).toBe(seen.length);
    expect(seen.sort((a, b) => a - b)).toEqual(Array.from({ length: 20 }, (_, i) => i + 1));
  });

  it('excludes in/out laps and gives every exclusion a reason', () => {
    const r = mockStints(HEADLINE_SESSION_ID);
    if (!isStints(r)) throw new Error('expected stints');

    const excluded = r.stints.flatMap((s) => s.laps.filter((l) => l.excluded));
    expect(excluded.length).toBeGreaterThanOrEqual(3);
    for (const l of excluded) expect(l.exclude_reason).toBeTruthy();

    // The in-lap and the out-lap around the stop are both out.
    const all = r.stints.flatMap((s) => s.laps);
    expect(all.find((l) => l.lap_number === 11)?.excluded).toBe(true);
    expect(all.find((l) => l.lap_number === 12)?.excluded).toBe(true);
  });

  it('fits a positive degradation slope with a believable r²', () => {
    const r = mockStints(HEADLINE_SESSION_ID);
    if (!isStints(r)) throw new Error('expected stints');

    for (const stint of r.stints) {
      expect(stint.fit).not.toBeNull();
      if (!stint.fit) continue;
      expect(stint.fit.n_used).toBeGreaterThanOrEqual(2);
      expect(stint.fit.r2).toBeGreaterThanOrEqual(0);
      expect(stint.fit.r2).toBeLessThanOrEqual(1);
      expect(stint.fit.base_ms).toBeGreaterThan(80_000);
      // Tyres go off; a fit that says otherwise means the model is broken.
      expect(stint.fit.deg_ms_per_lap).toBeGreaterThan(0);
      // And the slope must dominate the scatter, or the chart teaches nothing.
      expect(stint.fit.r2).toBeGreaterThan(0.5);
    }
  });

  it('reports four wear values per stint, in wheel order', () => {
    const r = mockStints(HEADLINE_SESSION_ID);
    if (!isStints(r)) throw new Error('expected stints');
    for (const stint of r.stints) {
      expect(stint.wear_end_pct).toHaveLength(4);
      for (const w of stint.wear_end_pct ?? []) {
        expect(w).toBeGreaterThanOrEqual(0);
        expect(w).toBeLessThanOrEqual(100);
      }
    }
  });
});

describe('request routing', () => {
  it('routes every documented endpoint', () => {
    const paths = [
      '/api/sessions?limit=50',
      `/api/sessions/${HEADLINE_SESSION_ID}`,
      `/api/sessions/${HEADLINE_SESSION_ID}/laps?car_index=${PLAYER_CAR_INDEX}`,
      `/api/sessions/${HEADLINE_SESSION_ID}/laps/12/telemetry`,
      `/api/analysis/compare?session_a=3&lap_a=12&session_b=3&lap_b=9`,
      `/api/analysis/corners?session_id=3&lap=12&ref=best`,
      `/api/analysis/stints?session_id=3`,
      `/api/sessions/${HEADLINE_SESSION_ID}/debrief`
    ];
    for (const p of paths) {
      const res = handleMockRequest(p);
      expect(res, p).not.toBeNull();
      expect(res?.status, p).toBe(200);
    }
  });

  it('ignores anything that is not an API path', () => {
    expect(handleMockRequest('/index.html')).toBeNull();
    expect(handleMockRequest('/assets/app.js')).toBeNull();
  });

  it('rejects a compare call missing its parameters', () => {
    expect(handleMockRequest('/api/analysis/compare?session_a=3')?.status).toBe(422);
  });

  it('serves a debrief for the race and a 404 for the sessions without one', () => {
    // The absent state is only reachable in dev if some session genuinely lacks a
    // debrief, so at least one fixture session has to answer 404.
    const present = handleMockRequest(`/api/sessions/${HEADLINE_SESSION_ID}/debrief`);
    expect(present?.status).toBe(200);

    const missing = mockSessions()
      .map((s) => s.id)
      .filter((id) => id !== HEADLINE_SESSION_ID)
      .map((id) => handleMockRequest(`/api/sessions/${id}/debrief`));
    expect(missing.length).toBeGreaterThan(0);
    for (const res of missing) expect(res?.status).toBe(404);
  });

  it('quotes only numbers that are in the fact sheet it ships with', () => {
    // The mock has to demonstrate the grounding contract, not just fill space: a
    // fixture whose prose cites figures its own fact sheet does not contain would
    // model the exact failure the real feature is built to prevent.
    const debrief = mockDebrief(HEADLINE_SESSION_ID);
    expect(debrief).not.toBeNull();
    const sheet = JSON.stringify(debrief?.fact_sheet);
    for (const quoted of ['1:33.412', '1:34.180', '0.62', '148', '84']) {
      expect(sheet, quoted).toContain(quoted);
      expect(debrief?.text, quoted).toContain(quoted);
    }
    expect(debrief?.prompt_version).toBe(1);
    expect(debrief?.model).toBeTruthy();
  });
});

describe('career derivation behaves like the engine', () => {
  /** Recompute a totals block from the weekends it claims to summarise. */
  function recount(weekends: readonly CareerWeekend[]): CareerTotals {
    const t: CareerTotals = {
      points: 0,
      wins: 0,
      podiums: 0,
      poles: 0,
      fastest_laps: 0,
      sprint_wins: 0,
      races: 0
    };
    for (const w of weekends) {
      if (w.points !== null) t.points += w.points;
      if (w.race) {
        t.races += 1;
        if (w.race.position === 1) t.wins += 1;
        if (w.race.position !== null && w.race.position <= 3) t.podiums += 1;
        if (w.race.fastest_lap === true) t.fastest_laps += 1;
      }
      if (w.quali?.position === 1) t.poles += 1;
      if (w.sprint?.position === 1) t.sprint_wins += 1;
    }
    return t;
  }

  it('groups five weekends into two seasons, incrementing on the track repeat', () => {
    const overview = mockCareerOverview();
    expect(overview.seasons.map((s) => s.season)).toEqual([1, 2]);
    expect(overview.seasons[0]?.rounds).toBe(4);
    expect(overview.seasons[1]?.rounds).toBe(1);

    // Rounds are the 1-based chronological index within the season.
    const s1 = overview.seasons[0]!.weekends;
    expect(s1.map((w) => w.round)).toEqual([1, 2, 3, 4]);
    const starts = s1.map((w) => w.started_at_wall ?? 0);
    expect([...starts].sort((a, b) => a - b)).toEqual(starts);

    // Season 2 exists because a circuit repeated, and it is that circuit.
    const s2 = overview.seasons[1]!.weekends[0]!;
    expect(s2.track_id).toBe(MIAMI_TRACK_ID);
    expect(s1.some((w) => w.track_id === MIAMI_TRACK_ID)).toBe(true);
  });

  it('builds a weekend from the consecutive same-track run, time trials excluded', () => {
    const overview = mockCareerOverview();
    const weekends = overview.seasons.flatMap((s) => s.weekends);

    const miami = weekends.find((w) => w.season === 1 && w.track_id === MIAMI_TRACK_ID)!;
    expect(miami.session_ids).toEqual([6, 7, 8, 9, 10]);
    expect(miami.sessions).toHaveLength(5);

    // The Bahrain time trial sits chronologically inside the weekend and is
    // still not part of it: not a career session.
    const bahrain = weekends.find((w) => w.track_id === BAHRAIN_TRACK_ID)!;
    expect(bahrain.session_ids).not.toContain(BAHRAIN_TT_SESSION_ID);
    expect(overview.untracked_sessions).toBe(1);
  });

  it('calls the last race-type session the grand prix and reads poles off quali only', () => {
    const overview = mockCareerOverview();
    const miami = overview.seasons[0]!.weekends.find((w) => w.track_id === MIAMI_TRACK_ID)!;

    expect(miami.format).toBe('sprint');
    expect(miami.race?.session_id).toBe(MIAMI_GP_SESSION_ID);
    expect(miami.sprint?.session_id).toBe(MIAMI_SPRINT_SESSION_ID);
    // The shootout classified P1, but it sets the sprint grid, not a pole:
    // the weekend's quali is the one-shot session and it was P2.
    expect(miami.quali?.session_id).toBe(9);
    expect(miami.quali?.position).toBe(2);
    expect(overview.seasons[0]?.totals.poles).toBe(1);
    // The sprint win is counted separately from the GP result.
    expect(miami.sprint?.position).toBe(1);
    expect(overview.seasons[0]?.totals.sprint_wins).toBe(1);
  });

  it('keeps every total equal to the sum of what its weekends show', () => {
    const overview = mockCareerOverview();
    for (const season of overview.seasons) {
      expect(season.totals).toEqual(recount(season.weekends));
    }
    expect(overview.career_totals).toEqual(
      recount(overview.seasons.flatMap((s) => s.weekends))
    );
    // And the fixture actually exercises the counters.
    expect(overview.career_totals.points).toBeGreaterThan(0);
    expect(overview.career_totals.wins).toBeGreaterThan(0);
    expect(overview.career_totals.podiums).toBeGreaterThan(0);
  });

  it('shows a race whose classification never landed as nulls, not zeros', () => {
    const overview = mockCareerOverview();
    const weekend = overview.seasons[1]!.weekends[0]!;

    expect(weekend.race?.session_id).toBe(MIAMI_S2_RACE_SESSION_ID);
    expect(weekend.race?.position).toBeNull();
    expect(weekend.race?.points).toBeNull();
    expect(weekend.race?.status).toBeNull();
    expect(weekend.race?.fastest_lap).toBeNull();
    // The laps were still recorded, so the best lap is a real number.
    expect(weekend.race?.best_lap_ms).toBeGreaterThan(0);
    // An unknown is never summed into a points figure.
    expect(weekend.points).toBeNull();
  });

  it('keeps a practice-only visit as a round, with nothing invented for it', () => {
    const overview = mockCareerOverview();
    const monza = overview.seasons[0]!.weekends.find((w) => w.track_id === 11)!;
    expect(monza.race).toBeNull();
    expect(monza.sprint).toBeNull();
    expect(monza.quali).toBeNull();
    expect(monza.consistency).toBeNull();
    // No race-type sessions means zero points scored — a known fact, not a gap.
    expect(monza.points).toBe(0);
  });

  it('resolves disagreeing tags newest-first and flags the conflict', () => {
    const overview = mockCareerOverview();
    const weekends = overview.seasons.flatMap((s) => s.weekends);
    const pinned = weekends.filter((w) => w.tags !== null);
    expect(pinned).toHaveLength(1);

    const weekend = pinned[0]!;
    expect(weekend.track_id).toBe(MIAMI_TRACK_ID);
    expect(weekend.tags).toEqual({
      season: 2,
      round: 1,
      note: 'season 2 opener, pinned by hand'
    });
    expect(weekend.tag_conflict).toBe(true);
    expect(weekend.season).toBe(2);
    expect(weekend.round).toBe(1);
    for (const other of weekends.filter((w) => w !== weekend)) {
      expect(other.tag_conflict).toBe(false);
    }
  });

  it('credits the Bahrain PB to the time trial and orders the board by first visit', () => {
    const overview = mockCareerOverview();
    expect(overview.pbs.map((p) => p.track_id)).toEqual([
      MIAMI_TRACK_ID,
      MONTREAL_TRACK_ID,
      11,
      BAHRAIN_TRACK_ID
    ]);

    const bahrain = overview.pbs.find((p) => p.track_id === BAHRAIN_TRACK_ID)!;
    expect(bahrain.session_id).toBe(BAHRAIN_TT_SESSION_ID);
    expect(bahrain.best_lap_ms).toBeGreaterThan(0);
  });

  it('derives a theoretical best that can never beat itself', () => {
    const overview = mockCareerOverview();
    for (const pb of overview.pbs) {
      expect(pb.best_lap_ms).toBeGreaterThan(0);
      expect(pb.theoretical_ms).not.toBeNull();
      // Best sectors can only equal the best lap's own, give or take the
      // per-sector rounding the lap fixtures already tolerate.
      expect(pb.theoretical_ms!).toBeLessThanOrEqual(pb.best_lap_ms! + 3);
      expect(pb.top_speed_kmh).toBeGreaterThan(0);
    }
  });

  it('serves a track page whose visits agree with the overview', () => {
    const overview = mockCareerOverview();
    const track = mockCareerTrack(MIAMI_TRACK_ID) as CareerTrackResponse;

    expect(track.visits.map((v) => [v.season, v.round])).toEqual([
      [1, 1],
      [2, 1]
    ]);
    const fromOverview = overview.seasons
      .flatMap((s) => s.weekends)
      .filter((w) => w.track_id === MIAMI_TRACK_ID);
    track.visits.forEach((visit, i) => {
      expect(visit.session_ids).toEqual(fromOverview[i]!.session_ids);
      expect(visit.race).toEqual(fromOverview[i]!.race);
    });

    // The career progressed: the second visit is quicker than the first.
    expect(track.visits[1]!.best_lap_ms!).toBeLessThan(track.visits[0]!.best_lap_ms!);

    // Sector PBs are the parts the theoretical best is the sum of.
    const sectors = track.pb!.sectors!;
    expect(sectors.s1_ms! + sectors.s2_ms! + sectors.s3_ms!).toBe(track.pb!.theoretical_ms);
    for (const sid of [sectors.s1_session_id, sectors.s2_session_id, sectors.s3_session_id]) {
      expect(track.sessions.some((s) => s.id === sid)).toBe(true);
    }
  });

  it('lists every session at the circuit, time trial included, chronologically', () => {
    const track = mockCareerTrack(BAHRAIN_TRACK_ID) as CareerTrackResponse;
    expect(track.sessions.map((s) => s.id)).toEqual([BAHRAIN_TT_SESSION_ID, 2, 3]);
    // The visit still excludes it.
    expect(track.visits).toHaveLength(1);
    expect(track.visits[0]!.session_ids).toEqual([2, 3]);
    // Per-session consistency carries real numbers where laps exist.
    for (const session of track.sessions) {
      expect(session.laps_used).toBeGreaterThan(0);
      expect(session.median_ms).toBeGreaterThan(0);
      expect(session.cv_pct).toBeGreaterThanOrEqual(0);
    }
  });

  it('404s a circuit with no sessions', () => {
    expect(mockCareerTrack(999)).toMatchObject({ status: 404 });
  });

  it('routes both career endpoints', () => {
    expect(handleMockRequest('/api/career/overview')?.status).toBe(200);
    expect(handleMockRequest(`/api/career/tracks/${MIAMI_TRACK_ID}`)?.status).toBe(200);
    expect(handleMockRequest('/api/career/tracks/999')?.status).toBe(404);
  });
});

describe('fetch interception', () => {
  it('answers /api requests and restores the original fetch on teardown', async () => {
    const before = globalThis.fetch;
    const restore = installAnalysisMock();
    try {
      const res = await fetch('/api/sessions');
      expect(res.status).toBe(200);
      const body = (await res.json()) as unknown[];
      expect(Array.isArray(body)).toBe(true);
      expect(body.length).toBeGreaterThan(0);
    } finally {
      restore();
    }
    expect(globalThis.fetch).toBe(before);
  });

  it('can force a 503, which is how the unavailable panel is exercised', async () => {
    const restore = installAnalysisMock({ failStatus: 503 });
    try {
      const res = await fetch('/api/sessions');
      expect(res.status).toBe(503);
      expect(await res.json()).toEqual({ detail: 'database unavailable' });
    } finally {
      restore();
    }
  });
});
