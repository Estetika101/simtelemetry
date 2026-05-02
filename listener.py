"""
SimTelemetry Listener
Supports: Forza Motorsport, Assetto Corsa Competizione, F1 (Codemasters 2023/2024)
Listens on all three ports simultaneously, auto-detects game from packet size/id.
Saves raw archives and structured JSON sessions to USB storage.
Exposes local web status server at http://pi.local:8000
"""

import asyncio
import collections
import json
import logging
import os
import shutil
import socket
import sqlite3
import threading
import urllib.parse
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─── Config file ──────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "simtelemetry.config.json"

DEFAULTS: dict = {
    "storage_path":      "/mnt/usb/simtelemetry",
    "session_timeout_s": 10,
    "idle_timeout_s":    30,
    "status_port":       8000,
    "ports": {
        "forza_motorsport": 5300,
        "acc":              9996,
        "f1":               20777,
    },
    "anthropic_api_key": "",
    "anthropic_model":   "claude-sonnet-4-6",
}

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            merged = {**DEFAULTS, **saved}
            merged["ports"] = {**DEFAULTS["ports"], **saved.get("ports", {})}
            return merged
        except Exception:
            pass
    return {**DEFAULTS, "ports": {**DEFAULTS["ports"]}}

def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

config = load_config()

# Convenience accessors — always read through config so runtime updates take effect
_LOCAL_FALLBACK = Path(__file__).parent / "data"

def storage_path() -> Path:
    """Return the active storage root, falling back to a local data/ dir if USB isn't mounted."""
    p = Path(config["storage_path"])
    if p.exists():
        return p
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except OSError:
        _LOCAL_FALLBACK.mkdir(parents=True, exist_ok=True)
        return _LOCAL_FALLBACK

PORTS             = config["ports"]          # used at bind time; port changes need restart
SESSION_TIMEOUT_S = config["session_timeout_s"]
IDLE_TIMEOUT_S    = config["idle_timeout_s"]
STATUS_PORT       = config["status_port"]
LOG_LEVEL         = logging.INFO

# ─── Logging ──────────────────────────────────────────────────────────────────

# Bootstrap log dir before logger is configured; use default path if storage
# doesn't exist yet so the process doesn't crash on first run.
_log_dir = Path(config["storage_path"]) / "logs"
try:
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_handler = logging.FileHandler(_log_dir / "listener.log")
except OSError:
    _log_handler = logging.StreamHandler()  # fallback if path isn't mounted yet

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[_log_handler, logging.StreamHandler()],
)
log = logging.getLogger("simtelemetry")

# ─── Debug Console ────────────────────────────────────────────────────────────

_debug_clients: list = []
_debug_buffer: collections.deque = collections.deque(maxlen=500)

def _debug_push(line: str):
    _debug_buffer.append(line)
    for q in list(_debug_clients):
        try:
            q.put_nowait(line)
        except Exception:
            pass

class _DebugLogHandler(logging.Handler):
    def emit(self, record):
        _debug_push(self.format(record))

_dbg_log_handler = _DebugLogHandler()
_dbg_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
log.addHandler(_dbg_log_handler)

# ─── Storage Setup ────────────────────────────────────────────────────────────

def ensure_storage():
    for subdir in ["raw", "sessions", "logs"]:
        (storage_path() / subdir).mkdir(parents=True, exist_ok=True)

def disk_info() -> dict:
    """Return free/total bytes for the storage path volume."""
    try:
        usage = shutil.disk_usage(storage_path())
        return {
            "total_gb": round(usage.total / 1e9, 1),
            "used_gb":  round(usage.used  / 1e9, 1),
            "free_gb":  round(usage.free  / 1e9, 1),
        }
    except OSError:
        return {"total_gb": None, "used_gb": None, "free_gb": None}

# ─── Forza Motorsport Parser ──────────────────────────────────────────────────
# FM2023 Data Out "Car Dash" packet: 311 bytes
# Reference: https://support.forzamotorsport.net/hc/en-us/articles/21742934024211

FM_PACKET_SIZE    = 311  # Forza Motorsport 2023 / FM7 Car Dash
FM_PACKET_SIZE_FH = 331  # Forza Horizon 4 / 5 Car Dash (adds tire wear + track ordinal)

# Forza track ordinals — shared across FM7, FM2023, FH4, FH5 (user-verified)
FORZA_TRACKS = {
    0:   "Test Track Airfield",
    1:   "Test Track Airfield Drag",
    3:   "Top Gear Full Circuit",
    6:   "Yas Marina South Circuit",
    7:   "Yas Marina North Circuit",
    9:   "Yas Marina Corkscrew",
    10:  "Yas Marina Full Circuit",
    12:  "Yas Marina North Corkscrew",
    16:  "Nürburgring Grand Prix",
    18:  "Le Mans Old Mulsanne Circuit",
    21:  "Le Mans Full Circuit",
    23:  "Circuit de Spa-Francorchamps",
    25:  "Sebring Full Circuit",
    36:  "Road America West Route",
    38:  "Road America East Route",
    39:  "Road America Full Circuit",
    41:  "Watkins Glen Short Circuit",
    45:  "Watkins Glen Full Circuit",
    48:  "Road Atlanta Full Circuit",
    53:  "Silverstone Grand Prix",
    55:  "Silverstone National",
    60:  "Brands Hatch Grand Prix",
    62:  "Brands Hatch Indy Circuit",
    66:  "Laguna Seca Full Circuit",
    68:  "Homestead-Miami Speedway",
    73:  "Mugello Full Circuit",
    76:  "Catalunya Grand Prix",
    78:  "Catalunya National Circuit",
    83:  "Lime Rock Full Circuit",
    86:  "Maple Valley Full Circuit",
    92:  "Maple Valley Short Circuit",
    112: "Mid-Ohio Sports Car Course",
    113: "Eaglerock Speedway",
    114: "Grand Oak Raceway",
    115: "Hakone Circuit",
    116: "Kyalami Grand Prix Circuit",
}


# i I [51×f] [5×i: car_ordinal/class/pi/drivetrain/cylinders] [17×f] H [6×B] [3×b]
# drivetrain_type and num_cylinders are int32 per spec, not float.
FM_FORMAT    = "<iIfffffffffffffffffffffffffffffffffffffffffffffffffffiiiiifffffffffffffffffHBBBBBBbbb"
# FH4/FH5 appends: tireWearFL tireWearFR tireWearRL tireWearRR (4f) + trackOrdinal (i)
FM_FORMAT_FH = FM_FORMAT + "ffffi"

FM_FIELDS = [
    "is_race_on", "timestamp_ms",
    "engine_max_rpm", "engine_idle_rpm", "current_engine_rpm",
    "acceleration_x", "acceleration_y", "acceleration_z",
    "velocity_x", "velocity_y", "velocity_z",
    "angular_velocity_x", "angular_velocity_y", "angular_velocity_z",
    "yaw", "pitch", "roll",
    "normalized_suspension_travel_fl", "normalized_suspension_travel_fr",
    "normalized_suspension_travel_rl", "normalized_suspension_travel_rr",
    "tire_slip_ratio_fl", "tire_slip_ratio_fr",
    "tire_slip_ratio_rl", "tire_slip_ratio_rr",
    "wheel_rotation_speed_fl", "wheel_rotation_speed_fr",
    "wheel_rotation_speed_rl", "wheel_rotation_speed_rr",
    "wheel_on_rumble_strip_fl", "wheel_on_rumble_strip_fr",
    "wheel_on_rumble_strip_rl", "wheel_on_rumble_strip_rr",
    "wheel_in_puddle_fl", "wheel_in_puddle_fr",
    "wheel_in_puddle_rl", "wheel_in_puddle_rr",
    "surface_rumble_fl", "surface_rumble_fr",
    "surface_rumble_rl", "surface_rumble_rr",
    "tire_slip_angle_fl", "tire_slip_angle_fr",
    "tire_slip_angle_rl", "tire_slip_angle_rr",
    "tire_combined_slip_fl", "tire_combined_slip_fr",
    "tire_combined_slip_rl", "tire_combined_slip_rr",
    "suspension_travel_meters_fl", "suspension_travel_meters_fr",
    "suspension_travel_meters_rl", "suspension_travel_meters_rr",
    "car_ordinal", "car_class", "car_performance_index",
    "drivetrain_type", "num_cylinders",
    "position_x", "position_y", "position_z",
    "speed", "power", "torque",
    "tire_temp_fl", "tire_temp_fr", "tire_temp_rl", "tire_temp_rr",
    "boost", "fuel", "distance_traveled",
    "best_lap_time", "last_lap_time", "current_lap_time",
    "current_race_time",
    "lap_number", "race_position",
    "accel", "brake", "clutch", "handbrake",
    "gear", "steer",
    "normalized_driving_lane", "normalized_ai_brake_difference",
]

def parse_forza(data: bytes) -> Optional[dict]:
    if len(data) == FM_PACKET_SIZE_FH:
        fmt = FM_FORMAT_FH
    elif len(data) == FM_PACKET_SIZE:
        fmt = FM_FORMAT
    else:
        return None
    try:
        values = struct.unpack(fmt, data)
        parsed = dict(zip(FM_FIELDS, values))
        if len(data) == FM_PACKET_SIZE_FH:
            parsed["tire_wear_fl"]  = values[len(FM_FIELDS)]
            parsed["tire_wear_fr"]  = values[len(FM_FIELDS) + 1]
            parsed["tire_wear_rl"]  = values[len(FM_FIELDS) + 2]
            parsed["tire_wear_rr"]  = values[len(FM_FIELDS) + 3]
            ord_val                 = values[len(FM_FIELDS) + 4]
            parsed["track_ordinal"] = ord_val
            parsed["track"]         = FORZA_TRACKS.get(ord_val, f"Track #{ord_val}" if ord_val else "unknown")
        else:
            parsed["track"] = "unknown"  # FM2023 doesn't broadcast track in telemetry
        parsed["speed_mph"]      = parsed["speed"] * 2.237
        parsed["throttle_pct"]   = parsed["accel"] / 255 * 100
        parsed["brake_pct"]      = parsed["brake"] / 255 * 100
        parsed["clutch_pct"]     = parsed["clutch"] / 255 * 100
        parsed["slip_ratio_fl"]  = abs(parsed["tire_slip_ratio_fl"])
        parsed["slip_ratio_fr"]  = abs(parsed["tire_slip_ratio_fr"])
        parsed["slip_ratio_rl"]  = abs(parsed["tire_slip_ratio_rl"])
        parsed["slip_ratio_rr"]  = abs(parsed["tire_slip_ratio_rr"])
        parsed["g_lat"]          = parsed["acceleration_x"] / 9.81
        parsed["g_lon"]          = parsed["acceleration_z"] / 9.81
        return parsed
    except struct.error:
        return None

# ─── ACC Parser ───────────────────────────────────────────────────────────────
# ACC UDP plugin physics packet
# Full struct reference: https://www.assettocorsa.net/forum/index.php?threads/acc-udp-remote-telemetry-port.59734/

ACC_PHYSICS_SIZE = 328  # v1.7+ physics packet size

def parse_acc(data: bytes) -> Optional[dict]:
    """Parse ACC physics packet (full field set)."""
    if len(data) < 100:
        return None
    try:
        o = 0
        def ri(fmt):
            nonlocal o
            val = struct.unpack_from(fmt, data, o)
            o += struct.calcsize(fmt)
            return val[0] if len(val) == 1 else val

        packet_id = ri("<i")
        gas       = ri("<f")
        brake     = ri("<f")
        fuel      = ri("<f")
        gear      = ri("<i")
        rpm       = ri("<i")
        steer     = ri("<f")
        speed_kmh = ri("<f")
        vel_x, vel_y, vel_z = ri("<fff")
        acc_x, acc_y, acc_z = ri("<fff")  # G-forces (m/s²)
        slip_fl, slip_fr, slip_rl, slip_rr = ri("<ffff")

        # wheelSlip done — continue with more fields if packet is large enough
        result = {
            "packet_id":    packet_id,
            "throttle_pct": round(gas * 100, 1),
            "brake_pct":    round(brake * 100, 1),
            "fuel":         round(fuel, 2),
            "gear":         gear,
            "rpm":          rpm,
            "steer":        round(steer, 3),
            "speed_mph":    round(speed_kmh * 0.621371, 1),
            "velocity_x":   round(vel_x, 3),
            "velocity_y":   round(vel_y, 3),
            "velocity_z":   round(vel_z, 3),
            "g_lat":        round(acc_x / 9.81, 3),
            "g_lon":        round(acc_z / 9.81, 3),
            "g_vert":       round(acc_y / 9.81, 3),
            "slip_ratio_fl": round(abs(slip_fl), 4),
            "slip_ratio_fr": round(abs(slip_fr), 4),
            "slip_ratio_rl": round(abs(slip_rl), 4),
            "slip_ratio_rr": round(abs(slip_rr), 4),
        }

        # Extended fields: wheelsPressure(4f), brakeTemp(4f), tyreCoreTemp(4f)
        if len(data) >= o + 48:
            slip_angle_fl, slip_angle_fr, slip_angle_rl, slip_angle_rr = ri("<ffff")
            slip_speed_fl, slip_speed_fr, slip_speed_rl, slip_speed_rr = ri("<ffff")
            slip_speed2_fl, slip_speed2_fr, slip_speed2_rl, slip_speed2_rr = ri("<ffff")

        if len(data) >= o + 32:
            p_fl, p_fr, p_rl, p_rr = ri("<ffff")
            b_fl, b_fr, b_rl, b_rr = ri("<ffff")
            result["tyre_pressure_fl"] = round(p_fl, 2)
            result["tyre_pressure_fr"] = round(p_fr, 2)
            result["tyre_pressure_rl"] = round(p_rl, 2)
            result["tyre_pressure_rr"] = round(p_rr, 2)
            result["brake_temp_fl"]    = round(b_fl, 1)
            result["brake_temp_fr"]    = round(b_fr, 1)
            result["brake_temp_rl"]    = round(b_rl, 1)
            result["brake_temp_rr"]    = round(b_rr, 1)

        if len(data) >= o + 16:
            t_fl, t_fr, t_rl, t_rr = ri("<ffff")
            result["tyre_core_temp_fl"] = round(t_fl, 1)
            result["tyre_core_temp_fr"] = round(t_fr, 1)
            result["tyre_core_temp_rl"] = round(t_rl, 1)
            result["tyre_core_temp_rr"] = round(t_rr, 1)

        return result
    except struct.error:
        return None

# ─── F1 Parser ────────────────────────────────────────────────────────────────
# Codemasters F1 UDP format (F1 2023/2024)
# Header: packetFormat(H), gameYear(B), gameMajorVersion(B), gameMinorVersion(B),
#         packetVersion(B), packetId(B), sessionUID(Q), sessionTime(f),
#         frameIdentifier(I), playerCarIndex(B), secondaryPlayerCarIndex(B)
# Packet IDs: 0=Motion, 1=Session, 6=CarTelemetry, 7=CarStatus

F1_HEADER_SIZE = 29  # F1 2023/2024 header

# Track names by F1 track ID (F1 2023)
F1_TRACKS = {
    0: "Melbourne", 1: "Paul Ricard", 2: "Shanghai", 3: "Sakhir (Bahrain)",
    4: "Catalunya", 5: "Monaco", 6: "Montreal", 7: "Silverstone",
    8: "Hockenheim", 9: "Hungaroring", 10: "Spa", 11: "Monza",
    12: "Singapore", 13: "Suzuka", 14: "Abu Dhabi", 15: "Texas",
    16: "Brazil", 17: "Austria", 18: "Sochi", 19: "Mexico",
    20: "Baku (Azerbaijan)", 21: "Sakhir Short", 22: "Silverstone Short",
    23: "Texas Short", 24: "Suzuka Short", 25: "Hanoi", 26: "Zandvoort",
    27: "Imola", 28: "Portimão", 29: "Jeddah", 30: "Miami",
    31: "Las Vegas", 32: "Losail",
}

# Per-session F1 state (track, session type) populated from packet ID 1
_f1_session_meta: dict = {}

def parse_f1(data: bytes) -> Optional[dict]:
    """Dispatch F1 packets by ID; return unified dict for telemetry packets."""
    if len(data) < F1_HEADER_SIZE:
        return None
    try:
        packet_id  = struct.unpack_from("<B", data, 5)[0]
        session_uid = struct.unpack_from("<Q", data, 7)[0]

        if packet_id == 1:
            # Session packet — extract track name and session type
            # weather(B), trackTemp(b), airTemp(b), totalLaps(B), trackLength(H),
            # sessionType(B), trackId(b), formula(B) ...
            base = F1_HEADER_SIZE
            if len(data) >= base + 8:
                track_id     = struct.unpack_from("<b", data, base + 5)[0]
                session_type = struct.unpack_from("<B", data, base + 4)[0]
                session_types = {
                    0: "Unknown", 1: "P1", 2: "P2", 3: "P3", 4: "Short P",
                    5: "Q1", 6: "Q2", 7: "Q3", 8: "Short Q", 9: "OSQ",
                    10: "R", 11: "R2", 12: "R3", 13: "Time Trial",
                }
                _f1_session_meta[session_uid] = {
                    "track":        F1_TRACKS.get(track_id, f"track_{track_id}"),
                    "session_type": session_types.get(session_type, "Unknown"),
                }
            return None  # Session packets don't produce a telemetry sample

        if packet_id == 0:
            # Motion packet — position and velocity for player car
            player_idx = struct.unpack_from("<B", data, F1_HEADER_SIZE - 2)[0]
            # Each car motion: worldPosX(f), worldPosY(f), worldPosZ(f),
            #   worldVelX(f), worldVelY(f), worldVelZ(f),
            #   worldForwardDirX(H), worldForwardDirY(H), worldForwardDirZ(H),
            #   worldRightDirX(H), worldRightDirY(H), worldRightDirZ(H),
            #   gForceLateral(f), gForceLongitudinal(f), gForceVertical(f),
            #   yaw(f), pitch(f), roll(f)
            car_size = 60
            base = F1_HEADER_SIZE + player_idx * car_size
            if len(data) < base + car_size:
                return None
            pos_x, pos_y, pos_z = struct.unpack_from("<fff", data, base)
            vel_x, vel_y, vel_z = struct.unpack_from("<fff", data, base + 12)
            # Skip direction vectors (6 shorts = 12 bytes)
            g_lat  = struct.unpack_from("<f", data, base + 36)[0]
            g_lon  = struct.unpack_from("<f", data, base + 40)[0]
            g_vert = struct.unpack_from("<f", data, base + 44)[0]
            return {
                "_packet_type": "motion",
                "_session_uid": session_uid,
                "position_x": round(pos_x, 2),
                "position_y": round(pos_y, 2),
                "position_z": round(pos_z, 2),
                "velocity_x": round(vel_x, 2),
                "velocity_y": round(vel_y, 2),
                "velocity_z": round(vel_z, 2),
                "g_lat":  round(g_lat, 3),
                "g_lon":  round(g_lon, 3),
                "g_vert": round(g_vert, 3),
            }

        if packet_id == 6:
            # Car telemetry packet
            player_idx = struct.unpack_from("<B", data, F1_HEADER_SIZE - 2)[0]
            # Each car: speed(H), throttle(f), steer(f), brake(f), clutch(B),
            #   gear(b), engineRPM(H), drs(B), revLightsPercent(B),
            #   revLightsBitValue(H), brakesTemp(4H), tyresSurfaceTemp(4B),
            #   tyresInnerTemp(4B), engineTemp(H), tyresPressure(4f), surfaceType(4B)
            car_size = 60
            base = F1_HEADER_SIZE + player_idx * car_size
            if len(data) < base + car_size:
                return None

            speed    = struct.unpack_from("<H", data, base)[0]
            throttle = struct.unpack_from("<f", data, base + 2)[0]
            steer    = struct.unpack_from("<f", data, base + 6)[0]
            brake    = struct.unpack_from("<f", data, base + 10)[0]
            clutch   = struct.unpack_from("<B", data, base + 14)[0]
            gear     = struct.unpack_from("<b", data, base + 15)[0]
            rpm      = struct.unpack_from("<H", data, base + 16)[0]
            drs      = struct.unpack_from("<B", data, base + 18)[0]

            # Brake temps: 4 × uint16 at base+21
            bt_rl, bt_rr, bt_fl, bt_fr = struct.unpack_from("<HHHH", data, base + 21)
            # Tyre surface temps: 4 × uint8 at base+29
            ts_rl, ts_rr, ts_fl, ts_fr = struct.unpack_from("<BBBB", data, base + 29)
            # Tyre inner temps: 4 × uint8 at base+33
            ti_rl, ti_rr, ti_fl, ti_fr = struct.unpack_from("<BBBB", data, base + 33)
            engine_temp = struct.unpack_from("<H", data, base + 37)[0]
            # Tyre pressure: 4 × float at base+39
            tp_rl, tp_rr, tp_fl, tp_fr = struct.unpack_from("<ffff", data, base + 39)

            meta = _f1_session_meta.get(session_uid, {})
            return {
                "_packet_type":   "telemetry",
                "_session_uid":   session_uid,
                "track":          meta.get("track", "unknown"),
                "session_type":   meta.get("session_type", "unknown"),
                "speed_mph":      round(speed * 0.621371, 1),
                "throttle_pct":   round(throttle * 100, 1),
                "brake_pct":      round(brake * 100, 1),
                "clutch_pct":     round(clutch / 255 * 100, 1),
                "steer":          round(steer, 3),
                "gear":           gear,
                "rpm":            rpm,
                "drs":            bool(drs),
                "brake_temp_fl":  bt_fl,
                "brake_temp_fr":  bt_fr,
                "brake_temp_rl":  bt_rl,
                "brake_temp_rr":  bt_rr,
                "tyre_surface_temp_fl": ts_fl,
                "tyre_surface_temp_fr": ts_fr,
                "tyre_surface_temp_rl": ts_rl,
                "tyre_surface_temp_rr": ts_rr,
                "tyre_inner_temp_fl": ti_fl,
                "tyre_inner_temp_fr": ti_fr,
                "tyre_inner_temp_rl": ti_rl,
                "tyre_inner_temp_rr": ti_rr,
                "tyre_pressure_fl": round(tp_fl, 2),
                "tyre_pressure_fr": round(tp_fr, 2),
                "tyre_pressure_rl": round(tp_rl, 2),
                "tyre_pressure_rr": round(tp_rr, 2),
                "engine_temp":    engine_temp,
            }

        if packet_id == 7:
            # Car status — fuel, ERS, tyre compound
            player_idx = struct.unpack_from("<B", data, F1_HEADER_SIZE - 2)[0]
            # Each car status: tractionControl(B), antiLockBrakes(B), fuelMix(B),
            #   frontBrakeBias(B), pitLimiterStatus(B), fuelInTank(f),
            #   fuelCapacity(f), fuelRemainingLaps(f), maxRPM(H), idleRPM(H),
            #   maxGears(B), drsAllowed(B), drsActivationDistance(H),
            #   actualTyreCompound(B), visualTyreCompound(B), tyresAgeLaps(B), ...
            car_size = 47
            base = F1_HEADER_SIZE + player_idx * car_size
            if len(data) < base + car_size:
                return None
            fuel_in_tank       = struct.unpack_from("<f", data, base + 5)[0]
            fuel_remaining_laps = struct.unpack_from("<f", data, base + 13)[0]
            tyre_compound      = struct.unpack_from("<B", data, base + 23)[0]
            tyre_age_laps      = struct.unpack_from("<B", data, base + 25)[0]
            compounds = {16: "C5", 17: "C4", 18: "C3", 19: "C2", 20: "C1",
                         21: "C0", 7: "Inter", 8: "Wet", 9: "Wet"}
            return {
                "_packet_type":       "car_status",
                "_session_uid":       session_uid,
                "fuel_in_tank":       round(fuel_in_tank, 2),
                "fuel_remaining_laps": round(fuel_remaining_laps, 1),
                "tyre_compound":      compounds.get(tyre_compound, f"compound_{tyre_compound}"),
                "tyre_age_laps":      tyre_age_laps,
            }

        return None
    except struct.error:
        return None

