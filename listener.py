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
import urllib.parse
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─── Config file ──────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "simtelemetry.config.json"

DEFAULTS: dict = {
    "storage_path":     "/mnt/usb/simtelemetry",
    "session_timeout_s": 10,
    "idle_timeout_s":    30,
    "status_port":      8000,
    "ports": {
        "forza_motorsport": 5300,
        "acc":              9996,
        "f1":               20777,
    },
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
            parsed["tire_wear_fl"]   = values[len(FM_FIELDS)]
            parsed["tire_wear_fr"]   = values[len(FM_FIELDS) + 1]
            parsed["tire_wear_rl"]   = values[len(FM_FIELDS) + 2]
            parsed["tire_wear_rr"]   = values[len(FM_FIELDS) + 3]
            parsed["track_ordinal"]  = values[len(FM_FIELDS) + 4]
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
        """True when the player is actively doing something — not parked/in menu."""
        return (
            parsed.get("speed_mph", 0) > 2 or
            parsed.get("throttle_pct", 0) > 2 or
            parsed.get("brake_pct", 0) > 2 or
            abs(parsed.get("steer", 0)) > 5
        )

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
            "started_at":       self.started_at.isoformat(),
            "ended_at":         datetime.now().isoformat(),
            "packet_count":     self.packet_count,
            "best_lap_time_s":  round(self.best_lap_time_s, 3) if self.best_lap_time_s else None,
            "laps":             laps_summary,
        }

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

        if self.game not in active_sessions:
            session = Session(self.game, datetime.now())
            active_sessions[self.game] = session

        session = active_sessions[self.game]
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
  a { color: #888; text-decoration: none; }
  a:hover { color: #ccc; }
  .topbar { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 16px; }
  .topbar h1 { font-size: 1.1rem; color: #aaa; letter-spacing: 3px; text-transform: uppercase; }
  .topbar nav { font-size: 0.75rem; color: #555; }
  .topbar nav a { margin-left: 16px; }
  .topbar nav a.active { color: #e0e0e0; border-bottom: 1px solid #e0e0e0; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: #1a1a1f; border: 1px solid #2a2a3a; border-radius: 6px; padding: 12px 16px; }
  .card .label { font-size: 0.65rem; color: #666; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px; }
  .card .value { font-size: 1.8rem; font-weight: bold; color: #fff; }
  .card .unit  { font-size: 0.75rem; color: #555; margin-left: 4px; }
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
  .meta { font-size: 0.75rem; color: #555; margin-bottom: 20px; }
  .meta span { color: #888; margin-right: 16px; }
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
.tb{flex:none;height:40px;display:flex;align-items:center;padding:0 18px;gap:14px;border-bottom:1px solid #0f0f0f}
.dot{width:7px;height:7px;border-radius:50%;flex:none;background:#1a1a1a}
.dot.receiving{background:#00ff41;box-shadow:0 0 7px #00ff41;animation:p 1s infinite}
.dot.idle{background:#2a2a2a}
.dot.race_ended{background:#f59e0b}
@keyframes p{0%,100%{opacity:1}50%{opacity:.4}}
.tb-stat{font-size:.65rem;letter-spacing:2px;text-transform:uppercase;color:#2a2a2a;min-width:72px}
.tb-stat.receiving{color:#00ff41}
.tb-stat.race_ended{color:#f59e0b}
.tb-meta{display:flex;gap:18px;flex:1;font-size:.68rem;letter-spacing:1px;overflow:hidden}
.tb-game{color:#555;text-transform:uppercase}
.tb-track{color:#888;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tb-lap{color:#444}
.tb-best{color:#22c55e}
.tb-drs{color:#00ff41;font-weight:bold;display:none}
.tb-cmp{color:#555}
.tb-nav{display:flex;gap:14px;flex:none}
.tb-nav a{font-size:.62rem;color:#222;text-decoration:none;letter-spacing:1px;text-transform:uppercase}
.tb-nav a:hover{color:#666}
.tb-nav a.cur{color:#555;border-bottom:1px solid #333}

/* ── main grid ── */
.main{flex:1;display:flex;flex-direction:column;min-height:0}

/* big 3 stats */
.big-row{flex:1;min-height:0;display:grid;grid-template-columns:1fr 1.1fr 1fr}
.big-cell{display:flex;flex-direction:column;justify-content:center;align-items:center;padding:12px 8px}
.big-cell.center{border-left:1px solid #0f0f0f;border-right:1px solid #0f0f0f}
.big-lbl{font-size:.6rem;color:#222;text-transform:uppercase;letter-spacing:3px;margin-bottom:6px}
.big-num{font-size:clamp(3.5rem,10vw,7.5rem);font-weight:900;line-height:1;color:#fff;letter-spacing:-2px}
.big-unit{font-size:.62rem;color:#222;text-transform:uppercase;letter-spacing:3px;margin-top:5px}
.gear-num{font-size:clamp(7rem,20vw,15rem);font-weight:900;line-height:.9;color:#fff}
.gear-num.N{color:#444}
.gear-num.R{color:#ef4444}

/* rev bar */
.rev-row{flex:none;height:22px;position:relative;background:#060606;border-top:1px solid #0f0f0f;border-bottom:1px solid #0f0f0f;overflow:hidden}
.rev-fill{position:absolute;top:0;left:0;height:100%;width:0%;transition:width .04s linear;background:#1a4d28}
.rev-fill.lo{background:#1a6630}
.rev-fill.mid{background:#22c55e}
.rev-fill.hi{background:#f59e0b}
.rev-fill.shift{background:#ef4444;animation:sf .12s infinite}
@keyframes sf{0%,100%{opacity:1;box-shadow:0 0 20px #ef4444}50%{opacity:.7;box-shadow:0 0 40px #ff4400}}
.rev-label{position:absolute;right:10px;top:50%;transform:translateY(-50%);font-size:.55rem;color:#1a1a1a;letter-spacing:2px}

/* pedal bars */
.ped-row{flex:none;display:flex;gap:0;border-bottom:1px solid #0f0f0f}
.ped{flex:1;padding:10px 16px;border-right:1px solid #0a0a0a}
.ped:last-child{border-right:none}
.ped-top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}
.ped-name{font-size:.58rem;color:#1e1e1e;text-transform:uppercase;letter-spacing:2px}
.ped-pct{font-size:1rem;font-weight:900;min-width:3ch;text-align:right}
.ped-bg{height:14px;background:#0a0a0a;border-radius:2px;overflow:hidden}
.ped-fill{height:100%;width:0%;border-radius:2px;transition:width .04s linear}
.thr .ped-pct{color:#00ff41}.thr .ped-fill{background:#00ff41}
.brk .ped-pct{color:#ef4444}.brk .ped-fill{background:#ef4444}
.clt .ped-pct{color:#f59e0b}.clt .ped-fill{background:#f59e0b}

/* secondary stats */
.sec-row{flex:none;display:flex;border-bottom:1px solid #080808}
.sec{flex:1;padding:8px 12px;text-align:center;border-right:1px solid #0a0a0a}
.sec:last-child{border-right:none}
.sec-lbl{font-size:.55rem;color:#1a1a1a;text-transform:uppercase;letter-spacing:2px;margin-bottom:3px}
.sec-val{font-size:clamp(1rem,2.5vw,1.6rem);font-weight:bold;color:#888}

/* bottom strip */
.bot{flex:none;height:28px;display:flex;align-items:center;padding:0 14px;gap:10px;background:#000}
.udp-strip{flex:1;font-size:.55rem;color:#151515;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.bot-btn{background:none;border:1px solid #111;color:#222;font-family:inherit;font-size:.55rem;padding:2px 8px;border-radius:2px;cursor:pointer;text-transform:uppercase;letter-spacing:1px}
.bot-btn:hover{color:#666;border-color:#333}
.bot-btn.on{color:#00ff41;border-color:#00ff4133}

/* debug overlay */
#dbg{position:fixed;bottom:28px;left:0;right:0;height:230px;background:#03030a;border-top:1px solid #111;z-index:50;display:none;flex-direction:column}
#dbg .dh{flex:none;display:flex;justify-content:space-between;align-items:center;padding:5px 12px;border-bottom:1px solid #0d0d18}
#dbg .dh span{font-size:.55rem;color:#252525;text-transform:uppercase;letter-spacing:2px}
#dbg-log{flex:1;overflow-y:auto;font-size:.66rem;padding:6px 12px;font-family:'Courier New',monospace;line-height:1.6}
</style>
</head>
<body>
<div class="tb">
  <div class="dot" id="dot"></div>
  <span class="tb-stat" id="tb-stat">IDLE</span>
  <div class="tb-meta">
    <span class="tb-game" id="tb-game">—</span>
    <span class="tb-track" id="tb-track">—</span>
    <span class="tb-lap" id="tb-lap">LAP —</span>
    <span class="tb-best" id="tb-best">BEST —</span>
    <span class="tb-drs" id="tb-drs">DRS</span>
    <span class="tb-cmp" id="tb-cmp"></span>
  </div>
  <nav class="tb-nav">
    <a href="/" class="cur">Live</a>
    <a href="/sessions">Sessions</a>
    <a href="/setup">Setup</a>
    <a href="/admin">Admin</a>
  </nav>
</div>

<div class="main">
  <div class="big-row">
    <div class="big-cell">
      <div class="big-lbl">Speed</div>
      <div class="big-num" id="spd">—</div>
      <div class="big-unit">mph</div>
    </div>
    <div class="big-cell center">
      <div class="big-lbl">Gear</div>
      <div class="gear-num" id="gear">—</div>
    </div>
    <div class="big-cell">
      <div class="big-lbl">RPM</div>
      <div class="big-num" id="rpm">—</div>
      <div class="big-unit" id="rpm-sub"></div>
    </div>
  </div>

  <div class="rev-row">
    <div class="rev-fill" id="rev"></div>
    <div class="rev-label">REV</div>
  </div>

  <div class="ped-row">
    <div class="ped thr">
      <div class="ped-top"><span class="ped-name">Throttle</span><span class="ped-pct" id="thr-v">0%</span></div>
      <div class="ped-bg"><div class="ped-fill" id="thr-b"></div></div>
    </div>
    <div class="ped brk">
      <div class="ped-top"><span class="ped-name">Brake</span><span class="ped-pct" id="brk-v">0%</span></div>
      <div class="ped-bg"><div class="ped-fill" id="brk-b"></div></div>
    </div>
    <div class="ped clt">
      <div class="ped-top"><span class="ped-name">Clutch</span><span class="ped-pct" id="clt-v">0%</span></div>
      <div class="ped-bg"><div class="ped-fill" id="clt-b"></div></div>
    </div>
  </div>

  <div class="sec-row">
    <div class="sec"><div class="sec-lbl">G-Lat</div><div class="sec-val" id="glat">—</div></div>
    <div class="sec"><div class="sec-lbl">G-Lon</div><div class="sec-val" id="glon">—</div></div>
    <div class="sec"><div class="sec-lbl">Slip RL</div><div class="sec-val" id="srl">—</div></div>
    <div class="sec"><div class="sec-lbl">Slip RR</div><div class="sec-val" id="srr">—</div></div>
    <div class="sec"><div class="sec-lbl">Fuel Laps</div><div class="sec-val" id="fuel">—</div></div>
    <div class="sec"><div class="sec-lbl">Tyre</div><div class="sec-val" id="tyre" style="font-size:.9rem">—</div></div>
  </div>
</div>

<div class="bot">
  <div class="udp-strip" id="udp-strip"></div>
  <button class="bot-btn" onclick="resetCounters()">Reset</button>
  <button class="bot-btn" id="dbg-btn" onclick="toggleDebug()">Debug</button>
</div>

<div id="dbg">
  <div class="dh">
    <span>Debug Console</span>
    <div style="display:flex;gap:10px;align-items:center">
      <label style="font-size:.58rem;color:#333;cursor:pointer;display:flex;align-items:center;gap:4px"><input type="checkbox" id="dbg-as" checked> scroll</label>
      <select id="dbg-f" onchange="applyFilter()" style="background:#0a0a12;border:1px solid #1a1a28;color:#444;font-family:inherit;font-size:.58rem;padding:2px 6px;border-radius:2px">
        <option value="all">All</option><option value="warn">Warn+</option><option value="udp">UDP</option>
      </select>
      <button onclick="clearDebug()" style="background:none;border:1px solid #1a1a28;color:#333;font-family:inherit;font-size:.58rem;padding:2px 8px;border-radius:2px;cursor:pointer">Clear</button>
    </div>
  </div>
  <div id="dbg-log"></div>
</div>

<script>
const $=id=>document.getElementById(id);
const es=new EventSource('/stream');
let _maxRpm=8500, _dbgEs=null, _dbgOpen=false;
const _dbgLines=[];

es.onmessage=e=>{
  const d=JSON.parse(e.data);
  const recv=d.status==='receiving';
  const ended=d.status==='race_ended';

  $('dot').className='dot '+(recv?'receiving':ended?'race_ended':'idle');
  $('tb-stat').textContent=ended?'RACE ENDED':(d.status||'idle').toUpperCase();
  $('tb-stat').className='tb-stat'+(recv?' receiving':ended?' race_ended':'');
  $('tb-game').textContent=d.game?d.game.replace(/_/g,' ').toUpperCase():'—';
  $('tb-track').textContent=d.track&&d.track!=='unknown'?d.track:'—';
  $('tb-lap').textContent='LAP '+(d.lap??'—');
  $('tb-best').textContent=d.best_lap_time_s?'BEST '+fmt(d.best_lap_time_s):'BEST —';
  $('tb-drs').style.display=d.drs?'':'none';
  $('tb-cmp').textContent=d.tyre_compound||'';

  $('spd').textContent=d.speed_mph!=null?d.speed_mph.toFixed(0):'—';

  const g=d.gear;
  const ge=$('gear');
  ge.textContent=g==null?'—':g===0?'N':g===-1?'R':g;
  ge.className='gear-num'+(g===0?' N':g===-1?' R':'');

  const rpm=d.rpm||0;
  $('rpm').textContent=rpm?Math.round(rpm).toLocaleString():'—';
  if(d.engine_max_rpm&&d.engine_max_rpm>2000)_maxRpm=d.engine_max_rpm;
  const pct=Math.min(100,rpm/_maxRpm*100);
  const rev=$('rev');
  rev.style.width=pct+'%';
  const cls=pct>=88?'shift':pct>=75?'hi':pct>=55?'mid':'lo';
  rev.className='rev-fill '+cls;
  $('rpm-sub').textContent=pct>1?Math.round(pct)+'%':'';

  const thr=d.throttle_pct||0,brk=d.brake_pct||0,clt=d.clutch_pct||0;
  $('thr-b').style.width=thr+'%';$('thr-v').textContent=thr.toFixed(0)+'%';
  $('brk-b').style.width=brk+'%';$('brk-v').textContent=brk.toFixed(0)+'%';
  $('clt-b').style.width=clt+'%';$('clt-v').textContent=clt.toFixed(0)+'%';

  $('glat').textContent=d.g_lat!=null?d.g_lat.toFixed(2)+'g':'—';
  $('glon').textContent=d.g_lon!=null?d.g_lon.toFixed(2)+'g':'—';
  $('srl').textContent=d.slip_rl!=null?d.slip_rl.toFixed(3):'—';
  $('srr').textContent=d.slip_rr!=null?d.slip_rr.toFixed(3):'—';
  $('fuel').textContent=d.fuel_remaining_laps!=null?d.fuel_remaining_laps.toFixed(1):'—';
  $('tyre').textContent=d.tyre_compound||'—';

  const udp=d.udp_received||{},rej=d.udp_rejected||{},rsz=d.last_rejected_size||{};
  $('udp-strip').innerHTML=['forza_motorsport','acc','f1'].map(g=>{
    const n=udp[g]||0,r=rej[g]||0,sz=rsz[g];
    const c=n>0?'#22c55e22':r>0?'#ef444433':'#111';
    return `<span style="color:${c}">${g.replace('_motorsport','').replace('_',' ')}: ${n}ok${r?' '+r+'rej':''}${sz?' ('+sz+'B)':''}</span>`;
  }).join('<span style="color:#0a0a0a"> · </span>');
};
es.onerror=()=>{$('dot').className='dot idle';};

function fmt(s){const m=Math.floor(s/60);return m+':'+(s%60).toFixed(3).padStart(6,'0');}
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
    <a href="/admin">Admin</a>
  </nav>
</div>

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
    }
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


SESSIONS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SimTelemetry · Sessions</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0f;color:#e0e0e0;font-family:'Courier New',monospace;display:flex;flex-direction:column;height:100vh;overflow:hidden}
a{color:#888;text-decoration:none}a:hover{color:#ccc}
.topbar{flex:none;padding:12px 20px;border-bottom:1px solid #1a1a28;display:flex;align-items:baseline;justify-content:space-between}
.topbar h1{font-size:1.1rem;color:#aaa;letter-spacing:3px;text-transform:uppercase}
.topbar nav a{margin-left:16px;font-size:0.75rem;color:#555}
.topbar nav a.active{color:#e0e0e0;border-bottom:1px solid #e0e0e0}
.layout{display:flex;flex:1;overflow:hidden}
/* sidebar */
.sidebar{width:270px;flex:none;border-right:1px solid #1a1a28;display:flex;flex-direction:column;overflow:hidden}
.game-tabs{display:flex;flex:none;border-bottom:1px solid #1a1a28}
.g-tab{flex:1;background:none;border:none;border-bottom:2px solid transparent;color:#444;font-family:inherit;font-size:0.65rem;padding:10px 4px;cursor:pointer;letter-spacing:1px;text-transform:uppercase}
.g-tab.active{color:#22c55e;border-bottom-color:#22c55e}
.g-tab:hover:not(.active){color:#888}
.session-list{flex:1;overflow-y:auto}
.s-item{padding:11px 14px;border-bottom:1px solid #0f0f16;cursor:pointer;border-left:2px solid transparent}
.s-item:hover{background:#0e0e16}
.s-item.active{background:#0a180e;border-left-color:#22c55e}
.s-date{font-size:0.6rem;color:#383838;margin-bottom:2px}
.s-track{font-size:0.78rem;color:#aaa;font-weight:bold;margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.s-meta{display:flex;gap:10px;font-size:0.62rem;color:#404040}
.s-best{color:#22c55e66}
.no-s{padding:24px;font-size:0.72rem;color:#2a2a2a;text-align:center}
/* main */
.main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
.sess-hdr{flex:none;padding:12px 20px;border-bottom:1px solid #1a1a28;display:flex;align-items:center;gap:20px;flex-wrap:wrap;display:none}
.hdr-title{font-size:0.88rem;color:#ccc;font-weight:bold;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hdr-stat .v{font-size:1.05rem;color:#fff;font-weight:bold}
.hdr-stat .l{font-size:0.58rem;color:#383838;text-transform:uppercase;letter-spacing:1px}
.lap-bar{flex:none;display:flex;align-items:center;gap:6px;padding:7px 16px;border-bottom:1px solid #111118;overflow-x:auto;display:none}
.lap-bar::-webkit-scrollbar{height:3px}.lap-bar::-webkit-scrollbar-thumb{background:#2a2a3a}
.l-chip{background:#141418;border:1px solid #222230;color:#444;font-family:inherit;font-size:0.62rem;padding:4px 10px;border-radius:3px;cursor:pointer;white-space:nowrap;flex:none}
.l-chip.active{background:#0a180e;border-color:#22c55e44;color:#22c55e}
.l-chip:hover:not(.active){color:#aaa;border-color:#3a3a4a}
.chart-area{flex:1;overflow-y:auto;overflow-x:hidden}
.chart-area::-webkit-scrollbar{width:4px}.chart-area::-webkit-scrollbar-thumb{background:#1a1a2a}
.c-row{position:relative;border-bottom:1px solid #0a0a12}
.c-lbl{position:absolute;top:5px;left:10px;font-size:0.58rem;color:#2a2a3a;text-transform:uppercase;letter-spacing:1px;pointer-events:none;z-index:1;display:flex;gap:6px;align-items:center}
.c-lbl span{padding:1px 5px;border-radius:2px}
canvas{display:block;cursor:crosshair}
.empty{display:flex;align-items:center;justify-content:center;flex:1;font-size:0.8rem;color:#252525}
#tip{position:fixed;background:#0a0a12;border:1px solid #1e1e2e;border-radius:4px;padding:8px 12px;font-size:0.63rem;pointer-events:none;display:none;z-index:200;min-width:130px;line-height:1.8}
.tr{display:flex;justify-content:space-between;gap:14px}
.tk{color:#444}.tv{font-weight:bold}
</style>
</head>
<body>
<div class="topbar">
  <h1>SimTelemetry</h1>
  <nav>
    <a href="/">Live</a>
    <a href="/sessions" class="active">Sessions</a>
    <a href="/setup">Setup</a>
    <a href="/admin">Admin</a>
  </nav>
</div>
<div class="layout">
  <div class="sidebar">
    <div class="game-tabs">
      <button class="g-tab active" onclick="setGame('forza_motorsport',this)">Forza</button>
      <button class="g-tab" onclick="setGame('acc',this)">ACC</button>
      <button class="g-tab" onclick="setGame('f1',this)">F1</button>
    </div>
    <div class="session-list" id="slist"><div class="no-s">Loading…</div></div>
  </div>
  <div class="main">
    <div class="sess-hdr" id="shdr"></div>
    <div class="lap-bar" id="lbar"></div>
    <div class="chart-area" id="carea"></div>
    <div class="empty" id="empty">Select a session</div>
  </div>
</div>
<div id="tip"></div>
<script>
const PAD = {l:52,r:10,t:22,b:18};
const ROWS = [
  {id:'spd', label:'Speed',  h:130, zero:false,
   ch:[{key:'speed_mph',  color:'#d4d4d4', lbl:'mph'}], ymin:0, ymax:'auto'},
  {id:'ped', label:'Throttle / Brake', h:110, zero:false,
   ch:[{key:'throttle_pct',color:'#22c55e',lbl:'thr'},
       {key:'brake_pct',   color:'#ef4444',lbl:'brk'}], ymin:0, ymax:100},
  {id:'rpm', label:'RPM',    h:100, zero:false,
   ch:[{key:'rpm',         color:'#a78bfa',lbl:'rpm'}], ymin:0, ymax:'auto'},
  {id:'gr',  label:'Gear',   h:80,  zero:false, stepped:true,
   ch:[{key:'gear',        color:'#f59e0b',lbl:'gear'}], ymin:-1, ymax:8},
  {id:'gl',  label:'G-Lat',  h:100, zero:true,
   ch:[{key:'g_lat',       color:'#22d3ee',lbl:'g'}], ymin:'auto',ymax:'auto'},
  {id:'gn',  label:'G-Long', h:100, zero:true,
   ch:[{key:'g_lon',       color:'#fb923c',lbl:'g'}], ymin:'auto',ymax:'auto'},
];

let _game='forza_motorsport', _sessions=[], _cur=null, _laps=[], _lapIdx=0;
let _charts=[], _hT=null;

async function init() {
  try {
    _sessions = await fetch('/sessions/data').then(r=>r.json());
  } catch(e) { _sessions=[]; }
  renderList();
}

function setGame(g,el) {
  _game=g;
  document.querySelectorAll('.g-tab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  renderList();
}

function renderList() {
  const el=document.getElementById('slist');
  const list=[..._sessions].filter(s=>s.game===_game).reverse();
  if(!list.length){el.innerHTML='<div class="no-s">No sessions</div>';return;}
  el.innerHTML=list.map(s=>{
    const dt=new Date(s.started_at).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
    const best=s.best_lap_time_s?fmtLap(s.best_lap_time_s):'--:--.---';
    const laps=(s.laps||[]).length;
    const act=_cur&&_cur.session_id===s.session_id?'active':'';
    return `<div class="s-item ${act}" onclick="pick('${s.session_id}')">
      <div class="s-date">${dt}</div>
      <div class="s-track">${s.track&&s.track!=='unknown'?s.track:'Unknown Track'}</div>
      <div class="s-meta"><span>${laps} lap${laps!==1?'s':''}</span><span class="s-best">${best}</span><span>${s.packet_count||0} pkt</span></div>
    </div>`;
  }).join('');
}

async function pick(id) {
  _cur=_sessions.find(s=>s.session_id===id);
  renderList();
  try {
    _laps=await fetch('/sessions/laps?id='+id).then(r=>r.json());
  } catch(e){_laps=[];}
  _lapIdx=0;
  renderHeader();
  renderLapBar();
  renderCharts();
  document.getElementById('empty').style.display='none';
  document.getElementById('shdr').style.display='flex';
  document.getElementById('lbar').style.display=_laps.length>1?'flex':'none';
}

function renderHeader() {
  const s=_cur;
  const best=s.best_lap_time_s?fmtLap(s.best_lap_time_s):'—';
  const dt=new Date(s.started_at).toLocaleString([],{weekday:'short',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
  const maxSpd=_laps.length?Math.max(..._laps.map(l=>l.max_speed_mph||0)).toFixed(0):'—';
  document.getElementById('shdr').innerHTML=`
    <div class="hdr-title">${s.track&&s.track!=='unknown'?s.track:'Unknown Track'}&nbsp;&middot;&nbsp;${s.game||''}</div>
    <div class="hdr-stat"><div class="v">${best}</div><div class="l">Best Lap</div></div>
    <div class="hdr-stat"><div class="v">${(_laps||[]).length}</div><div class="l">Laps</div></div>
    <div class="hdr-stat"><div class="v">${maxSpd}</div><div class="l">Max mph</div></div>
    <div class="hdr-stat" style="font-size:0.62rem;color:#333">${dt}</div>`;
}

function renderLapBar() {
  document.getElementById('lbar').innerHTML=_laps.map((lap,i)=>{
    const t=lap.lap_time_s?fmtLap(lap.lap_time_s):'—';
    return `<button class="l-chip ${i===_lapIdx?'active':''}" onclick="selLap(${i})">Lap ${lap.lap_number}&nbsp;&nbsp;${t}</button>`;
  }).join('');
}

function selLap(i) {
  _lapIdx=i;
  document.querySelectorAll('.l-chip').forEach((c,j)=>c.classList.toggle('active',j===i));
  renderCharts();
}

function renderCharts() {
  const lap=_laps[_lapIdx];
  const area=document.getElementById('carea');
  area.innerHTML=''; _charts=[];
  if(!lap||!lap.samples||!lap.samples.length){area.innerHTML='<div class="no-s">No sample data for this lap</div>';return;}
  const smp=lap.samples;
  const tMax=smp[smp.length-1].t||1;
  const dpr=window.devicePixelRatio||1;

  ROWS.forEach(row=>{
    let yMin=row.ymin==='auto'?Infinity:row.ymin;
    let yMax=row.ymax==='auto'?-Infinity:row.ymax;
    if(row.ymin==='auto'||row.ymax==='auto'){
      smp.forEach(s=>row.ch.forEach(c=>{
        const v=s[c.key];if(v==null)return;
        if(row.ymin==='auto')yMin=Math.min(yMin,v);
        if(row.ymax==='auto')yMax=Math.max(yMax,v);
      }));
      if(row.zero){yMin=Math.min(yMin,0);yMax=Math.max(yMax,0);}
      const pad=(yMax-yMin)*0.12||1;
      if(row.ymin==='auto')yMin-=pad;
      if(row.ymax==='auto')yMax+=pad;
    }
    if(yMin===yMax)yMax=yMin+1;

    const wrap=document.createElement('div');
    wrap.className='c-row';wrap.style.height=row.h+'px';

    const lbl=document.createElement('div');
    lbl.className='c-lbl';
    lbl.innerHTML=row.ch.map(c=>`<span style="color:${c.color}88">${c.lbl}</span>`).join('');

    const cv=document.createElement('canvas');
    cv.style.height=row.h+'px';cv.height=row.h*dpr;

    wrap.appendChild(lbl);wrap.appendChild(cv);area.appendChild(wrap);
    const ctx={cv,row,smp,yMin,yMax,tMax};
    _charts.push(ctx);

    cv.addEventListener('mousemove',e=>onHover(e,ctx));
    cv.addEventListener('mouseleave',()=>{
      _hT=null;
      _charts.forEach(c=>draw(c,null));
      document.getElementById('tip').style.display='none';
    });
  });

  requestAnimationFrame(()=>{
    _charts.forEach(ctx=>{
      const w=ctx.cv.parentElement.clientWidth;
      ctx.cv.width=w*dpr;ctx.cv.style.width=w+'px';
      draw(ctx,null);
    });
  });
}

function draw(ctx,hT) {
  const {cv,row,smp,yMin,yMax,tMax}=ctx;
  const dpr=window.devicePixelRatio||1;
  const W=cv.width,H=cv.height;
  const {l,r,t,b}=PAD;
  const ox=l*dpr,oy=t*dpr,pw=W-(l+r)*dpr,ph=H-(t+b)*dpr;
  const c=cv.getContext('2d');
  c.clearRect(0,0,W,H);
  c.fillStyle='#050508';c.fillRect(0,0,W,H);

  // grid lines + y-axis labels
  const ticks=3;
  for(let i=0;i<=ticks;i++){
    const gy=oy+ph*(1-i/ticks);
    c.strokeStyle='#0e0e18';c.lineWidth=1;
    c.beginPath();c.moveTo(ox,gy);c.lineTo(ox+pw,gy);c.stroke();
    const val=yMin+(yMax-yMin)*(i/ticks);
    c.fillStyle='#2a2a3a';c.font=`${8.5*dpr}px "Courier New",monospace`;c.textAlign='right';
    c.fillText(fmtN(val,row),(l-4)*dpr,gy+3*dpr);
  }

  // zero line
  if(row.zero){
    const zy=oy+ph*(1-(0-yMin)/(yMax-yMin));
    c.strokeStyle='#1a1a2a';c.lineWidth=1;
    c.beginPath();c.moveTo(ox,zy);c.lineTo(ox+pw,zy);c.stroke();
  }

  // traces
  row.ch.forEach(ch=>{
    c.strokeStyle=ch.color;c.lineWidth=1.5*dpr;c.lineJoin='round';c.lineCap='round';
    c.beginPath();
    let first=true,prevY=null;
    smp.forEach(s=>{
      const v=s[ch.key];if(v==null)return;
      const x=ox+(s.t/tMax)*pw;
      const y=oy+ph*(1-clamp((v-yMin)/(yMax-yMin),0,1));
      if(first){c.moveTo(x,y);first=false;}
      else if(row.stepped&&prevY!=null){c.lineTo(x,prevY);c.lineTo(x,y);}
      else c.lineTo(x,y);
      prevY=y;
    });
    c.stroke();
  });

  // cursor
  if(hT!=null){
    const cx=ox+hT*pw;
    c.strokeStyle='#ffffff18';c.lineWidth=1;
    c.setLineDash([3*dpr,3*dpr]);
    c.beginPath();c.moveTo(cx,oy);c.lineTo(cx,oy+ph);c.stroke();
    c.setLineDash([]);
    row.ch.forEach(ch=>{
      const s=smpAt(smp,hT*tMax);if(!s)return;
      const v=s[ch.key];if(v==null)return;
      const y=oy+ph*(1-clamp((v-yMin)/(yMax-yMin),0,1));
      c.fillStyle=ch.color;c.beginPath();c.arc(cx,y,3*dpr,0,Math.PI*2);c.fill();
    });
  }
}

function onHover(e,ctx) {
  const {cv,smp,tMax}=ctx;
  const dpr=window.devicePixelRatio||1;
  const rect=cv.getBoundingClientRect();
  const pw=cv.width-(PAD.l+PAD.r)*dpr;
  const hT=clamp((( e.clientX-rect.left)*dpr-PAD.l*dpr)/pw,0,1);
  _hT=hT;
  _charts.forEach(c=>draw(c,hT));

  const s=smpAt(smp,hT*tMax);if(!s)return;
  const tip=document.getElementById('tip');
  const rows=[
    {k:'time', v:s.t!=null?s.t.toFixed(2)+'s':'—', col:'#666'},
    {k:'speed',v:(s.speed_mph||0).toFixed(1)+' mph',col:'#d4d4d4'},
    {k:'thr',  v:(s.throttle_pct||0).toFixed(0)+'%', col:'#22c55e'},
    {k:'brk',  v:(s.brake_pct||0).toFixed(0)+'%',    col:'#ef4444'},
    {k:'gear', v:s.gear!=null?(s.gear===-1?'R':s.gear===0?'N':s.gear):'—', col:'#f59e0b'},
    {k:'rpm',  v:s.rpm!=null?Math.round(s.rpm):'—', col:'#a78bfa'},
    {k:'g_lat',v:(s.g_lat||0).toFixed(2)+'g',       col:'#22d3ee'},
    {k:'g_lon',v:(s.g_lon||0).toFixed(2)+'g',       col:'#fb923c'},
  ];
  tip.innerHTML=rows.map(r=>`<div class="tr"><span class="tk">${r.k}</span><span class="tv" style="color:${r.col}">${r.v}</span></div>`).join('');
  tip.style.display='block';
  const tx=e.clientX+18, ty=e.clientY-8;
  tip.style.left=(tx+150>window.innerWidth?e.clientX-162:tx)+'px';
  tip.style.top=Math.min(ty,window.innerHeight-220)+'px';
}

function smpAt(smp,t) {
  let lo=0,hi=smp.length-1;
  while(lo<hi){const m=(lo+hi)>>1;if(smp[m].t<t)lo=m+1;else hi=m;}
  return smp[lo]||null;
}
function clamp(v,a,b){return v<a?a:v>b?b:v;}
function fmtLap(s){const m=Math.floor(s/60);return m+':'+(s%60).toFixed(3).padStart(6,'0');}
function fmtN(v){if(Math.abs(v)>=1000)return Math.round(v);if(Math.abs(v)>=10)return v.toFixed(1);return v.toFixed(2);}

window.addEventListener('resize',()=>{if(_laps.length)renderCharts();});
init();
</script>
</body>
</html>
"""


def _http_response(status: str, content_type: str, body: bytes, extra_headers: str = "") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Access-Control-Allow-Origin: *\r\n"
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
                    config["storage_path"]     = new_path
                    config["session_timeout_s"] = int(incoming.get("session_timeout_s", config["session_timeout_s"]))
                    if "ports" in incoming:
                        config["ports"].update({
                            k: int(v) for k, v in incoming["ports"].items()
                            if k in config["ports"]
                        })
                    save_config(config)
                    msg = "Saved."
                    if incoming.get("ports") and incoming["ports"] != PORTS:
                        msg += " Restart required for port changes to take effect."
                    result = json.dumps({"ok": True, "message": msg, "disk": disk_info()}).encode()
                    writer.write(_http_response("200 OK", "application/json", result))

        elif path == "/status":
            writer.write(_http_response("200 OK", "application/json", json.dumps(state, indent=2).encode()))

        elif path in ("/sessions", "/sessions/"):
            writer.write(_http_response("200 OK", "text/html", SESSIONS_HTML.encode()))

        elif path == "/sessions/data":
            sessions_dir = storage_path() / "sessions"
            files = sorted(sessions_dir.glob("*_[!l]*.json"))
            result = []
            for f in files[-100:]:
                try:
                    result.append(json.loads(f.read_text()))
                except Exception:
                    pass
            writer.write(_http_response("200 OK", "application/json", json.dumps(result).encode()))

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

        elif path == "/reset" and method == "POST":
            for game in PORTS:
                state["udp_received"][game] = 0
                state["udp_rejected"][game] = 0
                state["last_rejected_size"][game] = None
            writer.write(_http_response("200 OK", "application/json", b'{"ok":true}'))

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
