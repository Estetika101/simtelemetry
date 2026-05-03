"""
Seed a SQLite database with demo sessions for screenshot capture.

Usage:
    python3 scripts/seed_demo_data.py --db /tmp/demo.db

Creates realistic sessions for Forza Motorsport, ACC, and F1 across
Suzuka, Spa-Francorchamps, and Monza — enough to populate the career
dashboard, circuit table, best-lap pills, and telemetry charts.
"""

import argparse
import json
import math
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    game             TEXT,
    track            TEXT,
    car              TEXT,
    session_type     TEXT,
    race_type        TEXT,
    started_at       TEXT,
    ended_at         TEXT,
    packet_count     INTEGER DEFAULT 0,
    best_lap_time_s  REAL,
    lap_count        INTEGER DEFAULT 0,
    ai_analysis      TEXT,
    ai_analyzed_at   TEXT,
    ai_model         TEXT,
    grid_pos         INTEGER,
    finish_pos       INTEGER
);
CREATE TABLE IF NOT EXISTS laps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    lap_number    INTEGER,
    lap_time_s    REAL,
    max_speed_mph REAL,
    sample_count  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS track_tips (
    track        TEXT PRIMARY KEY,
    tip          TEXT,
    generated_at TEXT,
    model        TEXT
);
CREATE TABLE IF NOT EXISTS track_references (
    track                      TEXT NOT NULL,
    reference_type             TEXT NOT NULL,
    session_id                 TEXT,
    lap_number                 INTEGER,
    samples_json               TEXT NOT NULL,
    theoretical_s1_s           REAL,
    theoretical_s1_session_id  TEXT,
    theoretical_s1_lap         INTEGER,
    theoretical_s2_s           REAL,
    theoretical_s2_session_id  TEXT,
    theoretical_s2_lap         INTEGER,
    theoretical_s3_s           REAL,
    theoretical_s3_session_id  TEXT,
    theoretical_s3_lap         INTEGER,
    distance_m_json            TEXT,
    PRIMARY KEY (track, reference_type)
);
CREATE TABLE IF NOT EXISTS lap_samples (
    session_id    TEXT NOT NULL,
    lap_number    INTEGER NOT NULL,
    samples_json  TEXT NOT NULL,
    distance_m_json TEXT NOT NULL,
    created_at    TEXT,
    PRIMARY KEY (session_id, lap_number)
);
CREATE INDEX IF NOT EXISTS idx_laps_session    ON laps(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_track  ON sessions(track);
CREATE INDEX IF NOT EXISTS idx_sessions_start  ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_lap_samples_session ON lap_samples(session_id);
CREATE INDEX IF NOT EXISTS idx_track_refs_track ON track_references(track);
"""

# ── Circuit definitions ────────────────────────────────────────────────────────
# Each circuit is a list of (distance_frac, speed_mph, throttle, brake, gear, steer)
# tuples at key points. Values are interpolated between points to generate samples.

CIRCUITS = {
    "Suzuka Circuit": {
        "lap_m": 5807,
        "base_lap_s": 107.0,  # ~1:47 for GT car
        "waypoints": [
            # dist  spd   thr  brk  g  str
            (0.00,  95,   100,   0,  3,  0.0),   # start/finish
            (0.04, 175,   100,   0,  6,  0.0),   # straight
            (0.07,  80,     0,  90,  2,  0.3),   # Turn 1 brake
            (0.10, 100,    80,   0,  3, -0.2),   # Turn 1 exit
            (0.14, 140,   100,   0,  5,  0.1),   # S curves entry
            (0.17,  90,    40,  30,  3,  0.4),   # S curve left
            (0.20, 110,    90,   0,  4, -0.3),   # S curve right
            (0.24, 155,   100,   0,  5,  0.0),   # Dunlop straight
            (0.27,  65,     0,  95,  2,  0.2),   # Dunlop chicane
            (0.30,  90,    85,   0,  3, -0.2),
            (0.34, 145,   100,   0,  5,  0.0),   # Degner straight
            (0.37,  55,     0, 100,  1,  0.4),   # Degner 1 brake
            (0.39,  75,    80,   0,  3, -0.1),   # Degner exit
            (0.43, 130,   100,   0,  4,  0.0),   # Hairpin approach
            (0.46,  45,     0, 100,  1,  0.5),   # Hairpin brake
            (0.49,  65,    75,   0,  2, -0.4),   # Hairpin exit
            (0.53, 155,   100,   0,  5,  0.0),   # Spoon straight
            (0.60,  70,     0,  95,  2,  0.3),   # Spoon curve brake
            (0.65, 100,    90,   0,  3, -0.2),
            (0.70, 170,   100,   0,  6,  0.0),   # Back straight
            (0.75, 185,   100,   0,  6,  0.0),   # 130R approach
            (0.78, 160,    90,  10,  5,  0.3),   # 130R entry
            (0.82, 170,   100,   0,  6,  0.1),   # 130R mid
            (0.85,  80,     0,  95,  2,  0.4),   # Casio chicane brake
            (0.88,  95,    80,   0,  3, -0.3),   # Casio exit
            (0.92, 140,   100,   0,  5,  0.0),   # Final straight entry
            (0.96, 160,   100,   0,  5,  0.0),   # Finish straight
            (1.00,  95,   100,   0,  3,  0.0),   # lap complete
        ],
    },
    "Spa-Francorchamps": {
        "lap_m": 7004,
        "base_lap_s": 133.0,  # ~2:13 for GT car
        "waypoints": [
            (0.00, 110,   100,   0,  4,  0.0),   # La Source exit
            (0.03, 165,   100,   0,  6,  0.0),   # Kemmel straight
            (0.07, 190,   100,   0,  6,  0.0),   # Raidillon approach
            (0.10, 155,    85,  15,  5,  0.2),   # Raidillon
            (0.13, 175,   100,   0,  6, -0.1),   # Eau Rouge exit
            (0.18, 195,   100,   0,  6,  0.0),   # Kemmel top
            (0.22,  70,     0, 100,  2,  0.3),   # Les Combes brake
            (0.25,  95,    80,   0,  3, -0.2),
            (0.28, 145,   100,   0,  5,  0.0),   # Malmedy
            (0.31,  75,     0,  95,  2,  0.4),   # Malmedy brake
            (0.34, 105,    90,   0,  3, -0.1),
            (0.40, 160,   100,   0,  5,  0.0),   # Rivage straight
            (0.43,  55,     0, 100,  1,  0.5),   # Rivage hairpin
            (0.46,  80,    80,   0,  3, -0.3),
            (0.52, 155,   100,   0,  5,  0.0),   # Pouhon approach
            (0.56, 145,    90,  10,  5,  0.3),   # Pouhon
            (0.60, 155,   100,   0,  5, -0.1),
            (0.65, 130,   100,   0,  4,  0.0),   # Fagnes
            (0.68,  80,     0,  90,  2,  0.3),   # Fagnes chicane
            (0.72, 110,    90,   0,  4, -0.1),
            (0.76, 170,   100,   0,  6,  0.0),   # Blanchimont straight
            (0.80, 175,   100,   0,  6,  0.0),   # Blanchimont
            (0.84,  60,     0, 100,  1,  0.4),   # Bus stop chicane
            (0.87,  80,    75,   0,  3, -0.3),
            (0.90, 120,   100,   0,  4,  0.0),   # Final sector
            (0.94, 145,   100,   0,  5,  0.0),
            (0.97,  85,     0,  80,  2,  0.4),   # La Source brake
            (1.00, 110,   100,   0,  4,  0.0),
        ],
    },
    "Monza": {
        "lap_m": 5793,
        "base_lap_s": 102.0,  # ~1:42 for F1 car
        "waypoints": [
            (0.00, 185,   100,   0,  7,  0.0),   # start
            (0.06, 200,   100,   0,  8,  0.0),   # full throttle
            (0.11,  65,     0, 100,  1,  0.3),   # Prima Variante brake
            (0.14,  95,    85,   0,  3, -0.2),
            (0.17, 175,   100,   0,  7,  0.0),   # Curva Grande
            (0.22, 165,    95,  10,  6,  0.2),   # Curva Grande apex
            (0.25, 175,   100,   0,  7, -0.1),
            (0.30,  70,     0, 100,  2,  0.3),   # Seconda Variante
            (0.33,  90,    80,   0,  3, -0.2),
            (0.38, 190,   100,   0,  7,  0.0),   # Lesmo straight
            (0.42,  90,     0,  90,  3,  0.4),   # Lesmo 1
            (0.45, 115,    90,   0,  4, -0.2),
            (0.48,  95,     0,  85,  3,  0.3),   # Lesmo 2
            (0.51, 120,    95,   0,  4, -0.1),
            (0.56, 195,   100,   0,  7,  0.0),   # Serraglio straight
            (0.61, 200,   100,   0,  8,  0.0),   # Variante Ascari approach
            (0.65,  80,     0, 100,  2,  0.3),   # Variante Ascari
            (0.68,  95,    80,   0,  3, -0.3),
            (0.70, 105,    90,   0,  3,  0.2),
            (0.73, 190,   100,   0,  7,  0.0),   # Back straight
            (0.79, 205,   100,   0,  8,  0.0),   # main straight
            (0.84,  65,     0, 100,  1,  0.4),   # Parabolica brake
            (0.88, 100,    85,   0,  3, -0.3),   # Parabolica
            (0.92, 140,   100,   0,  5,  0.1),
            (0.96, 180,   100,   0,  7,  0.0),
            (1.00, 185,   100,   0,  7,  0.0),
        ],
    },
}

CARS = {
    "forza_motorsport": [
        "Porsche 911 GT3 RS",
        "Ferrari 488 GT3",
        "BMW M4 GT3",
        "Aston Martin Vantage GT3",
    ],
    "acc": [
        "Ferrari 488 GT3 Evo",
        "McLaren 720S GT3",
        "Porsche 992 GT3 R",
    ],
    "f1": [
        "Mercedes AMG F1 W14",
        "Red Bull RB19",
        "Ferrari SF-23",
    ],
}

AI_ANALYSES = {
    "Suzuka Circuit": (
        "Your pace at Suzuka has improved by 1.8 seconds across your last 6 sessions — "
        "a meaningful gain driven by cleaner exits at the Hairpin and better commitment "
        "through 130R. Throttle traces show you're now hitting full throttle 0.3s earlier "
        "on the back straight compared to your first session here.\n\n"
        "The area still leaking time is the S-Curves. You're losing approximately 0.4s "
        "through turns 3–7 compared to your reference lap — the data shows inconsistent "
        "entry speeds, suggesting you're not fully committing to the geometric line.\n\n"
        "Focus: drive a straighter line through the S-Curves. The ideal path sacrifices "
        "T3 slightly to set up a faster exit from T4 — your current line optimises the "
        "wrong apex. Your pace everywhere else is already competitive."
    ),
    "Spa-Francorchamps": (
        "Spa rewards commitment through Eau Rouge/Raidillon, and your data shows you're "
        "leaving roughly 0.6s on the table there. Your minimum speed through Raidillon "
        "has improved 8 mph over your sessions here but is still 12 mph below your "
        "theoretical best.\n\n"
        "Blanchimont is a highlight — you're consistently flat, which puts you ahead of "
        "most drivers. Bus Stop is your biggest single-corner loss: you brake 15m earlier "
        "than your best lap, adding 0.3s every time.\n\n"
        "The longer your session at Spa, the better your times get — your best lap is "
        "always in the last third of a session. Tyre warm-up is critical here."
    ),
    "Monza": (
        "Monza is about bravery at the braking zones and power application on the exits. "
        "Your braking for Prima Variante is solid — consistently within 5m of your best "
        "marker. Parabolica is the key corner: your exit speed there sets up your whole "
        "main straight speed.\n\n"
        "Current analysis: you're losing 0.4s at Parabolica through under-rotation. "
        "You apex too early, run wide, and have to ease throttle on the exit. A later "
        "apex allows earlier full throttle and an extra 8 mph onto the straight.\n\n"
        "With a clean Parabolica your current pace is genuinely podium-competitive here."
    ),
}

TRACK_TIPS = {
    "Suzuka Circuit": (
        "Suzuka rewards smoothness above all else. The S-Curves require a flowing, "
        "connected driving style — treat turns 3-7 as one continuous movement rather "
        "than individual corners. Commit to 130R flat — if you lift, you lose the lap. "
        "The Hairpin is the slowest corner on the circuit; nail the exit and you gain "
        "the entire back straight."
    ),
    "Spa-Francorchamps": (
        "Eau Rouge / Raidillon is the defining corner sequence. The key is carrying speed "
        "through the compression at the bottom — a flat approach and early turn-in gets "
        "the car settled before the crest. Pouhon is a critical corner for sector 2 time. "
        "Tyres matter enormously at Spa — don't push until the rubber is fully up to temp."
    ),
    "Monza": (
        "Monza is all about maximising straight-line speed and getting the braking zones "
        "right. The chicanes require precise kerb use — the inside kerb at Prima Variante "
        "is your friend. Parabolica is the lap-defining corner: get the exit right and "
        "you carry speed all the way to the finish line."
    ),
}


def lerp(a, b, t):
    return a + (b - a) * t


def interp_waypoints(waypoints, n_samples):
    """Interpolate between waypoints to produce n_samples."""
    samples = []
    pts = waypoints
    for i in range(n_samples):
        t = i / (n_samples - 1)
        # find surrounding waypoints
        j = 0
        while j < len(pts) - 2 and pts[j + 1][0] <= t:
            j += 1
        p0 = pts[j]
        p1 = pts[j + 1]
        span = p1[0] - p0[0]
        local_t = (t - p0[0]) / span if span > 0 else 0.0
        speed    = lerp(p0[1], p1[1], local_t)
        throttle = lerp(p0[2], p1[2], local_t)
        brake    = lerp(p0[3], p1[3], local_t)
        gear     = round(lerp(p0[4], p1[4], local_t))
        steer    = lerp(p0[5], p1[5], local_t)
        samples.append((t, speed, throttle, brake, gear, steer))
    return samples


def make_lap_samples(circuit_name, lap_time_s, noise=0.03):
    """Generate realistic-looking telemetry samples for one lap."""
    info = CIRCUITS[circuit_name]
    n = 350
    raw = interp_waypoints(info["waypoints"], n)
    lap_m = info["lap_m"]
    rng = random.Random(hash(circuit_name) ^ int(lap_time_s * 1000))

    samples = []
    cum_dist = 0.0
    dist_m = []

    for i, (frac, spd, thr, brk, gear, steer) in enumerate(raw):
        # Add gentle noise
        spd   = max(30,  spd   + rng.gauss(0, spd   * noise * 0.3))
        thr   = max(0, min(100, thr + rng.gauss(0, 3)))
        brk   = max(0, min(100, brk + rng.gauss(0, 2)))
        steer = steer + rng.gauss(0, 0.02)

        t = frac * lap_time_s

        # Derived values
        rpm  = int(3000 + (gear - 1) * 1200 + (thr / 100) * 2500 + rng.gauss(0, 100))
        rpm  = max(1000, min(9000, rpm))
        g_lon = (thr - brk) / 100 * 1.5 + rng.gauss(0, 0.1)
        g_lat = steer * 2.5 + rng.gauss(0, 0.15)

        slip_rl = max(0, (thr / 100 * 0.04) + rng.gauss(0, 0.005))
        slip_rr = max(0, (thr / 100 * 0.04) + rng.gauss(0, 0.005))

        # Distance
        step = (frac - raw[i - 1][0]) * lap_m if i > 0 else 0.0
        cum_dist += step

        s = {
            "t":            round(t, 3),
            "speed_mph":    round(spd, 1),
            "throttle_pct": round(max(0, min(100, thr)), 1),
            "brake_pct":    round(max(0, min(100, brk)), 1),
            "clutch_pct":   0.0,
            "gear":         gear,
            "steer":        round(steer, 3),
            "rpm":          rpm,
            "slip_rl":      round(slip_rl, 4),
            "slip_rr":      round(slip_rr, 4),
            "g_lat":        round(g_lat, 3),
            "g_lon":        round(g_lon, 3),
            "distance_norm": round(frac, 6),
        }
        samples.append(s)
        dist_m.append(round(cum_dist, 2))

    return samples, dist_m


def make_sessions():
    """Return list of (session_dict, laps_list) to seed."""
    sessions = []
    base_dt = datetime(2025, 9, 1, 18, 0, 0)

    def dt_str(d):
        return d.strftime("%Y-%m-%dT%H:%M:%S")

    offset = 0

    def next_dt(hours_forward):
        nonlocal offset
        offset += hours_forward
        return base_dt + timedelta(hours=offset)

    # ── Forza Motorsport: Suzuka ──────────────────────────────────────────────
    fm_suzuka = [
        # session_id, grid, finish, laps, lap_base_s
        ("fm-suz-001", 8,  5, 12, 109.8),
        ("fm-suz-002", 5,  3, 12, 108.4),
        ("fm-suz-003", 3,  2, 12, 107.9),
        ("fm-suz-004", 4,  1, 14, 107.1),
        ("fm-suz-005", 2,  2, 14, 107.3),
        ("fm-suz-006", 1,  1, 16, 106.8),
    ]
    for sid, grid, finish, nlaps, base_s in fm_suzuka:
        started = next_dt(2)
        ended   = started + timedelta(minutes=nlaps * base_s / 60 + 3)
        laps = []
        best = None
        for ln in range(1, nlaps + 1):
            lt = base_s + random.gauss(0.4, 0.3)
            if best is None or lt < best:
                best = lt
            laps.append({"lap_number": ln, "lap_time_s": round(lt, 3),
                          "max_speed_mph": 178.0, "sample_count": 350})
        sessions.append(({
            "session_id": sid, "game": "forza_motorsport",
            "track": "Suzuka Circuit", "car": CARS["forza_motorsport"][0],
            "session_type": "race", "race_type": "real",
            "started_at": dt_str(started), "ended_at": dt_str(ended),
            "packet_count": nlaps * 350 * 60,
            "best_lap_time_s": round(best, 3), "lap_count": nlaps,
            "ai_analysis": AI_ANALYSES["Suzuka Circuit"],
            "ai_analyzed_at": dt_str(ended + timedelta(minutes=1)),
            "ai_model": "claude-sonnet-4-6",
            "grid_pos": grid, "finish_pos": finish,
        }, laps))

    # ── Forza Motorsport: Spa ─────────────────────────────────────────────────
    fm_spa = [
        ("fm-spa-001", 6, 4, 10, 135.2),
        ("fm-spa-002", 4, 2, 10, 134.1),
        ("fm-spa-003", 2, 1, 12, 133.4),
        ("fm-spa-004", 1, 1, 12, 133.0),
    ]
    for sid, grid, finish, nlaps, base_s in fm_spa:
        started = next_dt(2)
        ended   = started + timedelta(minutes=nlaps * base_s / 60 + 3)
        laps = []
        best = None
        for ln in range(1, nlaps + 1):
            lt = base_s + random.gauss(0.5, 0.4)
            if best is None or lt < best:
                best = lt
            laps.append({"lap_number": ln, "lap_time_s": round(lt, 3),
                          "max_speed_mph": 192.0, "sample_count": 350})
        sessions.append(({
            "session_id": sid, "game": "forza_motorsport",
            "track": "Spa-Francorchamps", "car": CARS["forza_motorsport"][1],
            "session_type": "race", "race_type": "real",
            "started_at": dt_str(started), "ended_at": dt_str(ended),
            "packet_count": nlaps * 350 * 60,
            "best_lap_time_s": round(best, 3), "lap_count": nlaps,
            "ai_analysis": AI_ANALYSES["Spa-Francorchamps"],
            "ai_analyzed_at": dt_str(ended + timedelta(minutes=1)),
            "ai_model": "claude-sonnet-4-6",
            "grid_pos": grid, "finish_pos": finish,
        }, laps))

    # ── ACC: Spa ──────────────────────────────────────────────────────────────
    acc_spa = [
        ("acc-spa-001", 5, 3, 8, 136.8),
        ("acc-spa-002", 3, 2, 8, 135.5),
        ("acc-spa-003", 2, 1, 8, 134.9),
    ]
    for sid, grid, finish, nlaps, base_s in acc_spa:
        started = next_dt(3)
        ended   = started + timedelta(minutes=nlaps * base_s / 60 + 3)
        laps = []
        best = None
        for ln in range(1, nlaps + 1):
            lt = base_s + random.gauss(0.6, 0.4)
            if best is None or lt < best:
                best = lt
            laps.append({"lap_number": ln, "lap_time_s": round(lt, 3),
                          "max_speed_mph": 190.0, "sample_count": 350})
        sessions.append(({
            "session_id": sid, "game": "acc",
            "track": "Spa-Francorchamps", "car": CARS["acc"][0],
            "session_type": "race", "race_type": "real",
            "started_at": dt_str(started), "ended_at": dt_str(ended),
            "packet_count": nlaps * 350 * 60,
            "best_lap_time_s": round(best, 3), "lap_count": nlaps,
            "ai_analysis": None, "ai_analyzed_at": None, "ai_model": None,
            "grid_pos": grid, "finish_pos": finish,
        }, laps))

    # ── F1: Monza ─────────────────────────────────────────────────────────────
    f1_monza = [
        ("f1-mon-001", 10, 6, 53, 103.5),
        ("f1-mon-002",  6, 4, 53, 102.8),
        ("f1-mon-003",  4, 2, 53, 102.1),
    ]
    for sid, grid, finish, nlaps, base_s in f1_monza:
        started = next_dt(4)
        ended   = started + timedelta(minutes=nlaps * base_s / 60 + 5)
        laps = []
        best = None
        for ln in range(1, nlaps + 1):
            lt = base_s + random.gauss(0.3, 0.2)
            if ln in (1, 2):
                lt += 2.0  # formation / out lap delta
            if best is None or lt < best:
                best = lt
            laps.append({"lap_number": ln, "lap_time_s": round(lt, 3),
                          "max_speed_mph": 205.0, "sample_count": 350})
        sessions.append(({
            "session_id": sid, "game": "f1",
            "track": "Monza", "car": CARS["f1"][0],
            "session_type": "race", "race_type": "real",
            "started_at": dt_str(started), "ended_at": dt_str(ended),
            "packet_count": nlaps * 350 * 60,
            "best_lap_time_s": round(best, 3), "lap_count": nlaps,
            "ai_analysis": None, "ai_analyzed_at": None, "ai_model": None,
            "grid_pos": grid, "finish_pos": finish,
        }, laps))

    return sessions


def seed(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()

    sessions = make_sessions()

    # Insert track tips
    for track, tip in TRACK_TIPS.items():
        conn.execute(
            "INSERT OR REPLACE INTO track_tips (track, tip, generated_at, model) VALUES (?,?,?,?)",
            (track, tip, datetime.now().isoformat(), "claude-sonnet-4-6"),
        )

    # Insert sessions and laps
    for sess, laps in sessions:
        conn.execute("""
            INSERT OR REPLACE INTO sessions
            (session_id, game, track, car, session_type, race_type,
             started_at, ended_at, packet_count, best_lap_time_s, lap_count,
             ai_analysis, ai_analyzed_at, ai_model, grid_pos, finish_pos)
            VALUES (:session_id,:game,:track,:car,:session_type,:race_type,
                    :started_at,:ended_at,:packet_count,:best_lap_time_s,:lap_count,
                    :ai_analysis,:ai_analyzed_at,:ai_model,:grid_pos,:finish_pos)
        """, sess)
        for lap in laps:
            conn.execute("""
                INSERT INTO laps (session_id, lap_number, lap_time_s, max_speed_mph, sample_count)
                VALUES (?, ?, ?, ?, ?)
            """, (sess["session_id"], lap["lap_number"], lap["lap_time_s"],
                  lap["max_speed_mph"], lap["sample_count"]))

    conn.commit()

    # Insert lap_samples for the best session of each circuit (drives telemetry page)
    telemetry_sessions = {
        "Suzuka Circuit":    ("fm-suz-006", 106.8),
        "Spa-Francorchamps": ("fm-spa-004", 133.0),
        "Monza":             ("f1-mon-003", 102.1),
    }
    for track, (sid, lap_s) in telemetry_sessions.items():
        samples, dist_m = make_lap_samples(track, lap_s)
        conn.execute("""
            INSERT OR REPLACE INTO lap_samples
            (session_id, lap_number, samples_json, distance_m_json, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (sid, 1, json.dumps(samples), json.dumps(dist_m), datetime.now().isoformat()))

        # Also store as track reference
        conn.execute("""
            INSERT OR REPLACE INTO track_references
            (track, reference_type, session_id, lap_number, samples_json, distance_m_json)
            VALUES (?, 'best', ?, 1, ?, ?)
        """, (track, sid, json.dumps(samples), json.dumps(dist_m)))

    conn.commit()
    conn.close()
    print(f"Seeded {len(sessions)} sessions into {db_path}")
    print(f"  Tracks: {', '.join(CIRCUITS.keys())}")
    print(f"  Games:  forza_motorsport, acc, f1")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Output SQLite database path")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()
    random.seed(args.seed)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    seed(args.db)