# ─── Session Manager ──────────────────────────────────────────────────────────

def _is_driving(parsed: dict) -> bool:
    """True when the player has meaningful input — not parked or in a menu."""
    return (
        parsed.get("speed_mph", 0) > 2 or
        parsed.get("throttle_pct", 0) > 2 or
        parsed.get("brake_pct", 0) > 2 or
        abs(parsed.get("steer", 0)) > 5
    )


class LapRecord:
    def __init__(self, lap_number: int):
        self.lap_number  = lap_number
        self.started_at  = time.time()
        self.ended_at    = None
        self.lap_time_s  = None
        self.samples     = []
        self.max_speed   = 0.0
        self.sector_times = []  # populated for Forza/F1 where available

    def add_sample(self, parsed: dict):
        speed = parsed.get("speed_mph", 0)
        if speed > self.max_speed:
            self.max_speed = speed
        self.samples.append({
            "t":            round(parsed.get("current_lap_time", parsed.get("_t", 0)), 3),
            "speed_mph":    round(parsed.get("speed_mph", 0), 1),
            "throttle_pct": round(parsed.get("throttle_pct", 0), 1),
            "brake_pct":    round(parsed.get("brake_pct", 0), 1),
            "clutch_pct":   round(parsed.get("clutch_pct", 0), 1),
            "gear":         parsed.get("gear", 0),
            "steer":        round(parsed.get("steer", 0), 3),
            "rpm":          parsed.get("rpm", parsed.get("current_engine_rpm", 0)),
            "slip_rl":      round(parsed.get("slip_ratio_rl", 0), 4),
            "slip_rr":      round(parsed.get("slip_ratio_rr", 0), 4),
            "g_lat":        round(parsed.get("g_lat", 0), 3),
            "g_lon":        round(parsed.get("g_lon", 0), 3),
        })

    def close(self, lap_time_s: Optional[float] = None):
        self.ended_at   = time.time()
        self.lap_time_s = lap_time_s or (self.ended_at - self.started_at)

    def to_dict(self) -> dict:
        return {
            "lap_number":   self.lap_number,
            "lap_time_s":   round(self.lap_time_s, 3) if self.lap_time_s else None,
            "max_speed_mph": round(self.max_speed, 1),
            "sample_count": len(self.samples),
            "samples":      self.samples,
        }


class Session:
    def __init__(self, game: str, started_at: datetime):
        self.game         = game
        self.started_at   = started_at
        self.session_id   = started_at.strftime("%Y-%m-%dT%H-%M-%S") + f"_{game}"
        self.last_packet  = time.time()
        self.packet_count = 0
        self.track        = "unknown"
        self.car          = "unknown"
        self.session_type = "unknown"

        # Lap tracking
        self.current_lap_num = 0
        self.current_lap: Optional[LapRecord] = None
        self.completed_laps: list[LapRecord] = []
        self.best_lap_time_s: Optional[float] = None

        # Motion cache (F1 motion packets arrive separately from telemetry)
        self._motion_cache: dict = {}

        self.race_type = None  # set post-session via /sessions/update

        self.last_activity = time.time()  # updated only when driver input is detected

        raw_path = storage_path() / "raw" / f"{self.session_id}.bin"
        try:
            self.raw_file = open(raw_path, "wb")
        except OSError as e:
            log.error(f"Cannot open raw archive {raw_path}: {e} — raw recording disabled")
            self.raw_file = None
        log.info(f"Session started: {self.session_id} (storage: {storage_path()})")

    def ingest(self, raw: bytes, parsed: dict):
        if self.raw_file:
            try:
                self.raw_file.write(struct.pack("<I", len(raw)) + raw)
            except OSError as e:
                log.error(f"Raw write failed: {e} — closing raw archive")
                self.raw_file = None
        self.last_packet  = time.time()
        self.packet_count += 1

        if self._is_driving(parsed):
            self.last_activity = self.last_packet

        packet_type = parsed.get("_packet_type")

        # Merge F1 motion data into next telemetry sample
        if packet_type == "motion":
            self._motion_cache.update({
                k: v for k, v in parsed.items() if not k.startswith("_")
            })
            return

        # Merge cached motion into telemetry
        if packet_type == "telemetry" and self._motion_cache:
            parsed = {**parsed, **self._motion_cache}
            self._motion_cache = {}

        # Update track/car metadata
        if parsed.get("track", "unknown") != "unknown":
            self.track = parsed["track"]
        if parsed.get("session_type", "unknown") != "unknown":
            self.session_type = parsed["session_type"]
        if "car_ordinal" in parsed and self.car == "unknown":
            self.car = str(parsed["car_ordinal"])

        # Forza: lap transitions via lap_number field
        lap_num = parsed.get("lap_number", 0)
        if lap_num and lap_num != self.current_lap_num:
            self._transition_lap(
                new_lap=lap_num,
                lap_time_s=parsed.get("last_lap_time"),
            )

        if self.current_lap is None:
            self.current_lap = LapRecord(self.current_lap_num)

        parsed["_t"] = time.time() - self.current_lap.started_at
        self.current_lap.add_sample(parsed)

        # Update car status fields (F1 car_status packets)
        if packet_type == "car_status":
            pass  # already merged into session via update_state

    def _transition_lap(self, new_lap: int, lap_time_s: Optional[float] = None):
        if self.current_lap is not None:
            self.current_lap.close(lap_time_s)
            if lap_time_s:
                if self.best_lap_time_s is None or lap_time_s < self.best_lap_time_s:
                    self.best_lap_time_s = lap_time_s
            self.completed_laps.append(self.current_lap)
            log.info(
                f"[{self.game}] Lap {self.current_lap.lap_number} complete | "
                f"time={lap_time_s:.3f}s | samples={len(self.current_lap.samples)}"
                if lap_time_s else
                f"[{self.game}] Lap {self.current_lap.lap_number} complete | "
                f"samples={len(self.current_lap.samples)}"
            )
        self.current_lap_num = new_lap
        self.current_lap = LapRecord(new_lap)

    def _is_driving(self, parsed: dict) -> bool:
        return _is_driving(parsed)

    def is_timed_out(self) -> bool:
        return time.time() - self.last_packet > SESSION_TIMEOUT_S

    def is_idle_timed_out(self) -> bool:
        return time.time() - self.last_activity > IDLE_TIMEOUT_S

    def close(self) -> dict:
        if self.raw_file:
            try:
                self.raw_file.close()
            except OSError:
                pass

        # Close current lap
        if self.current_lap and self.current_lap.samples:
            self.current_lap.close()
            self.completed_laps.append(self.current_lap)

        laps_summary = [
            {
                "lap_number":    lap.lap_number,
                "lap_time_s":    lap.lap_time_s,
                "max_speed_mph": lap.max_speed,
                "sample_count":  len(lap.samples),
            }
            for lap in self.completed_laps
        ]

        session_data = {
            "session_id":       self.session_id,
            "game":             self.game,
            "track":            self.track,
            "car":              self.car,
            "session_type":     self.session_type,
            "race_type":        self.race_type,
            "started_at":       self.started_at.isoformat(),
            "ended_at":         datetime.now().isoformat(),
            "packet_count":     self.packet_count,
            "best_lap_time_s":  round(self.best_lap_time_s, 3) if self.best_lap_time_s else None,
            "laps":             laps_summary,
        }

        _db_write_session(session_data)

        try:
            sp = storage_path()
            out_path = sp / "sessions" / f"{self.session_id}.json"
            with open(out_path, "w") as f:
                json.dump(session_data, f, indent=2)

            samples_path = sp / "sessions" / f"{self.session_id}_laps.json"
            with open(samples_path, "w") as f:
                json.dump([lap.to_dict() for lap in self.completed_laps], f, indent=2)
        except OSError as e:
            log.error(f"Failed to write session files: {e}")

        log.info(
            f"Session closed: {self.session_id} | "
            f"{self.packet_count} packets | {len(self.completed_laps)} laps | "
            f"best={self.best_lap_time_s:.3f}s" if self.best_lap_time_s
            else f"Session closed: {self.session_id} | {self.packet_count} packets"
        )
        return session_data

# ─── Shared State ─────────────────────────────────────────────────────────────

state = {
    "status":           "idle",
    "game":             None,
    "session_id":       None,
    "track":            None,
    "car":              None,
    "session_type":     None,
    "started_at":       None,
    "packet_count":     0,
    "lap":              0,
    "best_lap_time_s":  None,
    "speed_mph":        0,
    "throttle_pct":     0,
    "brake_pct":        0,
    "gear":             0,
    "rpm":              0,
    "engine_max_rpm":   0,
    "steer":            0,
    "slip_rl":          0,
    "slip_rr":          0,
    "g_lat":            0,
    "g_lon":            0,
    "drs":              False,
    "tyre_compound":    None,
    "fuel_remaining_laps": None,
    "current_lap_time": None,
    "last_lap_time_s":  None,
    "tyre_fl":          None,
    "tyre_fr":          None,
    "tyre_rl":          None,
    "tyre_rr":          None,
    "last_packet_at":   None,
    # per-game raw UDP counters (arrive regardless of whether parse succeeds)
    "udp_received": {"forza_motorsport": 0, "acc": 0, "f1": 0},
    "udp_rejected": {"forza_motorsport": 0, "acc": 0, "f1": 0},
    "last_rejected_size": {"forza_motorsport": None, "acc": None, "f1": None},
}

active_sessions: dict[str, Session] = {}

def update_state(game: str, session: Session, parsed: dict):
    if parsed.get("_packet_type") in ("motion", None) and "_packet_type" in parsed:
        return  # don't overwrite telemetry state with partial motion data
    state["status"]       = "receiving" if session._is_driving(parsed) else "idle"
    state["game"]         = game
    state["session_id"]   = session.session_id
    state["track"]        = session.track
    state["car"]          = session.car
    state["session_type"] = session.session_type
    state["started_at"]   = session.started_at.isoformat()
    state["packet_count"] = session.packet_count
    state["lap"]          = session.current_lap_num
    state["best_lap_time_s"] = session.best_lap_time_s
    state["speed_mph"]    = parsed.get("speed_mph", state["speed_mph"])
    state["throttle_pct"] = parsed.get("throttle_pct", state["throttle_pct"])
    state["brake_pct"]    = parsed.get("brake_pct", state["brake_pct"])
    state["gear"]         = parsed.get("gear", state["gear"])
    state["rpm"]            = parsed.get("rpm", parsed.get("current_engine_rpm", state["rpm"]))
    if parsed.get("engine_max_rpm", 0) > 2000:
        state["engine_max_rpm"] = parsed["engine_max_rpm"]
    state["steer"]        = round(parsed.get("steer", state["steer"]), 3)
    state["slip_rl"]      = round(parsed.get("slip_ratio_rl", state["slip_rl"]), 4)
    state["slip_rr"]      = round(parsed.get("slip_ratio_rr", state["slip_rr"]), 4)
    state["g_lat"]        = round(parsed.get("g_lat", state["g_lat"]), 3)
    state["g_lon"]        = round(parsed.get("g_lon", state["g_lon"]), 3)
    state["drs"]          = parsed.get("drs", state["drs"])
    state["tyre_compound"]       = parsed.get("tyre_compound", state["tyre_compound"])
    state["fuel_remaining_laps"] = parsed.get("fuel_remaining_laps", state["fuel_remaining_laps"])
    state["current_lap_time"]    = parsed.get("current_lap_time", state["current_lap_time"])
    if parsed.get("last_lap_time"):
        state["last_lap_time_s"] = parsed["last_lap_time"]
    for corner in ("fl", "fr", "rl", "rr"):
        v = parsed.get(f"tire_temp_{corner}")
        if v is not None:
            state[f"tyre_{corner}"] = round(v, 1)
    state["last_packet_at"]      = datetime.now().isoformat()

# ─── UDP Protocol Handlers ────────────────────────────────────────────────────

class TelemetryProtocol(asyncio.DatagramProtocol):
    def __init__(self, game: str, parser):
        self.game         = game
        self.parser       = parser
        self._logged_size = False  # log unexpected packet size once per run

    def datagram_received(self, data: bytes, addr):
        state["udp_received"][self.game] = state["udp_received"].get(self.game, 0) + 1

        parsed = self.parser(data)
        if not parsed:
            count = state["udp_rejected"].get(self.game, 0) + 1
            state["udp_rejected"][self.game] = count
            state["last_rejected_size"][self.game] = len(data)
            ts = datetime.now().strftime("%H:%M:%S")
            _debug_push(f"{ts} [REJECTED] {self.game} {len(data)}B from {addr[0]}")
            # Log on first rejection and every 100th after
            if count == 1 or count % 100 == 0:
                log.warning(
                    f"[{self.game}] packet #{count} from {addr[0]} rejected — "
                    f"size={len(data)} bytes. "
                    f"Forza expects {FM_PACKET_SIZE} (FM2023) or {FM_PACKET_SIZE_FH} (FH4/FH5), "
                    f"ACC expects >={100}, F1 expects >={F1_HEADER_SIZE}. "
                    f"Check Data Out settings."
                )
            return

        ts = datetime.now().strftime("%H:%M:%S")
        speed = parsed.get("speed_mph", 0)
        gear  = parsed.get("gear", parsed.get("current_engine_rpm", "?"))
        rpm   = parsed.get("rpm", parsed.get("current_engine_rpm", 0))
        _debug_push(f"{ts} [UDP OK]  {self.game} {len(data)}B  {speed:.0f}mph  rpm={rpm:.0f}  gear={gear}")

        driving = _is_driving(parsed)

        if self.game not in active_sessions:
            if not driving:
                return  # don't create a session from an idle broadcast
            session = Session(self.game, datetime.now())
            active_sessions[self.game] = session

        session = active_sessions[self.game]

        if not driving:
            session.last_packet = time.time()  # keep alive timer running
            update_state(self.game, session, parsed)
            return  # don't record idle packets

        session.ingest(data, parsed)
        update_state(self.game, session, parsed)

    def error_received(self, exc):
        log.error(f"[{self.game}] UDP error: {exc}")

    def connection_lost(self, exc):
        log.warning(f"[{self.game}] Connection lost: {exc}")

# ─── Session Watchdog ─────────────────────────────────────────────────────────

async def _clear_race_ended():
    await asyncio.sleep(30)
    if state["status"] == "race_ended":
        state["status"] = "idle"


async def session_watchdog():
    while True:
        await asyncio.sleep(2)
        to_close = []
        for game, session in active_sessions.items():
            if session.is_timed_out():
                to_close.append((game, "no packets"))
            elif session.is_idle_timed_out():
                to_close.append((game, "idle"))
        for game, reason in to_close:
            session = active_sessions.pop(game)
            log.info(f"[{game}] Closing session — {reason} for >{IDLE_TIMEOUT_S if reason == 'idle' else SESSION_TIMEOUT_S}s")
            session.close()
            if not active_sessions:
                state["status"] = "race_ended"
                state["game"]   = None
                log.info("All sessions closed. Listening...")
                asyncio.create_task(_clear_race_ended())

# ─── Admin Packet Injection ───────────────────────────────────────────────────

def _build_inject_packets(game: str, p: dict) -> list:
    """Build valid UDP telemetry packets from user-friendly params."""
    speed_mph = float(p.get("speed_mph", 0))
    throttle  = max(0.0, min(100.0, float(p.get("throttle_pct", 0))))
    brake     = max(0.0, min(100.0, float(p.get("brake_pct", 0))))
    rpm       = float(p.get("rpm", 1000))
    gear      = int(p.get("gear", 1))
    lap       = int(p.get("lap", 1))

    if game == "forza_motorsport":
        speed_ms = speed_mph / 2.237
        vals = [
            1, 0,                                                  # is_race_on, timestamp_ms
            8500.0, 800.0, rpm,                                    # engine max/idle/current rpm
            0.0, 0.0, 0.0,                                         # accel xyz
            speed_ms, 0.0, 0.0,                                    # velocity xyz
            0.0, 0.0, 0.0,                                         # angular velocity
            0.0, 0.0, 0.0,                                         # yaw pitch roll
            0.5, 0.5, 0.5, 0.5,                                    # norm suspension travel x4
            0.0, 0.0, 0.0, 0.0,                                    # tire slip ratio x4
            speed_ms*4, speed_ms*4, speed_ms*4, speed_ms*4,        # wheel rotation speed x4
            0.0, 0.0, 0.0, 0.0,                                    # rumble strip x4
            0.0, 0.0, 0.0, 0.0,                                    # puddle x4
            0.0, 0.0, 0.0, 0.0,                                    # surface rumble x4
            0.0, 0.0, 0.0, 0.0,                                    # slip angle x4
            0.0, 0.0, 0.0, 0.0,                                    # combined slip x4
            0.1, 0.1, 0.1, 0.1,                                    # suspension travel meters x4
            42, 3, 750, 1, 6,                                      # car_ordinal/class/pi/drivetrain/cylinders
            0.0, 0.0, 0.0,                                         # position xyz
            speed_ms, 250000.0, 400.0,                             # speed, power, torque
            85.0, 85.0, 85.0, 85.0,                                # tire temp x4
            0.5, 0.6, 0.0,                                         # boost, fuel, distance
            0.0, 0.0, 0.0, 0.0,                                    # best/last/current lap / race time
            lap, 1,                                                # lap_number (H), race_position (B)
            int(throttle/100*255), int(brake/100*255), 0, 0, gear, # accel brake clutch handbrake gear
            0, 0, 0,                                               # steer, norm_driving_lane, norm_ai_brake
        ]
        return [struct.pack(FM_FORMAT, *vals)]

    if game == "acc":
        speed_kmh = speed_mph * 1.60934
        vals = [
            0,                            # packet_id
            throttle / 100,               # gas
            brake / 100,                  # brake
            50.0,                         # fuel
            gear,                         # gear
            int(rpm),                     # rpm
            0.0,                          # steer
            speed_kmh,                    # speed_kmh
            0.0, 0.0, speed_kmh / 3.6,   # vel xyz
            0.0, 0.0, 0.0,               # acc xyz
            0.0, 0.0, 0.0, 0.0,          # wheelSlip x4
        ]
        return [struct.pack("<ifffiiffffffffffff", *vals).ljust(200, b'\x00')]

    if game == "f1":
        speed_kmh = int(speed_mph * 1.60934)
        uid = 0xDEADCAFE
        def hdr(pid):
            return struct.pack("<HBBBBBQfIIBB", 2024, 24, 1, 0, pid, 0, uid, 0.0, 0, 0, 0, 255)
        sess = hdr(1) + struct.pack("<BbbBHBb", 0, 25, 20, 50, 5793, 10, 11)
        car  = struct.pack(
            "<HfffBbHBBH4H4B4BH4f4B",
            speed_kmh, throttle/100, 0.0, brake/100, 0, gear, int(rpm), 0, 0, 0,
            0, 0, 0, 0, 85, 85, 85, 85, 90, 90, 90, 90, 105,
            23.5, 23.5, 22.8, 22.8, 0, 0, 0, 0,
        ).ljust(60, b'\x00')
        return [sess, hdr(6) + car]

    return []

# ─── Local Status Server ──────────────────────────────────────────────────────

_PAGE_STYLE = """
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d0d0f; color: #e0e0e0; font-family: 'Courier New', monospace; padding: 16px; max-width: 860px; }
  a { color: #aaa; text-decoration: none; }
  a:hover { color: #fff; }
  .topbar { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 16px; }
  .topbar h1 { font-size: 1.3rem; color: #e0e0e0; letter-spacing: 3px; text-transform: uppercase; }
  .topbar nav { font-size: 0.9rem; color: #888; }
  .topbar nav a { margin-left: 16px; color: #888; }
  .topbar nav a.active { color: #fff; border-bottom: 1px solid #fff; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: #1a1a1f; border: 1px solid #2a2a3a; border-radius: 6px; padding: 12px 16px; }
  .card .label { font-size: 0.78rem; color: #999; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px; }
  .card .value { font-size: 1.8rem; font-weight: bold; color: #fff; }
  .card .unit  { font-size: 0.85rem; color: #888; margin-left: 4px; }
  .bar-row { margin-bottom: 12px; }
  .bar-label { display: flex; justify-content: space-between; font-size: 0.7rem; color: #888; margin-bottom: 3px; }
  .bar-bg { background: #1a1a1f; border-radius: 3px; height: 12px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 3px; transition: width 0.1s; }
  .bar-throttle .bar-fill { background: #22c55e; }
  .bar-brake    .bar-fill { background: #ef4444; }
  .bar-clutch   .bar-fill { background: #f59e0b; }
  .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .status-dot.receiving { background: #22c55e; animation: pulse 1s infinite; }
  .status-dot.idle      { background: #555; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
  .meta { font-size: 0.9rem; color: #888; margin-bottom: 20px; }
  .meta span { color: #bbb; margin-right: 16px; }
  .slip-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 20px; }
  .slip-box { background: #1a1a1f; border: 1px solid #2a2a3a; border-radius: 4px; padding: 8px; text-align: center; }
  .slip-box .pos { font-size: 0.6rem; color: #555; }
  .slip-box .val { font-size: 1.1rem; color: #e0e0e0; }
  #drs-badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 0.7rem; font-weight: bold; }
  #drs-badge.on  { background: #22c55e22; color: #22c55e; border: 1px solid #22c55e55; }
  #drs-badge.off { background: #11111a; color: #333; border: 1px solid #222; }
  /* setup page */
  .section { margin-bottom: 28px; }
  .section-title { font-size: 0.65rem; color: #555; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 12px; border-bottom: 1px solid #1e1e28; padding-bottom: 6px; }
  .field { margin-bottom: 14px; }
  .field label { display: block; font-size: 0.7rem; color: #888; margin-bottom: 5px; }
  .field input { width: 100%; background: #1a1a1f; border: 1px solid #2a2a3a; color: #e0e0e0;
    font-family: inherit; font-size: 0.85rem; padding: 8px 10px; border-radius: 4px; outline: none; }
  .field input:focus { border-color: #4a4a6a; }
  .field .hint { font-size: 0.65rem; color: #444; margin-top: 4px; }
  .disk-bar-bg { background: #1a1a1f; border-radius: 3px; height: 8px; overflow: hidden; margin-top: 6px; }
  .disk-bar-fill { height: 100%; border-radius: 3px; background: #4a6aef; transition: width 0.3s; }
  .btn { background: #22c55e; color: #000; border: none; font-family: inherit; font-size: 0.8rem;
    font-weight: bold; padding: 9px 22px; border-radius: 4px; cursor: pointer; letter-spacing: 1px; }
  .btn:hover { background: #16a34a; }
  .btn:disabled { background: #2a2a3a; color: #555; cursor: default; }
  .toast { display: none; margin-top: 14px; padding: 10px 14px; border-radius: 4px; font-size: 0.8rem; }
  .toast.ok  { background: #22c55e22; color: #22c55e; border: 1px solid #22c55e44; display: block; }
  .toast.err { background: #ef444422; color: #ef4444; border: 1px solid #ef444444; display: block; }
  .ports-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }
  .disk-info { font-size: 0.7rem; color: #555; margin-top: 8px; }
  .disk-info span { color: #888; }
  /* file browser */
  .path-row { display: flex; gap: 8px; }
  .path-row input { flex: 1; min-width: 0; }
  .btn-browse { background: #1a1a1f; border: 1px solid #2a2a3a; color: #777; font-family: inherit;
    font-size: 0.75rem; padding: 8px 12px; border-radius: 4px; cursor: pointer; white-space: nowrap; }
  .btn-browse:hover { border-color: #4a4a6a; color: #e0e0e0; }
  .path-status { font-size: 0.7rem; margin-top: 5px; min-height: 1.2em; transition: color 0.2s; }
  .browse-panel { background: #0a0a0e; border: 1px solid #1e1e2e; border-radius: 4px; margin-top: 8px; }
  .browse-toolbar { display: flex; align-items: center; justify-content: space-between;
    padding: 7px 10px; border-bottom: 1px solid #1a1a28; gap: 8px; }
  .breadcrumb { font-size: 0.7rem; flex: 1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .crumb { color: #555; cursor: pointer; } .crumb:hover { color: #aaa; }
  .crumb-sep { color: #2a2a3a; } .crumb-cur { color: #888; }
  .btn-use { background: #13132a; border: 1px solid #2a2a5a; color: #5a5acc; font-family: inherit;
    font-size: 0.7rem; padding: 5px 10px; border-radius: 3px; cursor: pointer; white-space: nowrap; }
  .btn-use:hover { background: #1e1e48; color: #9999ee; }
  .dir-list { max-height: 200px; overflow-y: auto; }
  .dir-item { display: flex; align-items: center; gap: 8px; padding: 7px 12px;
    font-size: 0.75rem; color: #666; cursor: pointer; user-select: none; }
  .dir-item:hover { background: #0f0f1e; color: #ccc; }
  .dir-empty { padding: 14px 12px; font-size: 0.7rem; color: #333; text-align: center; }
  /* admin page */
  .tabs { display: flex; gap: 8px; margin-bottom: 24px; }
  .tab { background: #1a1a1f; border: 1px solid #2a2a3a; color: #555; font-family: inherit;
    font-size: 0.75rem; padding: 7px 18px; border-radius: 3px; cursor: pointer; letter-spacing: 1px; }
  .tab.active { border-color: #22c55e44; color: #22c55e; background: #22c55e11; }
  .tab:hover:not(.active) { color: #aaa; border-color: #444; }
  .ctrl-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px 24px; margin-bottom: 20px; }
  .ctrl label { display: flex; justify-content: space-between; font-size: 0.7rem; color: #666; margin-bottom: 6px; }
  .ctrl label .val { color: #e0e0e0; font-weight: bold; }
  .ctrl input[type=range] { width: 100%; accent-color: #22c55e; cursor: pointer; height: 4px; }
  .gear-row { display: flex; gap: 6px; flex-wrap: wrap; }
  .gear-btn { background: #1a1a1f; border: 1px solid #2a2a3a; color: #555; font-family: inherit;
    font-size: 0.75rem; padding: 6px 0; border-radius: 3px; cursor: pointer; width: 38px; text-align: center; }
  .gear-btn.active { background: #22c55e11; border-color: #22c55e44; color: #22c55e; }
  .gear-btn:hover:not(.active) { border-color: #444; color: #aaa; }
  .preset-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 20px; }
  .preset-btn { background: #1a1a1f; border: 1px solid #2a2a3a; color: #555; font-family: inherit;
    font-size: 0.7rem; padding: 6px 14px; border-radius: 3px; cursor: pointer; }
  .preset-btn:hover { border-color: #555; color: #ccc; }
  .action-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .btn-inject { background: #22c55e; color: #000; border: none; font-family: inherit;
    font-weight: bold; font-size: 0.8rem; padding: 9px 20px; border-radius: 4px; cursor: pointer; }
  .btn-inject:hover { background: #16a34a; }
  .btn-stream { background: #1a1a1f; border: 1px solid #2a2a3a; color: #aaa; font-family: inherit;
    font-size: 0.8rem; padding: 9px 20px; border-radius: 4px; cursor: pointer; }
  .btn-stream.on { background: #ef444411; border-color: #ef444444; color: #ef4444; }
  .hz-sel { background: #1a1a1f; border: 1px solid #2a2a3a; color: #777; font-family: inherit;
    font-size: 0.75rem; padding: 8px 10px; border-radius: 4px; }
  .sent-lbl { font-size: 0.75rem; color: #333; margin-left: 4px; }
  .sent-lbl span { color: #666; }
  .admin-divider { border: none; border-top: 1px solid #1a1a28; margin: 20px 0; }
  .lap-row { display: flex; align-items: center; gap: 10px; }
  .lap-input { background: #1a1a1f; border: 1px solid #2a2a3a; color: #e0e0e0; font-family: inherit;
    font-size: 0.95rem; padding: 6px 10px; border-radius: 4px; width: 60px; text-align: center; outline: none; }
  .btn-nextlap { background: #1a1a1f; border: 1px solid #2a2a3a; color: #666; font-family: inherit;
    font-size: 0.7rem; padding: 7px 12px; border-radius: 4px; cursor: pointer; }
  .btn-nextlap:hover { border-color: #555; color: #ccc; }
  .inject-err { font-size: 0.7rem; color: #ef4444; margin-top: 10px; min-height: 1em; }
</style>
"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SimTelemetry</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{background:#000;color:#fff;font-family:'Courier New',monospace;display:flex;flex-direction:column;user-select:none}

/* ── topbar ── */
.tb{flex:none;height:50px;display:flex;align-items:center;padding:0 18px;gap:14px;border-bottom:1px solid #1e1e1e}
.dot{width:9px;height:9px;border-radius:50%;flex:none;background:#444}
.dot.receiving{background:#00ff41;box-shadow:0 0 8px #00ff41;animation:blink 1s infinite}
.dot.race_ended{background:#f59e0b}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.tb-stat{font-size:1rem;letter-spacing:2px;text-transform:uppercase;color:#666;min-width:110px}
.tb-stat.receiving{color:#00ff41}
.tb-stat.race_ended{color:#f59e0b}
.tb-meta{display:flex;gap:20px;flex:1;font-size:.88rem;letter-spacing:1px;overflow:hidden}
.tb-game{color:#888;text-transform:uppercase}
.tb-track{color:#ddd;font-weight:bold;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tb-drs{color:#00ff41;font-weight:bold;display:none;letter-spacing:2px}
.tb-cmp{color:#aaa}
.tb-nav{display:flex;gap:14px;flex:none}
.tb-nav a{font-size:.8rem;color:#666;text-decoration:none;letter-spacing:1px;text-transform:uppercase}
.tb-nav a:hover{color:#ccc}
.tb-nav a.cur{color:#e0e0e0;border-bottom:1px solid #888}

/* ── layout ── */
.main{flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden}

/* ── flash ── */
@keyframes flash-red{0%{box-shadow:inset 0 0 0 2px #ef4444cc}60%{box-shadow:inset 0 0 0 2px #ef444455}100%{box-shadow:inset 0 0 0 2px transparent}}
.flash{animation:flash-red .35s ease-out forwards}

/* ── MAIN PANELS: 4 equal columns ── */
.panels{flex:1;display:flex;min-height:0;overflow:hidden}
.panel-col{flex:1;display:flex;flex-direction:column;padding:12px 14px;border-right:1px solid #111;min-width:0;border-radius:0}
.panel-col:last-child{border-right:none}
.p-lbl{font-size:1rem;color:#888;text-transform:uppercase;letter-spacing:3px;margin-bottom:10px;flex:none}

/* vertical bar (throttle / brake) */
.vbar-wrap{flex:1;background:#0a0a0a;border-radius:3px;position:relative;overflow:hidden;margin-bottom:12px;min-height:0}
.vbar-fill{position:absolute;bottom:0;left:0;right:0;height:0%;border-radius:3px;transition:height .04s linear}
.thr-fill{background:#00ff41}
.brk-fill{background:#ef4444}
.p-num{font-size:2.7rem;font-weight:900;text-align:center;flex:none;line-height:1}
.thr-pct{color:#00ff41}
.brk-pct{color:#ef4444}

/* slip column */
.slip-bars{flex:1;display:flex;gap:12px;min-height:0}
.slip-bar-col{flex:1;display:flex;flex-direction:column;min-width:0}
.slip-bar-lbl{font-size:.88rem;color:#777;text-transform:uppercase;letter-spacing:1px;text-align:center;margin-bottom:6px;flex:none}
.slip-num{font-size:2.1rem;font-weight:900;text-align:center;flex:none;line-height:1;transition:color .1s;margin-top:10px}

/* timing column */
.timing-vals{flex:1;display:flex;flex-direction:column;gap:6px;min-height:0;justify-content:space-around}
.t-lbl{font-size:.82rem;color:#888;text-transform:uppercase;letter-spacing:2px;margin-bottom:1px}
.t-val{font-size:1.6rem;font-weight:900;color:#e0e0e0;letter-spacing:.5px;line-height:1.1}
.t-val.green{color:#22c55e}
.t-delta-row{border-top:1px solid #111;padding-top:8px;margin-top:2px}
.delta-val{font-size:2.1rem;font-weight:900;letter-spacing:-1px;border-radius:3px;padding:0 4px;line-height:1}
.delta-val.ahead{color:#22c55e}
.delta-val.behind{color:#ef4444}
.delta-val.even{color:#555}

/* ── BOTTOM STRIP: Gear · Speed · RPM · Tyres ── */
.bot-panels{flex:none;height:68px;display:flex;align-items:stretch;border-bottom:1px solid #0a0a0a}
.bp{display:flex;flex-direction:column;justify-content:center;align-items:center;padding:0 18px;border-right:1px solid #0e0e0e;flex:none}
.bp-lbl{font-size:.68rem;color:#333;text-transform:uppercase;letter-spacing:2px;margin-bottom:2px}
.gear-val{font-size:2.2rem;font-weight:900;color:#555;line-height:1}
.gear-val.N{color:#2a2a2a}.gear-val.R{color:#7a222288}
.speed-val{font-size:1.8rem;font-weight:900;color:#555;line-height:1}
.speed-unit{font-size:.65rem;color:#333;text-transform:uppercase;letter-spacing:2px;margin-top:1px}
.rpm-bp{flex:1;align-items:stretch;padding:8px 18px;justify-content:center}
.rpm-lbl-row{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:5px}
.rpm-lbl{font-size:.68rem;color:#333;text-transform:uppercase;letter-spacing:2px}
.rpm-pct{font-size:.85rem;font-weight:bold;color:#444}
.rpm-track{background:#080808;border-radius:2px;overflow:hidden;height:12px;position:relative}
.rpm-fill{height:100%;width:0%;transition:width .04s linear}
.rpm-fill.lo  {background:#0f2a18}
.rpm-fill.mid {background:#143a1e}
.rpm-fill.hi  {background:#2a2208}
.rpm-fill.shift{background:#ef4444;animation:sf .1s infinite}
@keyframes sf{0%,100%{box-shadow:inset 0 0 20px #ef000066}50%{box-shadow:inset 0 0 40px #ff440099}}
.rpm-gear-mark{position:absolute;top:0;bottom:0;width:2px;background:#111}
.rpm-num{font-size:.7rem;color:#333;margin-top:3px;letter-spacing:1px}
.tyres-bp{flex:none;align-items:flex-start;justify-content:center;padding:8px 16px}
.tyre-grid{display:grid;grid-template-columns:1fr 1fr;gap:1px 10px}
.tyre-cell{display:flex;align-items:center;gap:4px}
.tyre-corner{font-size:.65rem;color:#444;width:20px;font-weight:bold;letter-spacing:.5px}
.tyre-temp{font-size:.85rem;font-weight:bold;transition:color .2s}
.tyre-temp.cold{color:#4a7aaa}
.tyre-temp.ok  {color:#22c55e}
.tyre-temp.hot {color:#f59e0b}
.tyre-temp.over{color:#ef4444}
.tyre-temp.na  {color:#1e1e1e}

/* ── bottom strip ── */
.bot{flex:none;height:28px;display:flex;align-items:center;padding:0 14px;gap:10px}
.udp-strip{flex:1;font-size:.6rem;color:#222;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.bot-btn{background:none;border:1px solid #111;color:#333;font-family:inherit;font-size:.6rem;padding:2px 8px;border-radius:2px;cursor:pointer;text-transform:uppercase;letter-spacing:1px}
.bot-btn:hover{color:#888;border-color:#333}
.bot-btn.on{color:#00ff41;border-color:#00ff4133}

/* ── debug overlay ── */
#dbg{position:fixed;bottom:28px;left:0;right:0;height:220px;background:#03030a;border-top:1px solid #111;z-index:50;display:none;flex-direction:column}
#dbg .dh{flex:none;display:flex;justify-content:space-between;align-items:center;padding:5px 12px;border-bottom:1px solid #0d0d18}
#dbg .dh span{font-size:.6rem;color:#444;text-transform:uppercase;letter-spacing:2px}
#dbg-log{flex:1;overflow-y:auto;font-size:.66rem;padding:6px 12px;line-height:1.6}

/* finish overlay */
#fo{position:fixed;inset:0;background:#000e;z-index:100;display:none;align-items:center;justify-content:center}
#fo.open{display:flex}
.fo-box{background:#080810;border:1px solid #1e1e2e;border-radius:8px;width:min(520px,95vw);max-height:90vh;display:flex;flex-direction:column;overflow:hidden}
.fo-head{padding:20px 24px 14px;border-bottom:1px solid #111}
.fo-title{font-size:1.4rem;font-weight:900;color:#fff;letter-spacing:-1px;margin-bottom:4px}
.fo-sub{font-size:.85rem;color:#666}
.fo-body{flex:1;overflow-y:auto;padding:20px 24px}
.fo-section{margin-bottom:20px}
.fo-lbl{font-size:.78rem;color:#888;text-transform:uppercase;letter-spacing:2px;margin-bottom:10px}
.type-chips{display:flex;flex-wrap:wrap;gap:8px}
.type-chip{background:#111;border:1px solid #222;color:#666;font-family:inherit;font-size:.82rem;padding:7px 14px;border-radius:20px;cursor:pointer;letter-spacing:.5px;transition:all .1s}
.type-chip:hover{border-color:#444;color:#ccc}
.type-chip.sel{background:#22c55e18;border-color:#22c55e88;color:#22c55e}
.fo-lap-list{display:flex;flex-direction:column;gap:4px}
.fo-lap{display:flex;align-items:center;gap:12px;padding:8px 10px;border-radius:4px;background:#0a0a12;border:1px solid #111}
.fo-lap.partial{border-color:#f59e0b44;background:#1a130a}
.fo-lap-num{font-size:.72rem;color:#555;width:32px}
.fo-lap-time{font-size:1rem;font-weight:900;color:#e0e0e0;flex:1}
.fo-lap-time.best{color:#22c55e}
.fo-lap-badge{font-size:.65rem;color:#f59e0b;letter-spacing:1px;background:#f59e0b18;border:1px solid #f59e0b44;padding:2px 7px;border-radius:10px}
.fo-lap-del{background:none;border:1px solid #ef444444;color:#ef4444;font-family:inherit;font-size:.72rem;padding:3px 9px;border-radius:3px;cursor:pointer}
.fo-lap-del:hover{background:#ef444422}
.fo-lap-del.undone{border-color:#22c55e44;color:#22c55e}
.fo-foot{padding:14px 24px;border-top:1px solid #111;display:flex;gap:10px;justify-content:flex-end}
.fo-save{background:#22c55e;color:#000;border:none;font-family:inherit;font-size:.9rem;font-weight:bold;padding:10px 28px;border-radius:4px;cursor:pointer;letter-spacing:1px}
.fo-save:hover{background:#16a34a}
.fo-skip{background:none;border:1px solid #222;color:#555;font-family:inherit;font-size:.85rem;padding:9px 20px;border-radius:4px;cursor:pointer}
.fo-skip:hover{border-color:#444;color:#aaa}
/* finish race button */
.bot-finish{background:#22c55e;color:#000;border:none;font-family:inherit;font-size:.72rem;font-weight:bold;padding:4px 14px;border-radius:2px;cursor:pointer;letter-spacing:1px;text-transform:uppercase;display:none}
.bot-finish:hover{background:#16a34a}
</style>
</head>
<body>

<div class="tb">
  <div class="dot" id="dot"></div>
  <span class="tb-stat" id="tb-stat">IDLE</span>
  <div class="tb-meta">
    <span class="tb-game" id="tb-game">—</span>
    <span class="tb-track" id="tb-track">—</span>
    <span class="tb-drs" id="tb-drs">DRS</span>
    <span class="tb-cmp" id="tb-cmp"></span>
  </div>
  <nav class="tb-nav">
    <a href="/" class="cur">Live</a>
    <a href="/sessions">Sessions</a>
    <a href="/setup">Setup</a>
    <a href="/admin" id="nav-admin" style="display:none">Admin</a>
  </nav>
</div>
<script>if(location.search.includes('debug=true'))document.getElementById('nav-admin').style.display='';</script>

<div class="main">

  <!-- MAIN PANELS: Throttle | Brake | Rear Slip | Lap Timing -->
  <div class="panels">

    <div class="panel-col" id="thr-row">
      <div class="p-lbl">Throttle</div>
      <div class="vbar-wrap">
        <div class="vbar-fill thr-fill" id="thr-b"></div>
      </div>
      <div class="p-num thr-pct" id="thr-v">0%</div>
    </div>

    <div class="panel-col" id="brk-row">
      <div class="p-lbl">Brake</div>
      <div class="vbar-wrap">
        <div class="vbar-fill brk-fill" id="brk-b"></div>
      </div>
      <div class="p-num brk-pct" id="brk-v">0%</div>
    </div>

    <div class="panel-col" id="slip-panel">
      <div class="p-lbl">Rear Slip</div>
      <div class="slip-bars">
        <div class="slip-bar-col">
          <div class="slip-bar-lbl">RL</div>
          <div class="vbar-wrap">
            <div class="vbar-fill" id="srl-b"></div>
          </div>
          <div class="slip-num" id="srl-v">—</div>
        </div>
        <div class="slip-bar-col">
          <div class="slip-bar-lbl">RR</div>
          <div class="vbar-wrap">
            <div class="vbar-fill" id="srr-b"></div>
          </div>
          <div class="slip-num" id="srr-v">—</div>
        </div>
      </div>
    </div>

    <div class="panel-col">
      <div class="p-lbl">Lap Timing</div>
      <div class="timing-vals">
        <div>
          <div class="t-lbl">Current</div>
          <div class="t-val" id="t-cur">—</div>
        </div>
        <div>
          <div class="t-lbl">Best</div>
          <div class="t-val green" id="t-best">—</div>
        </div>
        <div>
          <div class="t-lbl">Last</div>
          <div class="t-val" id="t-last">—</div>
        </div>
        <div>
          <div class="t-lbl">Lap</div>
          <div class="t-val" id="t-lap">—</div>
        </div>
        <div class="t-delta-row">
          <div class="t-lbl">Delta</div>
          <div class="delta-val even" id="t-delta">—</div>
        </div>
      </div>
    </div>

  </div>

  <!-- BOTTOM STRIP: Gear · Speed · RPM · Tyres -->
  <div class="bot-panels">
    <div class="bp">
      <div class="bp-lbl">Gear</div>
      <div class="gear-val" id="gear">—</div>
    </div>
    <div class="bp">
      <div class="bp-lbl">Speed</div>
      <div class="speed-val" id="spd">—</div>
      <div class="speed-unit">mph</div>
    </div>
    <div class="bp rpm-bp">
      <div class="rpm-lbl-row">
        <span class="rpm-lbl">RPM</span>
        <span class="rpm-pct" id="rpm-pct">—</span>
      </div>
      <div class="rpm-track">
        <div class="rpm-fill" id="rpm-fill"></div>
        <div class="rpm-gear-mark" id="rpm-mark" style="left:75%"></div>
      </div>
      <div class="rpm-num" id="rpm-num">—</div>
    </div>
    <div class="bp tyres-bp">
      <div class="bp-lbl">Tyres</div>
      <div class="tyre-grid">
        <div class="tyre-cell"><span class="tyre-corner">FL</span><span class="tyre-temp na" id="ty-fl">—</span></div>
        <div class="tyre-cell"><span class="tyre-corner">FR</span><span class="tyre-temp na" id="ty-fr">—</span></div>
        <div class="tyre-cell"><span class="tyre-corner">RL</span><span class="tyre-temp na" id="ty-rl">—</span></div>
        <div class="tyre-cell"><span class="tyre-corner">RR</span><span class="tyre-temp na" id="ty-rr">—</span></div>
      </div>
      <span style="font-size:.6rem;color:#2a2a2a;margin-top:2px" id="ty-cmp"></span>
    </div>
  </div>

</div><!-- /main -->

<div class="bot">
  <div class="udp-strip" id="udp-strip"></div>
  <button class="bot-finish" id="btn-finish" onclick="openFinish()">Finish Race</button>
  <button class="bot-btn" onclick="resetCounters()">Reset</button>
  <button class="bot-btn" id="dbg-btn" onclick="toggleDebug()">Debug</button>
</div>

<div id="dbg">
  <div class="dh">
    <span>Debug Console</span>
    <div style="display:flex;gap:10px;align-items:center">
      <label style="font-size:.6rem;color:#444;cursor:pointer;display:flex;align-items:center;gap:4px"><input type="checkbox" id="dbg-as" checked> scroll</label>
      <select id="dbg-f" onchange="applyFilter()" style="background:#0a0a12;border:1px solid #1a1a28;color:#555;font-family:inherit;font-size:.6rem;padding:2px 6px;border-radius:2px">
        <option value="all">All</option><option value="warn">Warn+</option><option value="udp">UDP</option>
      </select>
      <button onclick="clearDebug()" style="background:none;border:1px solid #1a1a28;color:#444;font-family:inherit;font-size:.6rem;padding:2px 8px;border-radius:2px;cursor:pointer">Clear</button>
    </div>
  </div>
  <div id="dbg-log"></div>
</div>

<div id="fo">
  <div class="fo-box">
    <div class="fo-head">
      <div class="fo-title" id="fo-title">Session Complete</div>
      <div class="fo-sub" id="fo-sub">—</div>
    </div>
    <div class="fo-body">
      <div class="fo-section">
        <div class="fo-lbl">Session Type</div>
        <div class="type-chips">
          <button class="type-chip" data-val="practice" onclick="selType(this)">Practice</button>
          <button class="type-chip" data-val="time_trial" onclick="selType(this)">Time Trial</button>
          <button class="type-chip" data-val="qualifying" onclick="selType(this)">Qualifying</button>
          <button class="type-chip" data-val="race_ai" onclick="selType(this)">Race vs AI</button>
          <button class="type-chip" data-val="race_online" onclick="selType(this)">Online Race</button>
          <button class="type-chip" data-val="hot_lap" onclick="selType(this)">Hot Lap</button>
        </div>
      </div>
      <div class="fo-section">
        <div class="fo-lbl">Laps</div>
        <div class="fo-lap-list" id="fo-laps"></div>
      </div>
    </div>
    <div class="fo-foot">
      <button class="fo-skip" onclick="closeFinish()">Skip</button>
      <button class="fo-save" onclick="saveFinish()">Save</button>
    </div>
  </div>
</div>

<script>
const $=id=>document.getElementById(id);
const es=new EventSource('/stream');
let _maxRpm=8500,_dbgEs=null,_dbgOpen=false,_bestLap=null;
let state_sid=null;
const _dbgLines=[];
const _flashTimers={};

function flash(id){
  const el=$(id); if(!el)return;
  if(_flashTimers[id])clearTimeout(_flashTimers[id]);
  el.classList.remove('flash');
  void el.offsetWidth; // reflow to restart animation
  el.classList.add('flash');
  _flashTimers[id]=setTimeout(()=>el.classList.remove('flash'),400);
}

function fmt(s){
  if(s==null)return'—';
  const m=Math.floor(s/60);
  return m+':'+(s%60).toFixed(3).padStart(6,'0');
}

function slipColor(v){
  if(v<0.1)return'#22c55e';
  if(v<0.3)return'#f59e0b';
  return'#ef4444';
}

function tyreClass(t){
  if(t==null)return'na';
  if(t<170)return'cold';
  if(t<=210)return'ok';
  if(t<=230)return'hot';
  return'over';
}

function setSlip(pfx,val){
  const v=val??0;
  const pct=Math.min(100,v/0.5*100);
  const col=slipColor(v);
  $(pfx+'-b').style.height=pct+'%';
  $(pfx+'-b').style.background=col;
  $(pfx+'-v').textContent=val!=null?v.toFixed(3):'—';
  $(pfx+'-v').style.color=col;
}

es.onmessage=e=>{
  const d=JSON.parse(e.data);
  const recv=d.status==='receiving';
  const ended=d.status==='race_ended';

  // topbar
  $('dot').className='dot '+(recv?'receiving':ended?'race_ended':'idle');
  $('tb-stat').textContent=ended?'RACE ENDED':(d.status||'IDLE').toUpperCase();
  $('tb-stat').className='tb-stat'+(recv?' receiving':ended?' race_ended':'');
  $('tb-game').textContent=d.game?d.game.replace(/_/g,' ').toUpperCase():'—';
  $('tb-track').textContent=d.track&&d.track!=='unknown'?d.track:'—';
  $('tb-drs').style.display=d.drs?'inline':'none';
  $('tb-cmp').textContent=d.tyre_compound||'';

  // track session id and show/hide finish button
  if(d.session_id) state_sid = d.session_id;
  $('btn-finish').style.display = (recv||ended) ? 'inline-block' : 'none';

  // gear
  const g=d.gear;
  const ge=$('gear');
  ge.textContent=g==null?'—':g===0?'N':g===-1?'R':String(g);
  ge.className='gear-val'+(g===0?' N':g===-1?' R':'');

  // speed
  $('spd').textContent=d.speed_mph!=null?d.speed_mph.toFixed(0):'—';

  // rpm bar
  const rpm=d.rpm||0;
  if(d.engine_max_rpm&&d.engine_max_rpm>2000)_maxRpm=d.engine_max_rpm;
  const rPct=Math.min(100,rpm/_maxRpm*100);
  const rf=$('rpm-fill');
  rf.style.width=rPct+'%';
  rf.className='rpm-fill '+(rPct>=88?'shift':rPct>=75?'hi':rPct>=55?'mid':'lo');
  $('rpm-pct').textContent=rPct>0?Math.round(rPct)+'%':'—';
  $('rpm-num').textContent=rpm?Math.round(rpm).toLocaleString()+' rpm':'—';

  // pedals
  const thr=d.throttle_pct||0,brk=d.brake_pct||0;
  $('thr-b').style.height=thr+'%';
  $('thr-v').textContent=Math.round(thr)+'%';
  $('brk-b').style.height=brk+'%';
  $('brk-v').textContent=Math.round(brk)+'%';
  // flash throttle row when braking and on throttle simultaneously (conflicting inputs)
  if(brk>15&&thr>30) flash('thr-row');
  // flash brake row at near-lock ABS territory
  if(brk>92) flash('brk-row');

  // slip
  setSlip('srl',d.slip_rl);
  setSlip('srr',d.slip_rr);
  // flash slip panel on oversteer threshold
  if((d.slip_rl||0)>0.3||(d.slip_rr||0)>0.3) flash('slip-panel');

  // timing
  $('t-cur').textContent=fmt(d.current_lap_time);
  $('t-best').textContent=fmt(d.best_lap_time_s);
  $('t-last').textContent=fmt(d.last_lap_time_s);
  $('t-lap').textContent=d.lap!=null?'L'+d.lap:'—';

  // delta to best
  const dEl=$('t-delta');
  if(d.current_lap_time!=null&&d.best_lap_time_s!=null){
    const delta=d.current_lap_time-d.best_lap_time_s;
    const sign=delta<0?'':'+';
    dEl.textContent=sign+delta.toFixed(3)+'s';
    dEl.className='delta-val '+(delta<-0.01?'ahead':delta>0.01?'behind':'even');
    // flash delta when significantly behind pace
    if(delta>1.5) flash('t-delta');
  } else {
    dEl.textContent='—'; dEl.className='delta-val even';
  }

  // tyres
  ['fl','fr','rl','rr'].forEach(c=>{
    const el=$('ty-'+c);
    const t=d['tyre_'+c];
    el.textContent=t!=null?Math.round(t)+'°':'—';
    el.className='tyre-temp '+tyreClass(t);
  });
  $('ty-cmp').textContent=d.tyre_compound||'';

  // udp strip
  const udp=d.udp_received||{},rej=d.udp_rejected||{},rsz=d.last_rejected_size||{};
  $('udp-strip').innerHTML=['forza_motorsport','acc','f1'].map(gm=>{
    const n=udp[gm]||0,r=rej[gm]||0,sz=rsz[gm];
    const c=n>0?'#22c55e33':r>0?'#ef444433':'#1a1a1a';
    return `<span style="color:${c}">${gm.replace('_motorsport','').replace('_',' ')}: ${n}ok${r?' '+r+'rej':''}${sz?' ('+sz+'B)':''}</span>`;
  }).join('<span style="color:#0d0d0d"> · </span>');
};

es.onerror=()=>{$('dot').className='dot';};

async function resetCounters(){await fetch('/reset',{method:'POST'});}

function toggleDebug(){
  _dbgOpen=!_dbgOpen;
  $('dbg').style.display=_dbgOpen?'flex':'none';
  $('dbg-btn').className='bot-btn'+(_dbgOpen?' on':'');
  if(_dbgOpen&&!_dbgEs)startDbg();
}
function startDbg(){
  _dbgEs=new EventSource('/debug-stream');
  _dbgEs.onmessage=e=>addDbg(JSON.parse(e.data));
  _dbgEs.onerror=()=>{_dbgEs=null;if(_dbgOpen)setTimeout(startDbg,2000);};
}
function lnColor(l){
  if(l.includes('[ERROR]'))return'#ef4444';
  if(l.includes('[WARNING]')||l.includes('[REJECTED]'))return'#f59e0b';
  if(l.includes('[UDP OK]'))return'#22c55e33';
  return'#1e1e2a';
}
function lnVis(l){
  const f=$('dbg-f').value;
  if(f==='warn')return l.includes('[ERROR]')||l.includes('[WARNING]')||l.includes('[REJECTED]');
  if(f==='udp')return l.includes('[UDP OK]')||l.includes('[REJECTED]');
  return true;
}
function addDbg(line){
  _dbgLines.push(line);if(_dbgLines.length>2000)_dbgLines.shift();
  if(!lnVis(line))return;
  const el=$('dbg-log');
  const d=document.createElement('div');
  d.style.cssText='color:'+lnColor(line)+';border-bottom:1px solid #08080f;padding:1px 0';
  d.textContent=line;el.appendChild(d);
  while(el.children.length>1000)el.removeChild(el.firstChild);
  if($('dbg-as').checked)el.scrollTop=el.scrollHeight;
}
function applyFilter(){
  const el=$('dbg-log');el.innerHTML='';
  _dbgLines.filter(lnVis).slice(-500).forEach(l=>{
    const d=document.createElement('div');
    d.style.cssText='color:'+lnColor(l)+';border-bottom:1px solid #08080f;padding:1px 0';
    d.textContent=l;el.appendChild(d);
  });
  el.scrollTop=el.scrollHeight;
}
function clearDebug(){_dbgLines.length=0;$('dbg-log').innerHTML='';}

// ── Finish Race overlay ───────────────────────────────────────────────────────
let _foSid=null, _foRaceType=null, _foDropLast=false, _foLaps=[], _foClosed=false;

function selType(el){
  document.querySelectorAll('.type-chip').forEach(c=>c.classList.remove('sel'));
  el.classList.add('sel');
  _foRaceType=el.dataset.val;
}

async function openFinish(){
  // Close session immediately if still active
  const sid = state_sid || null;
  await fetch('/finish',{method:'POST'});
  // Wait a tick for state to update
  await new Promise(r=>setTimeout(r,400));
  // Find the session we just closed — get from /status
  const st = await fetch('/status').then(r=>r.json());
  _foSid = sid || st.session_id;
  _foRaceType = null;
  _foDropLast = false;
  _foClosed = false;

  if(!_foSid){ alert('No active session to finish.'); return; }

  // Load session + laps
  try {
    const d = await fetch('/sessions/session/data?id='+encodeURIComponent(_foSid)).then(r=>r.json());
    const cur = d.session;
    if(!cur){ closeFinish(); return; }
    _foSid = cur.session_id;
    _foLaps = d.laps || [];

    $('fo-title').textContent = cur.track&&cur.track!=='unknown' ? cur.track : 'Session Complete';
    $('fo-sub').textContent = (cur.game||'').replace(/_/g,' ') + (cur.started_at?' · '+new Date(cur.started_at).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'');

    // Pre-select race_type if already set
    if(cur.race_type){
      const chip = document.querySelector(`.type-chip[data-val="${cur.race_type}"]`);
      if(chip){ selType(chip); }
    }
    renderFoLaps();
  } catch(e){ console.error(e); return; }

  $('fo').classList.add('open');
}

function renderFoLaps(){
  const best = _foClosed ? null : Math.min(..._foLaps.filter(l=>l.lap_time_s).map(l=>l.lap_time_s));
  const lastIdx = _foLaps.length - 1;
  $('fo-laps').innerHTML = _foLaps.map((lap,i)=>{
    const t = lap.lap_time_s;
    const isLast = i===lastIdx;
    const isBest = t && t===best;
    const isPartial = isLast && (!t || _foDropLast);
    const timeStr = t ? fmt(t) : 'partial';
    const delBtn = isLast
      ? `<button class="fo-lap-del${_foDropLast?' undone':''}" onclick="toggleDropLast()">${_foDropLast?'Restore':'Delete'}</button>`
      : '';
    return `<div class="fo-lap${isPartial?' partial':''}">
      <span class="fo-lap-num">L${lap.lap_number}</span>
      <span class="fo-lap-time${isBest&&!_foDropLast?' best':''}">${timeStr}</span>
      ${isPartial&&!_foDropLast?'<span class="fo-lap-badge">PARTIAL</span>':''}
      ${delBtn}
    </div>`;
  }).join('');
}

function toggleDropLast(){
  _foDropLast = !_foDropLast;
  renderFoLaps();
}

async function saveFinish(){
  if(!_foSid) return;
  const body = { id: _foSid };
  if(_foRaceType) body.race_type = _foRaceType;
  if(_foDropLast) body.drop_last_lap = true;
  await fetch('/sessions/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  closeFinish();
}

function closeFinish(){
  $('fo').classList.remove('open');
}
</script>
</body>
</html>
"""

SETUP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SimTelemetry · Setup</title>
""" + _PAGE_STYLE + r"""
</head>
<body>
<div class="topbar">
  <h1>SimTelemetry</h1>
  <nav>
    <a href="/">Live</a>
    <a href="/sessions">Sessions</a>
    <a href="/setup" class="active">Setup</a>
    <a href="/admin" id="nav-admin" style="display:none">Admin</a>
  </nav>
</div>
<script>if(location.search.includes('debug=true'))document.getElementById('nav-admin').style.display='';</script>

<div class="section">
  <div class="section-title">Storage</div>
  <div class="field">
    <label>Storage path — where raw archives and session JSON files are saved</label>
    <div class="path-row">
      <input type="text" id="storage_path" placeholder="/mnt/usb/simtelemetry"
             oninput="scheduleValidate()" onblur="validateNow()">
      <button type="button" class="btn-browse" onclick="toggleBrowse()">Browse</button>
    </div>
    <div id="path-status" class="path-status"></div>
    <div id="browse-panel" class="browse-panel" style="display:none">
      <div class="browse-toolbar">
        <div id="breadcrumb" class="breadcrumb"></div>
        <button type="button" class="btn-use" onclick="selectDir()">Use this directory</button>
      </div>
      <div id="dir-list" class="dir-list"></div>
    </div>
    <div class="hint">USB mount point on Pi; any writable directory on Mac/Windows.</div>
  </div>
  <div id="disk-info" class="disk-info"></div>
</div>

<div class="section">
  <div class="section-title">Session</div>
  <div class="field">
    <label>Session timeout (seconds) — silence before a session is closed</label>
    <input type="number" id="session_timeout_s" min="2" max="120" step="1">
  </div>
</div>

<div class="section">
  <div class="section-title">UDP Ports <span style="color:#444;font-size:0.6rem;margin-left:8px">restart required for port changes</span></div>
  <div class="ports-grid">
    <div class="field">
      <label>Forza Motorsport</label>
      <input type="number" id="port_forza" min="1024" max="65535">
    </div>
    <div class="field">
      <label>ACC</label>
      <input type="number" id="port_acc" min="1024" max="65535">
    </div>
    <div class="field">
      <label>F1 (Codemasters)</label>
      <input type="number" id="port_f1" min="1024" max="65535">
    </div>
  </div>
</div>

<div class="section">
  <div class="section-title">AI Analysis</div>
  <div class="field">
    <label>Anthropic API key — used for post-session race analysis</label>
    <input type="password" id="anthropic_api_key" placeholder="sk-ant-…" autocomplete="off">
    <div class="hint">Get a key at console.anthropic.com. Stored locally in simtelemetry.config.json.</div>
  </div>
  <div class="field">
    <label>Model</label>
    <select id="anthropic_model" style="width:100%;background:#1a1a1f;border:1px solid #2a2a3a;color:#e0e0e0;font-family:inherit;font-size:0.85rem;padding:8px 10px;border-radius:4px;outline:none">
      <option value="claude-sonnet-4-6">Claude Sonnet 4.6 — best balance of speed and quality</option>
      <option value="claude-opus-4-7">Claude Opus 4.7 — most capable, slower</option>
      <option value="claude-haiku-4-5-20251001">Claude Haiku 4.5 — fastest, lowest cost</option>
    </select>
  </div>
</div>

<button class="btn" id="save-btn" onclick="save()">Save</button>
<div class="toast" id="toast"></div>

<script>
// ── path validation ──────────────────────────────────────────────────────────
let _vTimer = null;
function scheduleValidate() { clearTimeout(_vTimer); _vTimer = setTimeout(validateNow, 350); }
function validateNow() { validatePath(document.getElementById('storage_path').value.trim()); }

async function validatePath(path) {
  const el = document.getElementById('path-status');
  if (!path) { el.textContent = ''; return; }
  el.style.color = '#555'; el.textContent = 'checking…';
  try {
    const d = await fetch('/browse?path=' + encodeURIComponent(path)).then(r => r.json());
    if (d.exists) {
      el.style.color = '#22c55e'; el.textContent = '✓ path exists';
    } else if (d.parent_exists) {
      el.style.color = '#f59e0b'; el.textContent = '⚠ will be created on save';
    } else {
      el.style.color = '#ef4444'; el.textContent = '✗ parent directory does not exist';
    }
  } catch(e) { el.style.color = '#444'; el.textContent = ''; }
}

// ── file browser ─────────────────────────────────────────────────────────────
let _browseOpen = false;

function toggleBrowse() {
  _browseOpen = !_browseOpen;
  document.getElementById('browse-panel').style.display = _browseOpen ? 'block' : 'none';
  if (_browseOpen) loadPath(document.getElementById('storage_path').value.trim() || '/');
}

async function loadPath(path) {
  const panel = document.getElementById('browse-panel');
  const list  = document.getElementById('dir-list');
  panel.dataset.cur = path;
  list.innerHTML = '<div class="dir-empty">Loading…</div>';
  try {
    const d = await fetch('/browse?path=' + encodeURIComponent(path)).then(r => r.json());
    panel.dataset.cur = d.path;
    renderBreadcrumb(d.path);
    list.innerHTML = '';
    if (d.parent && d.parent !== d.path) {
      const up = mkDir('↑  ..', () => loadPath(d.parent));
      up.style.color = '#444';
      list.appendChild(up);
    }
    if (!d.entries || !d.entries.length) {
      list.innerHTML += '<div class="dir-empty">No subdirectories</div>';
    }
    (d.entries || []).forEach(e => {
      const full = d.path.replace(/\/+$/, '') + '/' + e.name;
      list.appendChild(mkDir('▸  ' + e.name, () => loadPath(full)));
    });
  } catch(e) {
    list.innerHTML = '<div class="dir-empty" style="color:#ef4444">' + e.message + '</div>';
  }
}

function mkDir(text, onclick) {
  const el = document.createElement('div');
  el.className = 'dir-item'; el.textContent = text; el.onclick = onclick;
  return el;
}

function renderBreadcrumb(path) {
  const bc = document.getElementById('breadcrumb');
  const parts = path.split('/').filter(Boolean);
  let html = '<span class="crumb" data-p="/">/</span>';
  let built = '';
  parts.forEach((seg, i) => {
    built += '/' + seg;
    html += '<span class="crumb-sep"> / </span>';
    const cls = i === parts.length - 1 ? 'crumb-cur' : 'crumb';
    html += '<span class="' + cls + '" data-p="' + built + '">' + seg + '</span>';
  });
  bc.innerHTML = html;
  bc.querySelectorAll('.crumb').forEach(el => { el.onclick = () => loadPath(el.dataset.p); });
}

function selectDir() {
  const path = document.getElementById('browse-panel').dataset.cur;
  if (path) { document.getElementById('storage_path').value = path; validatePath(path); }
  _browseOpen = false;
  document.getElementById('browse-panel').style.display = 'none';
}

// ── config load / save ────────────────────────────────────────────────────────
async function load() {
  const d = await fetch('/config').then(r => r.json());
  document.getElementById('storage_path').value      = d.storage_path || '';
  document.getElementById('session_timeout_s').value = d.session_timeout_s || 10;
  document.getElementById('port_forza').value        = (d.ports || {}).forza_motorsport || 5300;
  document.getElementById('port_acc').value          = (d.ports || {}).acc || 9996;
  document.getElementById('port_f1').value           = (d.ports || {}).f1 || 20777;
  document.getElementById('anthropic_api_key').value = d.anthropic_api_key || '';
  const modelSel = document.getElementById('anthropic_model');
  if (d.anthropic_model) modelSel.value = d.anthropic_model;
  renderDisk(d.disk);
  if (d.storage_path) validatePath(d.storage_path);
}

function renderDisk(disk) {
  const el = document.getElementById('disk-info');
  if (!disk || disk.total_gb == null) { el.textContent = ''; return; }
  const pct = Math.round(disk.used_gb / disk.total_gb * 100);
  el.innerHTML = `
    <div class="disk-bar-bg"><div class="disk-bar-fill" style="width:${pct}%"></div></div>
    <span>${disk.used_gb} GB used of ${disk.total_gb} GB &mdash; <span>${disk.free_gb} GB free</span></span>`;
}

async function save() {
  const btn = document.getElementById('save-btn');
  const toast = document.getElementById('toast');
  btn.disabled = true; toast.className = 'toast';
  const body = {
    storage_path:      document.getElementById('storage_path').value.trim(),
    session_timeout_s: parseInt(document.getElementById('session_timeout_s').value, 10),
    ports: {
      forza_motorsport: parseInt(document.getElementById('port_forza').value, 10),
      acc:              parseInt(document.getElementById('port_acc').value, 10),
      f1:               parseInt(document.getElementById('port_f1').value, 10),
    },
    anthropic_api_key: document.getElementById('anthropic_api_key').value.trim(),
    anthropic_model:   document.getElementById('anthropic_model').value,
  };
  try {
    const r = await fetch('/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const d = await r.json();
    if (r.ok) {
      toast.className = 'toast ok'; toast.textContent = d.message || 'Saved.';
      renderDisk(d.disk); validatePath(body.storage_path);
    } else {
      toast.className = 'toast err'; toast.textContent = d.error || 'Save failed.';
    }
  } catch(e) { toast.className = 'toast err'; toast.textContent = 'Network error: ' + e.message; }
  btn.disabled = false;
}

load();
</script>
</body>
</html>
"""


ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SimTelemetry · Admin</title>
""" + _PAGE_STYLE + r"""
</head>
<body>
<div class="topbar">
  <h1>SimTelemetry</h1>
  <nav>
    <a href="/">Live</a>
    <a href="/sessions">Sessions</a>
    <a href="/setup">Setup</a>
    <a href="/admin" class="active">Admin</a>
  </nav>
</div>

<div class="tabs">
  <button class="tab active" onclick="setGame('forza_motorsport',this)">Forza</button>
  <button class="tab" onclick="setGame('acc',this)">ACC</button>
  <button class="tab" onclick="setGame('f1',this)">F1</button>
</div>

<div class="ctrl-grid">
  <div class="ctrl">
    <label>Speed <span class="val"><span id="speed-val">0</span> mph</span></label>
    <input type="range" id="speed" min="0" max="220" step="1" value="0" oninput="sync('speed','speed-val')">
  </div>
  <div class="ctrl">
    <label>RPM <span class="val"><span id="rpm-val">1000</span></span></label>
    <input type="range" id="rpm" min="0" max="12000" step="100" value="1000" oninput="sync('rpm','rpm-val')">
  </div>
  <div class="ctrl">
    <label>Throttle <span class="val"><span id="thr-val">0</span>%</span></label>
    <input type="range" id="throttle" min="0" max="100" step="1" value="0" oninput="sync('throttle','thr-val')">
  </div>
  <div class="ctrl">
    <label>Brake <span class="val"><span id="brk-val">0</span>%</span></label>
    <input type="range" id="brake" min="0" max="100" step="1" value="0" oninput="sync('brake','brk-val')">
  </div>
</div>

<div class="ctrl" style="margin-bottom:16px">
  <label>Gear</label>
  <div class="gear-row" id="gear-row">
    <button class="gear-btn" onclick="setGear(-1,this)">R</button>
    <button class="gear-btn" onclick="setGear(0,this)">N</button>
    <button class="gear-btn active" onclick="setGear(1,this)">1</button>
    <button class="gear-btn" onclick="setGear(2,this)">2</button>
    <button class="gear-btn" onclick="setGear(3,this)">3</button>
    <button class="gear-btn" onclick="setGear(4,this)">4</button>
    <button class="gear-btn" onclick="setGear(5,this)">5</button>
    <button class="gear-btn" onclick="setGear(6,this)">6</button>
    <button class="gear-btn" onclick="setGear(7,this)">7</button>
    <button class="gear-btn" onclick="setGear(8,this)">8</button>
  </div>
</div>

<div class="ctrl" style="margin-bottom:16px">
  <label>Lap</label>
  <div class="lap-row">
    <input type="number" class="lap-input" id="lap" value="1" min="0" max="99">
    <button class="btn-nextlap" onclick="nextLap()">Next Lap ↑</button>
  </div>
</div>

<hr class="admin-divider">

<div class="preset-row">
  <button class="preset-btn" onclick="applyPreset('idle')">Idle</button>
  <button class="preset-btn" onclick="applyPreset('cruise')">Cruise</button>
  <button class="preset-btn" onclick="applyPreset('full')">Full Throttle</button>
  <button class="preset-btn" onclick="applyPreset('brake')">Braking</button>
  <button class="preset-btn" onclick="applyPreset('pit')">Pit Lane</button>
</div>

<div class="action-row" style="margin-top:20px">
  <button class="btn-inject" onclick="sendOnce()">Send Once</button>
  <button class="btn-stream" id="stream-btn" onclick="toggleStream()">▶ Stream</button>
  <select class="hz-sel" id="hz-sel">
    <option value="1000">1 Hz</option>
    <option value="200">5 Hz</option>
    <option value="100" selected>10 Hz</option>
    <option value="50">20 Hz</option>
    <option value="33">30 Hz</option>
  </select>
  <div class="sent-lbl">Sent: <span id="sent-count">0</span></div>
</div>
<div class="inject-err" id="inject-err"></div>

<script>
let _game = 'forza_motorsport';
let _gear = 1;
let _streamTimer = null;
let _sentCount = 0;

function setGame(g, el) {
  _game = g;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
}

function sync(sliderId, valId) {
  document.getElementById(valId).textContent = document.getElementById(sliderId).value;
}

function setGear(g, el) {
  _gear = g;
  document.querySelectorAll('.gear-btn').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
}

function nextLap() {
  const el = document.getElementById('lap');
  el.value = parseInt(el.value || 0) + 1;
}

const PRESETS = {
  idle:   { speed: 0,   rpm: 900,  throttle: 0,  brake: 0,  gear: 0  },
  cruise: { speed: 100, rpm: 4000, throttle: 35, brake: 0,  gear: 5  },
  full:   { speed: 160, rpm: 9500, throttle: 100,brake: 0,  gear: 6  },
  brake:  { speed: 80,  rpm: 5000, throttle: 0,  brake: 90, gear: 4  },
  pit:    { speed: 37,  rpm: 2500, throttle: 20, brake: 0,  gear: 2  },
};

function applyPreset(name) {
  const p = PRESETS[name];
  if (!p) return;
  document.getElementById('speed').value    = p.speed;    sync('speed','speed-val');
  document.getElementById('rpm').value      = p.rpm;      sync('rpm','rpm-val');
  document.getElementById('throttle').value = p.throttle; sync('throttle','thr-val');
  document.getElementById('brake').value    = p.brake;    sync('brake','brk-val');
  // set gear button
  const gearMap = { '-1':'R', '0':'N', '1':'1','2':'2','3':'3','4':'4','5':'5','6':'6','7':'7','8':'8' };
  document.querySelectorAll('.gear-btn').forEach(b => {
    const g = b.textContent.trim();
    const match = String(p.gear) === Object.keys(gearMap).find(k => gearMap[k] === g);
    b.classList.toggle('active', match);
    if (match) _gear = p.gear;
  });
  _gear = p.gear;
  document.querySelectorAll('.gear-btn').forEach(b => {
    b.classList.remove('active');
    if ((p.gear === -1 && b.textContent === 'R') ||
        (p.gear === 0 && b.textContent === 'N') ||
        (String(p.gear) === b.textContent)) {
      b.classList.add('active');
    }
  });
}

function params() {
  return {
    game:         _game,
    speed_mph:    parseFloat(document.getElementById('speed').value),
    rpm:          parseFloat(document.getElementById('rpm').value),
    throttle_pct: parseFloat(document.getElementById('throttle').value),
    brake_pct:    parseFloat(document.getElementById('brake').value),
    gear:         _gear,
    lap:          parseInt(document.getElementById('lap').value || 1),
  };
}

async function sendOnce() {
  const errEl = document.getElementById('inject-err');
  errEl.textContent = '';
  try {
    const r = await fetch('/admin/inject', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params()),
    });
    const d = await r.json();
    if (!r.ok) { errEl.textContent = d.error || 'Inject failed'; return; }
    _sentCount += d.sent || 1;
    document.getElementById('sent-count').textContent = _sentCount;
  } catch(e) { errEl.textContent = 'Network error: ' + e.message; }
}

function toggleStream() {
  const btn = document.getElementById('stream-btn');
  if (_streamTimer) {
    clearInterval(_streamTimer);
    _streamTimer = null;
    btn.classList.remove('on');
    btn.textContent = '▶ Stream';
  } else {
    const hz = parseInt(document.getElementById('hz-sel').value);
    _streamTimer = setInterval(sendOnce, hz);
    btn.classList.add('on');
    btn.textContent = '■ Stop';
  }
}
</script>
</body>
</html>
"""


GAMES_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SimTelemetry &middot; Sessions</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#000;color:#e0e0e0;font-family:'Courier New',monospace;min-height:100vh}
a{color:inherit;text-decoration:none}
.tb{height:50px;display:flex;align-items:center;padding:0 18px;gap:14px;border-bottom:1px solid #1e1e1e;position:sticky;top:0;background:#000;z-index:10}
.tb h1{font-size:1.3rem;color:#e0e0e0;letter-spacing:3px;text-transform:uppercase;flex:1}
.tb-nav{display:flex;gap:14px}
.tb-nav a{font-size:.8rem;color:#666;letter-spacing:1px;text-transform:uppercase}
.tb-nav a:hover{color:#ccc}
.tb-nav a.cur{color:#e0e0e0;border-bottom:1px solid #888}
.page{padding:32px 28px}
.page-hdr{margin-bottom:28px}
.page-hdr h2{font-size:1.1rem;color:#e0e0e0;letter-spacing:2px;text-transform:uppercase}
.games-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}
.gc{background:#060608;border:1px solid #1a1a1a;padding:24px 22px;cursor:pointer;transition:border-color .15s}
.gc:hover{border-color:#2a2a3a;background:#08080e}
.gc.empty{opacity:.4;cursor:default}
.gc.empty:hover{border-color:#1a1a1a;background:#060608}
.gc-name{font-size:1.6rem;font-weight:900;color:#e0e0e0;letter-spacing:1px;margin-bottom:4px}
.gc-desc{font-size:.72rem;color:#444;letter-spacing:.5px;margin-bottom:18px}
.gc-stats{display:flex;gap:24px}
.gc-stat .v{font-size:1.2rem;font-weight:900;color:#fff}
.gc-stat .l{font-size:.65rem;color:#555;text-transform:uppercase;letter-spacing:1px;margin-top:1px}
.gc-last{font-size:.72rem;color:#333;margin-top:14px;border-top:1px solid #0e0e0e;padding-top:10px}
</style>
</head>
<body>
<div class="tb">
  <h1>SimTelemetry</h1>
  <nav class="tb-nav">
    <a href="/">Live</a>
    <a href="/sessions" class="cur">Sessions</a>
    <a href="/setup">Setup</a>
    <a href="/admin" id="nav-admin" style="display:none">Admin</a>
  </nav>
</div>
<script>if(location.search.includes('debug=true'))document.getElementById('nav-admin').style.display='';</script>
<div class="page">
  <div class="page-hdr"><h2>Sessions</h2></div>
  <div class="games-grid" id="grid"><div style="color:#333;padding:24px">Loading&hellip;</div></div>
</div>
<script>
const GAME_LABELS={'forza_motorsport':'Forza','acc':'ACC','f1':'F1'};
const GAME_DESC={'forza_motorsport':'Forza Motorsport / Horizon','acc':'Assetto Corsa Competizione','f1':'F1 2023 / 2024'};
function fmtDate(iso){if(!iso)return null;return new Date(iso).toLocaleDateString([],{month:'short',day:'numeric',year:'numeric'});}
async function init(){
  let games=[];
  try{games=await fetch('/sessions/games').then(r=>r.json());}catch(e){}
  const grid=document.getElementById('grid');
  if(!games.length){grid.innerHTML='<div style="color:#333;padding:24px">No sessions recorded yet</div>';return;}
  grid.innerHTML=games.map((g,i)=>{
    const label=GAME_LABELS[g.game]||g.game;
    const desc=GAME_DESC[g.game]||'';
    const empty=!g.session_count;
    const last=fmtDate(g.last_played);
    return `<div class="gc${empty?' empty':''}" data-i="${i}">
      <div class="gc-name">${label}</div>
      <div class="gc-desc">${desc}</div>
      <div class="gc-stats">
        <div class="gc-stat"><div class="v">${g.session_count||0}</div><div class="l">Sessions</div></div>
        <div class="gc-stat"><div class="v">${g.track_count||0}</div><div class="l">Tracks</div></div>
      </div>
      ${last?`<div class="gc-last">Last played ${last}</div>`:'<div class="gc-last">No sessions yet</div>'}
    </div>`;
  }).join('');
  document.querySelectorAll('.gc:not(.empty)').forEach((el,i)=>{
    const game=games.filter(g=>g.session_count)[i]||games[i];
    el.addEventListener('click',()=>location.href='/sessions/game?name='+encodeURIComponent(game.game));
  });
  // wire non-empty in order
  let ni=0;
  games.forEach(g=>{
    if(!g.session_count) return;
    const el=document.querySelectorAll('.gc:not(.empty)')[ni++];
    if(el) el.addEventListener('click',()=>location.href='/sessions/game?name='+encodeURIComponent(g.game));
  });
}
init();
</script>
</body>
</html>
"""

TRACKS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SimTelemetry &middot; Tracks</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#000;color:#e0e0e0;font-family:'Courier New',monospace;min-height:100vh}
a{color:inherit;text-decoration:none}
.tb{height:50px;display:flex;align-items:center;padding:0 18px;gap:14px;border-bottom:1px solid #1e1e1e;position:sticky;top:0;background:#000;z-index:10}
.tb h1{font-size:1.3rem;color:#e0e0e0;letter-spacing:3px;text-transform:uppercase;flex:1}
.tb-nav{display:flex;gap:14px}
.tb-nav a{font-size:.8rem;color:#666;letter-spacing:1px;text-transform:uppercase}
.tb-nav a:hover{color:#ccc}
.tb-nav a.cur{color:#e0e0e0;border-bottom:1px solid #888}
.breadcrumb{font-size:.78rem;color:#555;padding:10px 20px;border-bottom:1px solid #111}
.breadcrumb a{color:#444}.breadcrumb a:hover{color:#888}
.page{padding:24px}
.page-hdr{display:flex;align-items:baseline;gap:16px;margin-bottom:24px}
.page-hdr h2{font-size:1.1rem;color:#e0e0e0;letter-spacing:2px;text-transform:uppercase}
.count{font-size:.78rem;color:#555}
.tracks-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px}
.tc{background:#060608;border:1px solid #1a1a1a;padding:18px 20px;cursor:pointer;transition:border-color .15s}
.tc:hover{border-color:#2a2a3a;background:#08080e}
.tc-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}
.tc-name{font-size:1rem;color:#e0e0e0;font-weight:bold;line-height:1.3;flex:1;margin-right:10px}
.trend-up{color:#22c55e;font-size:1.1rem}.trend-dn{color:#ef4444;font-size:1.1rem}.trend-fl{color:#333;font-size:1.1rem}
.tc-stats{display:flex;gap:20px;margin-bottom:10px}
.tc-stat .v{font-size:1.1rem;font-weight:900;color:#fff}
.tc-stat .l{font-size:.68rem;color:#666;text-transform:uppercase;letter-spacing:1px;margin-top:1px}
.tc-tip{font-size:.78rem;color:#22c55e99;line-height:1.4;border-top:1px solid #111;padding-top:8px;margin-top:4px;display:none}
.tc-tip.on{display:block}
.empty-state{color:#333;font-size:.85rem;padding:48px 24px;text-align:center}
</style>
</head>
<body>
<div class="tb">
  <h1>SimTelemetry</h1>
  <nav class="tb-nav">
    <a href="/">Live</a>
    <a href="/sessions" class="cur">Sessions</a>
    <a href="/setup">Setup</a>
    <a href="/admin" id="nav-admin" style="display:none">Admin</a>
  </nav>
</div>
<script>if(location.search.includes('debug=true'))document.getElementById('nav-admin').style.display='';</script>
<div class="breadcrumb"><a href="/sessions">Sessions</a> &rsaquo; <span id="bc-game">Tracks</span></div>
<div class="page">
  <div class="page-hdr">
    <h2 id="page-title">Tracks</h2>
    <span class="count" id="count"></span>
  </div>
  <div class="tracks-grid" id="grid"><div class="empty-state">Loading&hellip;</div></div>
</div>
<script>
const GAME_LABELS={'forza_motorsport':'Forza','acc':'ACC','f1':'F1'};
function fmtLap(s){if(!s)return '—';const m=Math.floor(s/60);return m+':'+(s%60).toFixed(3).padStart(6,'0');}
function fmtDate(iso){if(!iso)return '—';return new Date(iso).toLocaleDateString([],{month:'short',day:'numeric',year:'numeric'});}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
const _game=new URLSearchParams(location.search).get('name')||'';
let _tracks=[];
async function init(){
  const label=GAME_LABELS[_game]||_game||'All';
  document.getElementById('bc-game').textContent=label;
  document.getElementById('page-title').textContent=label+' Tracks';
  document.title='SimTelemetry · '+label;
  const url='/sessions/tracks'+(_game?'?game='+encodeURIComponent(_game):'');
  try{_tracks=await fetch(url).then(r=>r.json());}catch(e){_tracks=[];}
  const grid=document.getElementById('grid');
  document.getElementById('count').textContent=_tracks.length+' track'+(_tracks.length!==1?'s':'');
  if(!_tracks.length){grid.innerHTML='<div class="empty-state">No sessions recorded yet</div>';return;}
  grid.innerHTML=_tracks.map((t,i)=>{
    const arrow=t.trend==='up'?'<span class="trend-up">▲</span>':t.trend==='dn'?'<span class="trend-dn">▼</span>':'<span class="trend-fl">—</span>';
    return `<div class="tc" data-i="${i}">
      <div class="tc-top"><div class="tc-name">${esc(t.track)}</div>${arrow}</div>
      <div class="tc-stats">
        <div class="tc-stat"><div class="v">${fmtLap(t.best_lap_time_s)}</div><div class="l">Best Lap</div></div>
        <div class="tc-stat"><div class="v">${t.session_count}</div><div class="l">Sessions</div></div>
        <div class="tc-stat"><div class="v">${fmtDate(t.last_raced)}</div><div class="l">Last Raced</div></div>
      </div>
      <div class="tc-tip" id="tip${i}"></div>
    </div>`;
  }).join('');
  document.querySelectorAll('.tc').forEach((el,i)=>{
    el.addEventListener('click',()=>{
      let url='/sessions/track?name='+encodeURIComponent(_tracks[i].track);
      if(_game) url+='&game='+encodeURIComponent(_game);
      location.href=url;
    });
    loadTip(_tracks[i].track,i);
  });
}
async function loadTip(track,i){
  try{
    const d=await fetch('/sessions/track/tip?name='+encodeURIComponent(track)).then(r=>r.json());
    if(d&&d.tip){const el=document.getElementById('tip'+i);if(el){el.textContent=d.tip;el.classList.add('on');}}
  }catch(e){}
}
init();
</script>
</body>
</html>
"""

TRACK_DETAIL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SimTelemetry &middot; Track</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#000;color:#e0e0e0;font-family:'Courier New',monospace;min-height:100vh}
a{color:inherit;text-decoration:none}
.tb{height:50px;display:flex;align-items:center;padding:0 18px;gap:14px;border-bottom:1px solid #1e1e1e;position:sticky;top:0;background:#000;z-index:10}
.tb h1{font-size:1.3rem;color:#e0e0e0;letter-spacing:3px;text-transform:uppercase;flex:1}
.tb-nav{display:flex;gap:14px}
.tb-nav a{font-size:.8rem;color:#666;letter-spacing:1px;text-transform:uppercase}
.tb-nav a:hover{color:#ccc}
.tb-nav a.cur{color:#e0e0e0;border-bottom:1px solid #888}
.breadcrumb{font-size:.78rem;color:#555;padding:10px 20px;border-bottom:1px solid #111}
.breadcrumb a{color:#444}.breadcrumb a:hover{color:#888}
.track-hdr{padding:20px 24px;border-bottom:1px solid #111;display:flex;align-items:center;flex-wrap:wrap;gap:24px}
.track-name{font-size:1.4rem;font-weight:900;color:#e0e0e0;letter-spacing:1px;flex:1}
.hdr-stat{text-align:center}
.hdr-stat .v{font-size:1.2rem;font-weight:900;color:#fff}
.hdr-stat .l{font-size:.68rem;color:#666;text-transform:uppercase;letter-spacing:1px}
.track-tip-bar{padding:10px 24px;background:#060809;border-bottom:1px solid #0e0e0e;font-size:.82rem;color:#22c55e99;display:none;align-items:center;gap:12px}
.track-tip-bar.on{display:flex}
.tip-gen{background:none;border:1px solid #1a3a1a;color:#22c55e55;font-family:inherit;font-size:.72rem;padding:3px 10px;cursor:pointer;border-radius:2px;flex-shrink:0}
.tip-gen:hover{border-color:#22c55e44;color:#22c55e88}
.page{padding:20px 24px}
table{width:100%;border-collapse:collapse;font-size:.82rem}
th{color:#666;text-transform:uppercase;letter-spacing:1px;font-weight:normal;padding:6px 10px;text-align:left;border-bottom:1px solid #1a1a1a;white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid #0d0d0d;color:#aaa;vertical-align:middle}
tr.clickable{cursor:pointer}
tr.clickable:hover td{background:#08080e}
td.best-time{color:#22c55e;font-weight:bold}
td.date-col{color:#e0e0e0}
.type-chip{font-size:.65rem;background:#22c55e18;border:1px solid #22c55e44;color:#22c55e;padding:1px 7px;border-radius:10px;letter-spacing:.5px}
.empty-state{color:#333;font-size:.85rem;padding:48px 24px;text-align:center}
</style>
</head>
<body>
<div class="tb">
  <h1>SimTelemetry</h1>
  <nav class="tb-nav">
    <a href="/">Live</a>
    <a href="/sessions" class="cur">Sessions</a>
    <a href="/setup">Setup</a>
    <a href="/admin" id="nav-admin" style="display:none">Admin</a>
  </nav>
</div>
<script>if(location.search.includes('debug=true'))document.getElementById('nav-admin').style.display='';</script>
<div class="breadcrumb"><a href="/sessions">Sessions</a> &rsaquo; <a href="#" id="bc-game"></a> <span id="bc-sep" style="display:none"> &rsaquo; </span><span id="bc-track">Track</span></div>
<div class="track-hdr">
  <div class="track-name" id="hdr-name">Loading&hellip;</div>
  <div class="hdr-stat"><div class="v" id="hdr-best">&mdash;</div><div class="l">Best Lap</div></div>
  <div class="hdr-stat"><div class="v" id="hdr-count">&mdash;</div><div class="l">Sessions</div></div>
  <div class="hdr-stat"><div class="v" id="hdr-trend">&mdash;</div><div class="l">Trend</div></div>
</div>
<div class="track-tip-bar" id="tip-bar">
  <span id="tip-text"></span>
  <button class="tip-gen" id="tip-btn" onclick="generateTip()">Generate AI tip</button>
</div>
<div class="page">
  <table id="sess-table" style="display:none">
    <thead><tr><th>Date</th><th>Type</th><th>Best Lap</th><th>Laps</th><th>Pace</th></tr></thead>
    <tbody id="sess-tbody"></tbody>
  </table>
  <div class="empty-state" id="empty" style="display:none">No sessions at this track</div>
</div>
<script>
const TYPE_LABELS={practice:'Practice',time_trial:'Time Trial',qualifying:'Qualifying',race_ai:'Race vs AI',race_online:'Online Race',hot_lap:'Hot Lap'};
function fmtLap(s){if(!s)return '—';const m=Math.floor(s/60);return m+':'+(s%60).toFixed(3).padStart(6,'0');}
function fmtDt(iso){if(!iso)return '—';return new Date(iso).toLocaleString([],{month:'short',day:'numeric',year:'2-digit',hour:'2-digit',minute:'2-digit'});}
function spark(times){
  const v=(times||[]).filter(t=>t>0);
  if(v.length<2)return '<span style="color:#1a1a1a">—</span>';
  const mn=Math.min(...v),mx=Math.max(...v),W=80,H=26,p=2;
  const xf=i=>p+i/(v.length-1)*(W-p*2);
  const yf=t=>H-p-(mx===mn?(H-p*2)/2:(t-mn)/(mx-mn)*(H-p*2));
  const pts=v.map((t,i)=>xf(i).toFixed(1)+','+yf(t).toFixed(1)).join(' ');
  const best=Math.min(...v);
  const dots=v.map((t,i)=>Math.abs(t-best)<0.001?`<circle cx="${xf(i).toFixed(1)}" cy="${yf(t).toFixed(1)}" r="2" fill="#22c55e"/>`:``).join('');
  return `<svg width="${W}" height="${H}" style="vertical-align:middle"><polyline points="${pts}" fill="none" stroke="#22c55e66" stroke-width="1.5" stroke-linejoin="round"/>${dots}</svg>`;
}
const _track=new URLSearchParams(location.search).get('name')||'';
const _game=new URLSearchParams(location.search).get('game')||'';
const GAME_LABELS={'forza_motorsport':'Forza','acc':'ACC','f1':'F1'};
let _sessions=[];
async function init(){
  if(!_track){location.href='/sessions';return;}
  document.getElementById('bc-track').textContent=_track;
  document.title='SimTelemetry · '+_track;
  const bcGame=document.getElementById('bc-game');
  const bcSep=document.getElementById('bc-sep');
  if(_game&&bcGame){
    bcGame.textContent=GAME_LABELS[_game]||_game;
    bcGame.href='/sessions/game?name='+encodeURIComponent(_game);
    if(bcSep)bcSep.style.display='';
  }
  const dataUrl='/sessions/track/data?name='+encodeURIComponent(_track)+(_game?'&game='+encodeURIComponent(_game):'');
  try{_sessions=await fetch(dataUrl).then(r=>r.json());}catch(e){_sessions=[];}
  renderHeader();
  renderTable();
  loadTip();
}
function renderHeader(){
  document.getElementById('hdr-name').textContent=_track;
  const allBest=_sessions.map(s=>s.best_lap_time_s).filter(v=>v);
  document.getElementById('hdr-best').textContent=allBest.length?fmtLap(Math.min(...allBest)):'—';
  document.getElementById('hdr-count').textContent=_sessions.length;
  const last3=_sessions.slice(0,3).map(s=>s.best_lap_time_s).filter(v=>v);
  let trendHtml='—';
  if(last3.length>=2){
    const d=last3[0]-last3[1];
    trendHtml=d<-0.5?'<span style="color:#22c55e">▲ Improving</span>':d>0.5?'<span style="color:#ef4444">▼ Declining</span>':'<span style="color:#555">Stable</span>';
  }
  document.getElementById('hdr-trend').innerHTML=trendHtml;
}
function renderTable(){
  if(!_sessions.length){document.getElementById('empty').style.display='block';return;}
  document.getElementById('sess-table').style.display='';
  const allBests=_sessions.map(s=>s.best_lap_time_s).filter(v=>v);
  const globalBest=allBests.length?Math.min(...allBests):null;
  document.getElementById('sess-tbody').innerHTML=_sessions.map(s=>{
    const isGB=globalBest&&s.best_lap_time_s&&Math.abs(s.best_lap_time_s-globalBest)<0.001;
    const typeHtml=s.race_type?`<span class="type-chip">${TYPE_LABELS[s.race_type]||s.race_type}</span>`:'';
    return `<tr class="clickable" data-id="${s.session_id}">
      <td class="date-col">${fmtDt(s.started_at)}</td>
      <td>${typeHtml}</td>
      <td class="${isGB?'best-time':''}">${fmtLap(s.best_lap_time_s)}</td>
      <td>${s.lap_count||0}</td>
      <td>${spark(s.lap_times)}</td>
    </tr>`;
  }).join('');
  document.querySelectorAll('#sess-tbody tr').forEach((tr,i)=>{
    tr.addEventListener('click',()=>{
      let u='/sessions/session?id='+encodeURIComponent(_sessions[i].session_id);
      if(_game)u+='&game='+encodeURIComponent(_game);
      if(_track)u+='&track='+encodeURIComponent(_track);
      location.href=u;
    });
  });
}
async function loadTip(){
  try{
    const d=await fetch('/sessions/track/tip?name='+encodeURIComponent(_track)).then(r=>r.json());
    document.getElementById('tip-bar').classList.add('on');
    if(d&&d.tip){
      document.getElementById('tip-text').textContent=d.tip;
      document.getElementById('tip-btn').style.display='none';
    }
  }catch(e){}
}
async function generateTip(){
  const btn=document.getElementById('tip-btn');
  btn.textContent='Generating…';btn.disabled=true;
  try{
    const d=await fetch('/sessions/track/tip?name='+encodeURIComponent(_track)+'&generate=true').then(r=>r.json());
    if(d&&d.tip){document.getElementById('tip-text').textContent=d.tip;btn.style.display='none';}
    else{btn.textContent='Generate AI tip';btn.disabled=false;}
  }catch(e){btn.textContent='Error — retry';btn.disabled=false;}
}
init();
</script>
</body>
</html>
"""

SESSION_DETAIL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SimTelemetry &middot; Session</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#000;color:#e0e0e0;font-family:'Courier New',monospace;min-height:100vh}
a{color:inherit;text-decoration:none}
.tb{height:50px;display:flex;align-items:center;padding:0 18px;gap:14px;border-bottom:1px solid #1e1e1e;position:sticky;top:0;background:#000;z-index:10}
.tb h1{font-size:1.3rem;color:#e0e0e0;letter-spacing:3px;text-transform:uppercase;flex:1}
.tb-nav{display:flex;gap:14px}
.tb-nav a{font-size:.8rem;color:#666;letter-spacing:1px;text-transform:uppercase}
.tb-nav a:hover{color:#ccc}
.tb-nav a.cur{color:#e0e0e0;border-bottom:1px solid #888}
.breadcrumb{font-size:.78rem;color:#555;padding:10px 20px;border-bottom:1px solid #111}
.breadcrumb a{color:#444}.breadcrumb a:hover{color:#888}
.sess-hdr{padding:18px 24px;border-bottom:1px solid #111;display:flex;align-items:center;flex-wrap:wrap;gap:24px}
.sess-title{font-size:1.2rem;font-weight:900;color:#e0e0e0;flex:1}
.sess-sub{font-size:.78rem;color:#555;margin-top:3px}
.hdr-stat .v{font-size:1.1rem;font-weight:900;color:#fff}
.hdr-stat .l{font-size:.68rem;color:#666;text-transform:uppercase;letter-spacing:1px}
.type-chip{font-size:.65rem;background:#22c55e18;border:1px solid #22c55e44;color:#22c55e;padding:1px 8px;border-radius:10px;letter-spacing:.5px}
.section{padding:20px 24px}
.section-lbl{font-size:.72rem;color:#666;text-transform:uppercase;letter-spacing:2px;margin-bottom:12px}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{color:#666;text-transform:uppercase;letter-spacing:1px;font-weight:normal;padding:6px 10px;text-align:right;border-bottom:1px solid #1a1a1a;white-space:nowrap}
th:first-child{text-align:left}
td{padding:7px 10px;border-bottom:1px solid #0d0d0d;color:#aaa;text-align:right}
td:first-child{text-align:left;color:#555}
tr.best-row td{color:#22c55e}
tr.best-row td:first-child{color:#22c55e88}
.warn{color:#f59e0b}.crit{color:#ef4444}
.ai-section{padding:20px 24px;border-top:1px solid #111;max-width:760px}
.ai-lbl{font-size:.72rem;color:#666;text-transform:uppercase;letter-spacing:2px;margin-bottom:14px}
.btn-analyze{background:#22c55e;color:#000;border:none;font-family:inherit;font-size:.85rem;font-weight:bold;padding:10px 24px;border-radius:4px;cursor:pointer;letter-spacing:1px}
.btn-analyze:hover{background:#16a34a}
.btn-analyze:disabled{background:#1a3a1a;color:#2a6a2a;cursor:default}
.btn-re{background:none;border:1px solid #2a2a3a;color:#666;font-family:inherit;font-size:.78rem;padding:8px 16px;border-radius:4px;cursor:pointer;letter-spacing:1px;margin-left:10px}
.btn-re:hover{border-color:#555;color:#ccc}
.btn-re:disabled{opacity:.4;cursor:default}
.ai-meta{font-size:.72rem;color:#555;margin-left:12px}
.ai-body{font-size:.85rem;line-height:1.75;color:#ccc;white-space:pre-wrap;margin-top:18px;padding-top:18px;border-top:1px solid #1a1a28;display:none}
.ai-err{color:#ef4444;font-size:.8rem;margin-top:12px;display:none}
</style>
</head>
<body>
<div class="tb">
  <h1>SimTelemetry</h1>
  <nav class="tb-nav">
    <a href="/">Live</a>
    <a href="/sessions" class="cur">Sessions</a>
    <a href="/setup">Setup</a>
    <a href="/admin" id="nav-admin" style="display:none">Admin</a>
  </nav>
</div>
<script>if(location.search.includes('debug=true'))document.getElementById('nav-admin').style.display='';</script>
<div class="breadcrumb">
  <a href="/sessions">Sessions</a> &rsaquo;
  <a href="#" id="bc-game" style="display:none"></a>
  <span id="bc-game-sep" style="display:none"> &rsaquo; </span>
  <a href="#" id="bc-track">Track</a> &rsaquo;
  <span id="bc-sess">Session</span>
</div>
<div class="sess-hdr">
  <div>
    <div class="sess-title" id="hdr-track">Loading&hellip;</div>
    <div class="sess-sub" id="hdr-sub"></div>
  </div>
  <div class="hdr-stat"><div class="v" id="hdr-best">&mdash;</div><div class="l">Best Lap</div></div>
  <div class="hdr-stat"><div class="v" id="hdr-laps">&mdash;</div><div class="l">Laps</div></div>
  <span class="type-chip" id="hdr-type" style="display:none"></span>
</div>
<div class="section">
  <div class="section-lbl">Lap Times</div>
  <table>
    <thead><tr>
      <th>Lap</th>
      <th>Time</th>
      <th>Max Spd</th>
      <th>Thr%</th>
      <th>Brk%</th>
      <th>Avg Slip</th>
      <th>Peak Slip</th>
      <th>Slip&gt;0.1%</th>
    </tr></thead>
    <tbody id="lap-tbody"></tbody>
  </table>
</div>
<div class="ai-section">
  <div class="ai-lbl">AI Coaching</div>
  <div>
    <button class="btn-analyze" id="btn-analyze" onclick="runAnalysis(false)">Analyze with Claude</button>
    <button class="btn-re" id="btn-re" onclick="runAnalysis(true)" style="display:none">Re-analyze</button>
    <span class="ai-meta" id="ai-meta"></span>
  </div>
  <div class="ai-body" id="ai-body"></div>
  <div class="ai-err" id="ai-err"></div>
</div>
<script>
const TYPE_LABELS={practice:'Practice',time_trial:'Time Trial',qualifying:'Qualifying',race_ai:'Race vs AI',race_online:'Online Race',hot_lap:'Hot Lap'};
function fmtLap(s){if(!s)return '—';const m=Math.floor(s/60);return m+':'+(s%60).toFixed(3).padStart(6,'0');}
function fmtDt(iso){if(!iso)return '—';return new Date(iso).toLocaleString([],{weekday:'short',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});}
function scls(v){return v>0.25?'crit':v>0.12?'warn':'';}
const _id=new URLSearchParams(location.search).get('id')||'';
const _sgame=new URLSearchParams(location.search).get('game')||'';
const _strack=new URLSearchParams(location.search).get('track')||'';
const SGAME_LABELS={'forza_motorsport':'Forza','acc':'ACC','f1':'F1'};
let _sess=null,_laps=[];
async function init(){
  if(!_id){location.href='/sessions';return;}
  let d;
  try{d=await fetch('/sessions/session/data?id='+encodeURIComponent(_id)).then(r=>r.json());}
  catch(e){document.getElementById('hdr-track').textContent='Session not found';return;}
  _sess=d.session;_laps=d.laps||[];
  renderHeader();
  renderLaps();
  renderAI();
}
function renderHeader(){
  const s=_sess;
  const track=s.track&&s.track!=='unknown'?s.track:(_strack||'Unknown Track');
  const game=_sgame||s.game||'';
  document.title='SimTelemetry · '+track;
  if(game){
    const bg=document.getElementById('bc-game');
    bg.textContent=SGAME_LABELS[game]||game;
    bg.href='/sessions/game?name='+encodeURIComponent(game);
    bg.style.display='';
    document.getElementById('bc-game-sep').style.display='';
  }
  let trackHref='/sessions/track?name='+encodeURIComponent(track);
  if(game)trackHref+='&game='+encodeURIComponent(game);
  document.getElementById('bc-track').textContent=track;
  document.getElementById('bc-track').href=trackHref;
  document.getElementById('bc-sess').textContent=fmtDt(s.started_at);
  document.getElementById('hdr-track').textContent=track;
  document.getElementById('hdr-sub').textContent=(s.game||'').replace(/_/g,' ')+' · '+fmtDt(s.started_at);
  document.getElementById('hdr-best').textContent=fmtLap(s.best_lap_time_s);
  document.getElementById('hdr-laps').textContent=_laps.length;
  if(s.race_type){const el=document.getElementById('hdr-type');el.textContent=TYPE_LABELS[s.race_type]||s.race_type;el.style.display='';}
}
function renderLaps(){
  const best=_sess.best_lap_time_s;
  document.getElementById('lap-tbody').innerHTML=_laps.map(l=>{
    const isB=best&&l.lap_time_s&&Math.abs(l.lap_time_s-best)<0.001;
    return `<tr class="${isB?'best-row':''}">
      <td>${l.lap_number}</td>
      <td>${fmtLap(l.lap_time_s)}</td>
      <td>${l.max_speed_mph!=null?l.max_speed_mph.toFixed(1)+' mph':'—'}</td>
      <td>${l.avg_throttle!=null?l.avg_throttle.toFixed(1)+'%':'—'}</td>
      <td>${l.avg_brake!=null?l.avg_brake.toFixed(1)+'%':'—'}</td>
      <td class="${scls(l.avg_slip||0)}">${l.avg_slip!=null?l.avg_slip.toFixed(4):'—'}</td>
      <td class="${scls(l.peak_slip||0)}">${l.peak_slip!=null?l.peak_slip.toFixed(4):'—'}</td>
      <td>${l.slip_above_pct!=null?l.slip_above_pct.toFixed(1)+'%':'—'}</td>
    </tr>`;
  }).join('');
}
function renderAI(){
  if(_sess.ai_analysis){
    document.getElementById('ai-body').textContent=_sess.ai_analysis;
    document.getElementById('ai-body').style.display='block';
    document.getElementById('btn-analyze').style.display='none';
    document.getElementById('btn-re').style.display='inline-block';
    if(_sess.ai_analyzed_at){
      const dt=new Date(_sess.ai_analyzed_at).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
      document.getElementById('ai-meta').textContent='Cached · '+dt+(_sess.ai_model?' · '+_sess.ai_model:'');
    }
  }
}
async function runAnalysis(force){
  const btn=document.getElementById('btn-analyze');
  const rbtn=document.getElementById('btn-re');
  const body=document.getElementById('ai-body');
  const meta=document.getElementById('ai-meta');
  const err=document.getElementById('ai-err');
  err.style.display='none';
  btn.disabled=true;rbtn.disabled=true;
  if(force){rbtn.textContent='Analyzing…';}else{btn.textContent='Analyzing…';}
  try{
    const r=await fetch('/analyze?id='+encodeURIComponent(_id)+(force?'&force=true':''));
    const d=await r.json();
    if(!r.ok)throw new Error(d.error||'Unknown error');
    body.textContent=d.analysis;body.style.display='block';
    btn.style.display='none';rbtn.style.display='inline-block';rbtn.textContent='Re-analyze';rbtn.disabled=false;
    if(d.analyzed_at){
      const dt=new Date(d.analyzed_at).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
      meta.textContent='Analyzed '+dt+(d.model?' · '+d.model:'');
    }
  }catch(e){
    err.textContent='✗ '+e.message;err.style.display='block';
    btn.disabled=false;btn.textContent='Analyze with Claude';
    rbtn.disabled=false;if(!_sess.ai_analysis)rbtn.style.display='none';
  }
}
init();
</script>
</body>
</html>
"""


# ─── SQLite Layer ─────────────────────────────────────────────────────────────

_db_lock = threading.Lock()

def _db_connect() -> sqlite3.Connection:
    db_path = storage_path() / "simtelemetry.db"
    conn = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def _db_init():
    with _db_lock:
        conn = _db_connect()
        try:
            conn.executescript("""
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
                    ai_model         TEXT
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
                CREATE INDEX IF NOT EXISTS idx_laps_session  ON laps(session_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_track ON sessions(track);
                CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(started_at);
            """)
            conn.commit()
        finally:
            conn.close()
    _db_migrate()

def _db_migrate():
    """Import existing session JSON files not yet in the database. Idempotent."""
    sessions_dir = storage_path() / "sessions"
    if not sessions_dir.exists():
        return
    imported = 0
    with _db_lock:
        conn = _db_connect()
        try:
            for f in sorted(sessions_dir.glob("*.json")):
                if f.name.endswith("_laps.json") or f.name.endswith("_analysis.json"):
                    continue
                try:
                    data = json.loads(f.read_text())
                    sid = data.get("session_id")
                    if not sid:
                        continue
                    if conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (sid,)).fetchone():
                        continue
                    ai_text = ai_at = ai_model = None
                    af = sessions_dir / f"{sid}_analysis.json"
                    if af.exists():
                        try:
                            a = json.loads(af.read_text())
                            ai_text  = a.get("analysis")
                            ai_at    = a.get("analyzed_at")
                            ai_model = a.get("model")
                        except Exception:
                            pass
                    laps = data.get("laps", [])
                    conn.execute("""
                        INSERT OR IGNORE INTO sessions
                        (session_id,game,track,car,session_type,race_type,
                         started_at,ended_at,packet_count,best_lap_time_s,lap_count,
                         ai_analysis,ai_analyzed_at,ai_model)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (sid,
                          data.get("game"), data.get("track"), data.get("car"),
                          data.get("session_type"), data.get("race_type"),
                          data.get("started_at"), data.get("ended_at"),
                          data.get("packet_count", 0), data.get("best_lap_time_s"),
                          len(laps), ai_text, ai_at, ai_model))
                    for lap in laps:
                        conn.execute("""
                            INSERT INTO laps (session_id,lap_number,lap_time_s,max_speed_mph,sample_count)
                            VALUES (?,?,?,?,?)
                        """, (sid, lap.get("lap_number"), lap.get("lap_time_s"),
                              lap.get("max_speed_mph"), lap.get("sample_count", 0)))
                    imported += 1
                except Exception as e:
                    log.warning(f"DB migration: skipping {f.name}: {e}")
            conn.commit()
        finally:
            conn.close()
    if imported:
        log.info(f"SQLite: migrated {imported} session(s) from JSON files")

def _db_write_session(session_data: dict):
    """Insert/replace a session and its lap summaries."""
    sid  = session_data["session_id"]
    laps = session_data.get("laps", [])
    with _db_lock:
        conn = _db_connect()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO sessions
                (session_id,game,track,car,session_type,race_type,
                 started_at,ended_at,packet_count,best_lap_time_s,lap_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (sid,
                  session_data.get("game"), session_data.get("track"),
                  session_data.get("car"), session_data.get("session_type"),
                  session_data.get("race_type"),
                  session_data.get("started_at"), session_data.get("ended_at"),
                  session_data.get("packet_count", 0), session_data.get("best_lap_time_s"),
                  len(laps)))
            conn.execute("DELETE FROM laps WHERE session_id=?", (sid,))
            for lap in laps:
                conn.execute("""
                    INSERT INTO laps (session_id,lap_number,lap_time_s,max_speed_mph,sample_count)
                    VALUES (?,?,?,?,?)
                """, (sid, lap.get("lap_number"), lap.get("lap_time_s"),
                      lap.get("max_speed_mph"), lap.get("sample_count", 0)))
            conn.commit()
        finally:
            conn.close()

def _db_sessions_list(limit: int = 100) -> list:
    """Return sessions newest-first — summary stats only, no sample data."""
    with _db_lock:
        conn = _db_connect()
        try:
            rows = conn.execute(
                "SELECT session_id,game,track,car,session_type,race_type,"
                "started_at,ended_at,packet_count,best_lap_time_s,lap_count "
                "FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

def _db_games_index() -> list:
    """Return per-game aggregate stats, newest-first."""
    all_games = ["forza_motorsport", "acc", "f1"]
    with _db_lock:
        conn = _db_connect()
        try:
            rows = conn.execute("""
                SELECT game,
                       COUNT(*) as session_count,
                       COUNT(DISTINCT CASE WHEN track IS NOT NULL AND track != 'unknown'
                                           THEN track END) as track_count,
                       MAX(started_at) as last_played
                FROM sessions
                GROUP BY game
            """).fetchall()
            by_game = {r["game"]: dict(r) for r in rows}
            result = []
            for g in all_games:
                r = by_game.get(g, {"game": g, "session_count": 0, "track_count": 0, "last_played": None})
                result.append(r)
            # sort present-first, then by last_played
            result.sort(key=lambda x: (x["last_played"] is None, x["last_played"] or ""), reverse=False)
            result.sort(key=lambda x: x["last_played"] is None)
            return result
        finally:
            conn.close()

def _db_tracks_index(game: Optional[str] = None) -> list:
    """Return aggregate stats per track, newest-first. Optionally filter by game."""
    with _db_lock:
        conn = _db_connect()
        try:
            where = "WHERE track IS NOT NULL AND track != 'unknown'"
            params: list = []
            if game:
                where += " AND game=?"
                params.append(game)
            rows = conn.execute(f"""
                SELECT track,
                       COUNT(*) as session_count,
                       MIN(best_lap_time_s) as best_lap_time_s,
                       MAX(started_at) as last_raced
                FROM sessions {where}
                GROUP BY track
                ORDER BY last_raced DESC
            """, params).fetchall()
            result = []
            for row in rows:
                r = dict(row)
                trend_params = [r["track"]]
                trend_extra = ""
                if game:
                    trend_extra = " AND game=?"
                    trend_params.append(game)
                last3 = conn.execute(f"""
                    SELECT best_lap_time_s FROM sessions
                    WHERE track=? AND best_lap_time_s IS NOT NULL{trend_extra}
                    ORDER BY started_at DESC LIMIT 3
                """, trend_params).fetchall()
                times = [l[0] for l in last3]
                if len(times) >= 2 and times[0] is not None and times[1] is not None:
                    diff = times[0] - times[1]
                    r["trend"] = "dn" if diff > 0.5 else ("up" if diff < -0.5 else "fl")
                else:
                    r["trend"] = "fl"
                result.append(r)
            return result
        finally:
            conn.close()

def _db_track_sessions(track: str, game: Optional[str] = None) -> list:
    """Return all sessions for a track, newest-first, with lap time arrays for spark graphs."""
    with _db_lock:
        conn = _db_connect()
        try:
            where = "WHERE track=?"
            params: list = [track]
            if game:
                where += " AND game=?"
                params.append(game)
            rows = conn.execute(f"""
                SELECT session_id,game,track,car,race_type,
                       started_at,ended_at,best_lap_time_s,lap_count,
                       ai_analyzed_at,ai_model
                FROM sessions {where} ORDER BY started_at DESC
            """, params).fetchall()
            result = []
            for row in rows:
                s = dict(row)
                lap_rows = conn.execute(
                    "SELECT lap_time_s FROM laps WHERE session_id=? ORDER BY lap_number",
                    (s["session_id"],)
                ).fetchall()
                s["lap_times"] = [l[0] for l in lap_rows if l[0] is not None]
                result.append(s)
            return result
        finally:
            conn.close()

def _db_get_track_tip(track: str) -> Optional[dict]:
    """Return cached coaching tip for a track, or None."""
    with _db_lock:
        conn = _db_connect()
        try:
            row = conn.execute(
                "SELECT tip,generated_at,model FROM track_tips WHERE track=?", (track,)
            ).fetchone()
            return dict(row) if row and row["tip"] else None
        finally:
            conn.close()

def _db_save_track_tip(track: str, tip: str, model: str):
    with _db_lock:
        conn = _db_connect()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO track_tips (track,tip,generated_at,model)
                VALUES (?,?,?,?)
            """, (track, tip, datetime.now().isoformat(), model))
            conn.commit()
        finally:
            conn.close()

def _db_update_session(sid: str, **kwargs):
    """Update arbitrary columns on a session row."""
    if not kwargs:
        return
    cols = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [sid]
    with _db_lock:
        conn = _db_connect()
        try:
            conn.execute(f"UPDATE sessions SET {cols} WHERE session_id=?", vals)
            conn.commit()
        finally:
            conn.close()

def _db_drop_last_lap(sid: str):
    """Remove the last lap row and recalculate best_lap_time_s."""
    with _db_lock:
        conn = _db_connect()
        try:
            last = conn.execute(
                "SELECT id FROM laps WHERE session_id=? ORDER BY lap_number DESC LIMIT 1",
                (sid,)
            ).fetchone()
            if last:
                conn.execute("DELETE FROM laps WHERE id=?", (last["id"],))
                best = conn.execute(
                    "SELECT MIN(lap_time_s) FROM laps WHERE session_id=? AND lap_time_s IS NOT NULL",
                    (sid,)
                ).fetchone()[0]
                count = conn.execute(
                    "SELECT COUNT(*) FROM laps WHERE session_id=?", (sid,)
                ).fetchone()[0]
                conn.execute(
                    "UPDATE sessions SET best_lap_time_s=?, lap_count=? WHERE session_id=?",
                    (best, count, sid)
                )
                conn.commit()
        finally:
            conn.close()

def _db_get_ai_analysis(sid: str) -> Optional[dict]:
    """Return cached AI analysis from DB, or None if not yet analyzed."""
    with _db_lock:
        conn = _db_connect()
        try:
            row = conn.execute(
                "SELECT ai_analysis,ai_analyzed_at,ai_model FROM sessions WHERE session_id=?",
                (sid,)
            ).fetchone()
            if row and row["ai_analysis"]:
                return {
                    "session_id":  sid,
                    "analysis":    row["ai_analysis"],
                    "analyzed_at": row["ai_analyzed_at"],
                    "model":       row["ai_model"],
                    "cached":      True,
                }
            return None
        finally:
            conn.close()

def _db_save_ai_analysis(sid: str, analysis: str, model: str):
    """Persist AI analysis text to the sessions row."""
    _db_update_session(sid,
                       ai_analysis=analysis,
                       ai_analyzed_at=datetime.now().isoformat(),
                       ai_model=model)

# ─── AI Analysis ──────────────────────────────────────────────────────────────

import statistics as _statistics
import urllib.request as _urllib_req

def _summarize_lap(lap: dict) -> Optional[dict]:
    samples = lap.get("samples", [])
    if not samples or not lap.get("lap_time_s"):
        return None
    throttle = [s.get("throttle_pct", 0) for s in samples]
    brake    = [s.get("brake_pct", 0)    for s in samples]
    g_lat    = [abs(s.get("g_lat", 0))   for s in samples]
    slip_rl  = [abs(s.get("slip_rl", 0)) for s in samples]
    slip_rr  = [abs(s.get("slip_rr", 0)) for s in samples]
    slip_avg = [(a + b) / 2 for a, b in zip(slip_rl, slip_rr)]
    n = len(samples)
    return {
        "lap_number":      lap["lap_number"],
        "lap_time_s":      lap["lap_time_s"],
        "max_speed_mph":   lap.get("max_speed_mph", 0),
        "avg_throttle":    round(sum(throttle) / n, 1),
        "avg_brake":       round(sum(brake)    / n, 1),
        "avg_g_lat":       round(sum(g_lat)    / n, 3),
        "avg_slip":        round(sum(slip_avg) / n, 4),
        "peak_slip":       round(max(slip_avg),     4) if slip_avg else 0,
        "slip_above_pct":  round(sum(1 for v in slip_avg if v > 0.1) / n * 100, 1),
    }


def _build_analysis_prompt(session: dict, laps: list, historical: list,
                           prev_analyses: Optional[list] = None) -> str:
    game  = session.get("game", "unknown").replace("_", " ").title()
    track = session.get("track", "unknown")
    date  = (session.get("started_at") or "")[:10]

    summaries = [s for lap in laps if (s := _summarize_lap(lap))]
    valid_times = [s["lap_time_s"] for s in summaries]
    best_time = min(valid_times) if valid_times else None
    avg_time  = sum(valid_times) / len(valid_times) if valid_times else None

    hdr = "Lap | Time      | Throttle% | Brake% | MaxSpd | AvgSlip | PeakSlip | Slip>0.1%\n"
    hdr += "-" * 80 + "\n"
    rows = ""
    for s in summaries:
        marker = " ◄" if best_time and abs(s["lap_time_s"] - best_time) < 0.001 else ""
        rows += (
            f"{s['lap_number']:<3} | {s['lap_time_s']:.3f}s   | "
            f"{s['avg_throttle']:.0f}%        | {s['avg_brake']:.0f}%     | "
            f"{s['max_speed_mph']:.0f}mph  | {s['avg_slip']:.4f}  | "
            f"{s['peak_slip']:.4f}   | {s['slip_above_pct']:.1f}%{marker}\n"
        )

    # Historical baseline — last 3 sessions at same track (from DB summaries)
    hist_block = ""
    if historical:
        hist_sessions = historical[-3:]
        h_best_times  = [h.get("best_lap_time_s") for h in hist_sessions if h.get("best_lap_time_s")]
        h_avg_slip_vals = []
        sessions_dir = storage_path() / "sessions"
        for h in hist_sessions:
            try:
                hlaps = json.loads((sessions_dir / f"{h['session_id']}_laps.json").read_text())
                hsums = [s for lap in hlaps if (s := _summarize_lap(lap))]
                if hsums:
                    h_avg_slip_vals.append(sum(s["avg_slip"] for s in hsums) / len(hsums))
            except Exception:
                pass
        h_best_str = f"{min(h_best_times):.3f}s" if h_best_times else "—"
        h_avg_str  = f"{sum(valid_times)/len(valid_times):.3f}s" if valid_times else "—"
        h_slip_str = f"{sum(h_avg_slip_vals)/len(h_avg_slip_vals):.4f}" if h_avg_slip_vals else "—"
        hist_block = (
            f"\nHISTORICAL BASELINE (last {len(hist_sessions)} sessions at this track):\n"
            f"Best lap: {h_best_str} | Avg lap: {h_avg_str} | Avg slip: {h_slip_str}\n"
        )

    return (
        f"Track: {track} | Game: {game} | Session: {date}\n\n"
        f"THIS SESSION — LAP TABLE:\n{hdr}{rows}\n"
        f"{hist_block}\n"
        "Analyze this session. Focus on slip management, throttle discipline, "
        "brake consistency, and lap time trend. Reference specific laps. "
        "Compare against historical baseline where relevant. Be direct, no padding."
    )


def _build_track_tip_prompt(track: str, stats: dict) -> str:
    best = f"{stats['best_lap_time_s']:.3f}s" if stats.get("best_lap_time_s") else "unknown"
    return (
        f"Track: {track} | Sessions: {stats.get('session_count',0)} | Best lap: {best} | Trend: {stats.get('trend','fl')}\n\n"
        "Write exactly one coaching focus sentence (max 20 words) for this sim racing driver at this track. "
        "Be specific to the track's characteristics if you know it. No intro, no padding, just the sentence."
    )

def _call_claude_api(prompt: str) -> str:
    api_key = config.get("anthropic_api_key", "").strip()
    model   = config.get("anthropic_model", "claude-sonnet-4-6").strip()
    if not api_key:
        raise ValueError("Anthropic API key not set — add it in Setup → AI Analysis")
    payload = json.dumps({
        "model": model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = _urllib_req.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload, method="POST",
    )
    req.add_header("x-api-key", api_key)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    with _urllib_req.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read())
    return data["content"][0]["text"]


def _http_response(status: str, content_type: str, body: bytes, extra_headers: str = "") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Access-Control-Allow-Origin: *\r\n"
        f"Access-Control-Allow-Private-Network: true\r\n"
        f"Connection: close\r\n"
        f"{extra_headers}\r\n"
    ).encode() + body


async def handle_status(reader, writer):
    try:
        # Read until the end of HTTP headers
        header_buf = b""
        while b"\r\n\r\n" not in header_buf:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
            if not chunk:
                break
            header_buf += chunk

        header_bytes, _, body_so_far = header_buf.partition(b"\r\n\r\n")
        header_str = header_bytes.decode("utf-8", errors="ignore")
        header_lines = header_str.split("\r\n")
        request_line = header_lines[0] if header_lines else ""
        parts        = request_line.split(" ")
        method       = parts[0] if parts else "GET"
        raw_url      = parts[1] if len(parts) > 1 else "/"
        path         = raw_url.split("?")[0]
        query_string = raw_url.split("?", 1)[1] if "?" in raw_url else ""

        # Parse Content-Length so we read the full POST body
        content_length = 0
        for line in header_lines[1:]:
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())
                break

        body_buf = body_so_far
        while len(body_buf) < content_length:
            chunk = await asyncio.wait_for(reader.read(content_length - len(body_buf)), timeout=5)
            if not chunk:
                break
            body_buf += chunk

        raw_body = body_buf.decode("utf-8", errors="ignore")

        if path in ("/", "/dashboard"):
            writer.write(_http_response("200 OK", "text/html", DASHBOARD_HTML.encode()))

        elif path == "/setup":
            writer.write(_http_response("200 OK", "text/html", SETUP_HTML.encode()))

        elif path == "/config" and method == "GET":
            payload = {**config, "disk": disk_info()}
            writer.write(_http_response("200 OK", "application/json", json.dumps(payload, indent=2).encode()))

        elif path == "/config" and method == "POST":
            try:
                incoming = json.loads(raw_body)
            except (json.JSONDecodeError, ValueError) as exc:
                err = json.dumps({"error": f"Invalid JSON: {exc}"}).encode()
                writer.write(_http_response("400 Bad Request", "application/json", err))
            else:
                new_path = str(incoming.get("storage_path", config["storage_path"])).strip()
                # Validate: try to create subdirs
                try:
                    test = Path(new_path)
                    for sub in ["raw", "sessions", "logs"]:
                        (test / sub).mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    err = json.dumps({"error": f"Cannot create storage path: {exc}"}).encode()
                    writer.write(_http_response("400 Bad Request", "application/json", err))
                else:
                    config["storage_path"]      = new_path
                    config["session_timeout_s"] = int(incoming.get("session_timeout_s", config["session_timeout_s"]))
                    if "ports" in incoming:
                        config["ports"].update({
                            k: int(v) for k, v in incoming["ports"].items()
                            if k in config["ports"]
                        })
                    if "anthropic_api_key" in incoming:
                        config["anthropic_api_key"] = str(incoming["anthropic_api_key"]).strip()
                    if "anthropic_model" in incoming:
                        config["anthropic_model"] = str(incoming["anthropic_model"]).strip()
                    save_config(config)
                    msg = "Saved."
                    if incoming.get("ports") and incoming["ports"] != PORTS:
                        msg += " Restart required for port changes to take effect."
                    result = json.dumps({"ok": True, "message": msg, "disk": disk_info()}).encode()
                    writer.write(_http_response("200 OK", "application/json", result))

        elif path == "/status":
            writer.write(_http_response("200 OK", "application/json", json.dumps(state, indent=2).encode()))

        elif path in ("/sessions", "/sessions/"):
            writer.write(_http_response("200 OK", "text/html", GAMES_HTML.encode()))

        elif path == "/sessions/game":
            writer.write(_http_response("200 OK", "text/html", TRACKS_HTML.encode()))

        elif path == "/sessions/track":
            writer.write(_http_response("200 OK", "text/html", TRACK_DETAIL_HTML.encode()))

        elif path == "/sessions/session":
            writer.write(_http_response("200 OK", "text/html", SESSION_DETAIL_HTML.encode()))

        elif path == "/sessions/data":
            result = _db_sessions_list(100)
            writer.write(_http_response("200 OK", "application/json", json.dumps(result).encode()))

        elif path == "/sessions/games":
            result = _db_games_index()
            writer.write(_http_response("200 OK", "application/json", json.dumps(result).encode()))

        elif path == "/sessions/tracks":
            qs = {k: urllib.parse.unquote_plus(v)
                  for pair in query_string.split("&") if "=" in pair
                  for k, v in [pair.split("=", 1)]}
            game_filter = qs.get("game", "") or None
            result = _db_tracks_index(game_filter)
            writer.write(_http_response("200 OK", "application/json", json.dumps(result).encode()))

        elif path == "/sessions/track/data":
            qs = {k: urllib.parse.unquote_plus(v)
                  for pair in query_string.split("&") if "=" in pair
                  for k, v in [pair.split("=", 1)]}
            track_name = qs.get("name", "")
            game_filter = qs.get("game", "") or None
            result = _db_track_sessions(track_name, game_filter)
            writer.write(_http_response("200 OK", "application/json", json.dumps(result).encode()))

        elif path == "/sessions/track/tip":
            qs = {k: urllib.parse.unquote_plus(v)
                  for pair in query_string.split("&") if "=" in pair
                  for k, v in [pair.split("=", 1)]}
            track_name = qs.get("name", "")
            generate   = qs.get("generate", "") == "true"
            cached = _db_get_track_tip(track_name)
            if cached:
                writer.write(_http_response("200 OK", "application/json",
                                            json.dumps(cached).encode()))
            elif generate and config.get("anthropic_api_key", "").strip():
                try:
                    stats = next((t for t in _db_tracks_index() if t["track"] == track_name), {})
                    tip_prompt = _build_track_tip_prompt(track_name, stats)
                    tip_text   = await asyncio.to_thread(_call_claude_api, tip_prompt)
                    tip_text   = tip_text.strip().split("\n")[0][:200]
                    model_name = config.get("anthropic_model", "claude-sonnet-4-6")
                    _db_save_track_tip(track_name, tip_text, model_name)
                    writer.write(_http_response("200 OK", "application/json",
                                                json.dumps({"tip": tip_text, "generated_at": datetime.now().isoformat(), "model": model_name}).encode()))
                except Exception as exc:
                    log.error(f"Track tip generation error: {exc}")
                    writer.write(_http_response("200 OK", "application/json", b'{"tip":null}'))
            else:
                writer.write(_http_response("200 OK", "application/json", b'{"tip":null}'))

        elif path == "/sessions/session/data":
            qs = {k: urllib.parse.unquote_plus(v)
                  for pair in query_string.split("&") if "=" in pair
                  for k, v in [pair.split("=", 1)]}
            sid = qs.get("id", "")
            with _db_lock:
                conn = _db_connect()
                try:
                    sess_row = conn.execute(
                        "SELECT session_id,game,track,car,race_type,started_at,ended_at,"
                        "best_lap_time_s,lap_count,ai_analysis,ai_analyzed_at,ai_model "
                        "FROM sessions WHERE session_id=?", (sid,)
                    ).fetchone()
                finally:
                    conn.close()
            if not sess_row:
                writer.write(_http_response("404 Not Found", "application/json",
                                            json.dumps({"error": "Session not found"}).encode()))
            else:
                sess_dict = dict(sess_row)
                laps_file = storage_path() / "sessions" / f"{sid}_laps.json"
                try:
                    raw_laps = json.loads(laps_file.read_text())
                except OSError:
                    raw_laps = []
                computed_laps = []
                for lap in raw_laps:
                    samples  = lap.get("samples", [])
                    n        = len(samples)
                    lap_time = lap.get("lap_time_s")
                    row = {
                        "lap_number":    lap.get("lap_number"),
                        "lap_time_s":    lap_time,
                        "max_speed_mph": lap.get("max_speed_mph"),
                    }
                    if n:
                        throttle   = [s.get("throttle_pct", 0) for s in samples]
                        brake      = [s.get("brake_pct", 0)    for s in samples]
                        slip_vals  = [(abs(s.get("slip_rl", 0)) + abs(s.get("slip_rr", 0))) / 2
                                      for s in samples]
                        row["avg_throttle"]   = round(sum(throttle) / n, 1)
                        row["avg_brake"]      = round(sum(brake)    / n, 1)
                        row["avg_slip"]       = round(sum(slip_vals) / n, 4)
                        row["peak_slip"]      = round(max(slip_vals), 4)
                        row["slip_above_pct"] = round(
                            sum(1 for v in slip_vals if v > 0.1) / n * 100, 1)
                    else:
                        row["avg_throttle"] = row["avg_brake"] = None
                        row["avg_slip"] = row["peak_slip"] = row["slip_above_pct"] = None
                    computed_laps.append(row)
                writer.write(_http_response("200 OK", "application/json",
                                            json.dumps({"session": sess_dict, "laps": computed_laps}).encode()))

        elif path == "/sessions/laps":
            qs = {k: urllib.parse.unquote_plus(v)
                  for pair in query_string.split("&") if "=" in pair
                  for k, v in [pair.split("=", 1)]}
            sid = qs.get("id", "")
            laps_file = storage_path() / "sessions" / f"{sid}_laps.json"
            try:
                writer.write(_http_response("200 OK", "application/json", laps_file.read_bytes()))
            except OSError:
                writer.write(_http_response("404 Not Found", "application/json", b"[]"))

        elif path == "/sessions/update" and method == "POST":
            try:
                body_data = json.loads(raw_body)
            except (json.JSONDecodeError, ValueError) as exc:
                writer.write(_http_response("400 Bad Request", "application/json",
                                            json.dumps({"error": str(exc)}).encode()))
            else:
                sid = body_data.get("id", "")
                sessions_dir = storage_path() / "sessions"
                session_file = sessions_dir / f"{sid}.json"
                laps_file    = sessions_dir / f"{sid}_laps.json"
                try:
                    session_data = json.loads(session_file.read_text())
                except OSError:
                    writer.write(_http_response("404 Not Found", "application/json",
                                                json.dumps({"error": "Session not found"}).encode()))
                else:
                    # Update race_type
                    if "race_type" in body_data:
                        session_data["race_type"] = body_data["race_type"]

                    # Drop last lap
                    if body_data.get("drop_last_lap") and session_data.get("laps"):
                        session_data["laps"] = session_data["laps"][:-1]
                        # Recalculate best
                        valid = [l["lap_time_s"] for l in session_data["laps"] if l.get("lap_time_s")]
                        session_data["best_lap_time_s"] = round(min(valid), 3) if valid else None
                        # Drop from laps detail file too
                        try:
                            laps_detail = json.loads(laps_file.read_text())
                            if laps_detail:
                                laps_detail = laps_detail[:-1]
                                laps_file.write_text(json.dumps(laps_detail, indent=2))
                        except OSError:
                            pass

                    session_file.write_text(json.dumps(session_data, indent=2))

                    # Sync to SQLite
                    db_kwargs = {}
                    if "race_type" in body_data:
                        db_kwargs["race_type"] = body_data["race_type"]
                    if db_kwargs:
                        _db_update_session(sid, **db_kwargs)
                    if body_data.get("drop_last_lap"):
                        _db_drop_last_lap(sid)

                    writer.write(_http_response("200 OK", "application/json",
                                                json.dumps({"ok": True, "session": session_data}).encode()))

        elif path == "/analyze":
            qs = {k: urllib.parse.unquote_plus(v)
                  for pair in query_string.split("&") if "=" in pair
                  for k, v in [pair.split("=", 1)]}
            sid   = qs.get("id", "")
            force = qs.get("force", "") == "true"
            sessions_dir  = storage_path() / "sessions"
            analysis_file = sessions_dir / f"{sid}_analysis.json"

            # Serve cached result unless caller requests a fresh one
            db_cached = _db_get_ai_analysis(sid)
            if not force and db_cached:
                writer.write(_http_response("200 OK", "application/json",
                                            json.dumps(db_cached).encode()))
            elif not force and analysis_file.exists():
                writer.write(_http_response("200 OK", "application/json", analysis_file.read_bytes()))
            else:
                # Get session from DB; fall back to JSON file
                with _db_lock:
                    conn = _db_connect()
                    try:
                        sess_row = conn.execute(
                            "SELECT * FROM sessions WHERE session_id=?", (sid,)
                        ).fetchone()
                    finally:
                        conn.close()
                session_data = dict(sess_row) if sess_row else None
                if not session_data:
                    try:
                        session_data = json.loads((sessions_dir / f"{sid}.json").read_text())
                    except OSError:
                        session_data = None
                try:
                    laps_data = json.loads((sessions_dir / f"{sid}_laps.json").read_text())
                except OSError:
                    laps_data = []
                if not session_data:
                    writer.write(_http_response("404 Not Found", "application/json",
                                                json.dumps({"error": "Session not found"}).encode()))
                else:
                    track = session_data.get("track", "unknown")
                    # Pull last 3 historical sessions at same track from DB
                    historical = []
                    if track and track != "unknown":
                        hist_rows = _db_track_sessions(track)
                        historical = [h for h in hist_rows if h["session_id"] != sid][:3]
                    try:
                        prompt   = _build_analysis_prompt(session_data, laps_data, historical)
                        analysis = await asyncio.to_thread(_call_claude_api, prompt)
                        result_obj = {
                            "session_id":  sid,
                            "analyzed_at": datetime.now().isoformat(),
                            "model":       config.get("anthropic_model", "claude-sonnet-4-6"),
                            "cached":      False,
                            "analysis":    analysis,
                        }
                        analysis_file.write_text(json.dumps(result_obj, indent=2))
                        _db_save_ai_analysis(sid, analysis,
                                             config.get("anthropic_model", "claude-sonnet-4-6"))
                        writer.write(_http_response("200 OK", "application/json",
                                                    json.dumps(result_obj).encode()))
                    except ValueError as exc:
                        writer.write(_http_response("400 Bad Request", "application/json",
                                                    json.dumps({"error": str(exc)}).encode()))
                    except Exception as exc:
                        log.error(f"Claude API error: {exc}")
                        writer.write(_http_response("502 Bad Gateway", "application/json",
                                                    json.dumps({"error": f"API error: {exc}"}).encode()))

        elif path == "/reset" and method == "POST":
            for game in PORTS:
                state["udp_received"][game] = 0
                state["udp_rejected"][game] = 0
                state["last_rejected_size"][game] = None
            writer.write(_http_response("200 OK", "application/json", b'{"ok":true}'))

        elif path == "/finish" and method == "POST":
            closed = []
            for game, session in list(active_sessions.items()):
                session.close()
                active_sessions.pop(game)
                closed.append(session.session_id)
            if closed:
                state["status"] = "race_ended"
                state["game"] = None
                asyncio.create_task(_clear_race_ended())
            writer.write(_http_response("200 OK", "application/json",
                                        json.dumps({"ok": True, "closed": closed}).encode()))

        elif path == "/admin" and method == "GET":
            writer.write(_http_response("200 OK", "text/html", ADMIN_HTML.encode()))

        elif path == "/admin/inject" and method == "POST":
            try:
                p = json.loads(raw_body)
            except (json.JSONDecodeError, ValueError) as exc:
                err = json.dumps({"error": f"Invalid JSON: {exc}"}).encode()
                writer.write(_http_response("400 Bad Request", "application/json", err))
            else:
                game = p.get("game", "forza_motorsport")
                if game not in PORTS:
                    err = json.dumps({"error": f"Unknown game: {game}"}).encode()
                    writer.write(_http_response("400 Bad Request", "application/json", err))
                else:
                    try:
                        packets = _build_inject_packets(game, p)
                        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        for pkt in packets:
                            sock.sendto(pkt, ("127.0.0.1", PORTS[game]))
                        sock.close()
                        result = json.dumps({"ok": True, "sent": len(packets)}).encode()
                        writer.write(_http_response("200 OK", "application/json", result))
                    except Exception as exc:
                        err = json.dumps({"error": str(exc)}).encode()
                        writer.write(_http_response("500 Internal Server Error", "application/json", err))

        elif path == "/browse":
            qs = {k: urllib.parse.unquote_plus(v)
                  for pair in query_string.split("&") if "=" in pair
                  for k, v in [pair.split("=", 1)]}
            browse_path = qs.get("path", "/") or "/"
            try:
                p = Path(browse_path)
                exists = p.is_dir()
                entries = []
                if exists:
                    try:
                        for item in sorted(p.iterdir()):
                            if item.is_dir() and not item.name.startswith("."):
                                entries.append({"name": item.name})
                    except PermissionError:
                        pass
                result = {
                    "path":          str(p.resolve()) if exists else str(p),
                    "parent":        str(p.parent),
                    "exists":        exists,
                    "parent_exists": p.parent.is_dir(),
                    "entries":       entries,
                }
            except Exception as exc:
                result = {
                    "path": browse_path, "parent": None,
                    "exists": False, "parent_exists": False,
                    "entries": [], "error": str(exc),
                }
            writer.write(_http_response("200 OK", "application/json", json.dumps(result).encode()))

        elif path == "/debug-stream":
            q: asyncio.Queue = asyncio.Queue(maxsize=2000)
            _debug_clients.append(q)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Access-Control-Allow-Origin: *\r\n"
                b"Connection: keep-alive\r\n\r\n"
            )
            for line in list(_debug_buffer):
                writer.write(f"data: {json.dumps(line)}\n\n".encode())
            await writer.drain()
            try:
                while True:
                    line = await q.get()
                    writer.write(f"data: {json.dumps(line)}\n\n".encode())
                    await writer.drain()
            finally:
                if q in _debug_clients:
                    _debug_clients.remove(q)

        elif path == "/stream":
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Access-Control-Allow-Origin: *\r\n"
                b"Connection: keep-alive\r\n\r\n"
            )
            while True:
                data = f"data: {json.dumps(state)}\n\n"
                writer.write(data.encode())
                await writer.drain()
                await asyncio.sleep(0.1)  # 10 Hz

        elif path == "/health":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")

        else:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\n\r\nNot Found")

        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()

# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    ensure_storage()
    _db_init()
    log.info("SimTelemetry listener starting...")

    loop = asyncio.get_event_loop()

    parsers = {
        "forza_motorsport": parse_forza,
        "acc":              parse_acc,
        "f1":               parse_f1,
    }

    for game, port in PORTS.items():
        try:
            await loop.create_datagram_endpoint(
                lambda g=game, p=parsers[game]: TelemetryProtocol(g, p),
                local_addr=("0.0.0.0", port),
            )
            log.info(f"Listening for {game} on UDP port {port}")
        except OSError as e:
            log.error(f"Failed to bind {game} on port {port}: {e}")

    asyncio.create_task(session_watchdog())

    server = await asyncio.start_server(handle_status, "0.0.0.0", STATUS_PORT)
    log.info(f"Storage path: {storage_path()}")
    log.info(f"Dashboard at http://pi.local:{STATUS_PORT}/")
    log.info(f"Setup     at http://pi.local:{STATUS_PORT}/setup")
    log.info(f"Admin     at http://pi.local:{STATUS_PORT}/admin")
    log.info(f"Status API at http://pi.local:{STATUS_PORT}/status")

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
