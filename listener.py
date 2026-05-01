"""
SimTelemetry Listener
Supports: Forza Motorsport, Assetto Corsa Competizione, F1 (Codemasters 2023/2024)
Listens on all three ports simultaneously, auto-detects game from packet size/id.
Saves raw archives and structured JSON sessions to USB storage.
Exposes local web status server at http://pi.local:8000
"""

import asyncio
import json
import logging
import os
import shutil
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
def storage_path() -> Path:
    return Path(config["storage_path"])

PORTS             = config["ports"]          # used at bind time; port changes need restart
SESSION_TIMEOUT_S = config["session_timeout_s"]
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

FM_PACKET_SIZE = 311

FM_FORMAT = "<iIfffffffffffffffffffffffffffffffffffffffffffffffffffIiifffffffffffffffHHBBBBBBBBBBbbbbffffHHHHHHHHH"

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
    if len(data) != FM_PACKET_SIZE:
        return None
    try:
        values = struct.unpack(FM_FORMAT, data)
        parsed = dict(zip(FM_FIELDS, values))
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

        raw_path = storage_path() / "raw" / f"{self.session_id}.bin"
        self.raw_file = open(raw_path, "wb")
        log.info(f"Session started: {self.session_id}")

    def ingest(self, raw: bytes, parsed: dict):
        self.raw_file.write(struct.pack("<I", len(raw)) + raw)
        self.last_packet  = time.time()
        self.packet_count += 1

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

    def is_timed_out(self) -> bool:
        return time.time() - self.last_packet > SESSION_TIMEOUT_S

    def close(self) -> dict:
        self.raw_file.close()

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

        # Write summary JSON
        out_path = storage_path() / "sessions" / f"{self.session_id}.json"
        with open(out_path, "w") as f:
            json.dump(session_data, f, indent=2)

        # Write full lap samples as separate file
        samples_path = storage_path() / "sessions" / f"{self.session_id}_laps.json"
        with open(samples_path, "w") as f:
            json.dump(
                [lap.to_dict() for lap in self.completed_laps],
                f, indent=2,
            )

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
    "steer":            0,
    "slip_rl":          0,
    "slip_rr":          0,
    "g_lat":            0,
    "g_lon":            0,
    "drs":              False,
    "tyre_compound":    None,
    "fuel_remaining_laps": None,
    "last_packet_at":   None,
}

active_sessions: dict[str, Session] = {}

def update_state(game: str, session: Session, parsed: dict):
    if parsed.get("_packet_type") in ("motion", None) and "_packet_type" in parsed:
        return  # don't overwrite telemetry state with partial motion data
    state["status"]       = "receiving"
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
    state["rpm"]          = parsed.get("rpm", parsed.get("current_engine_rpm", state["rpm"]))
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
        self.game   = game
        self.parser = parser

    def datagram_received(self, data: bytes, addr):
        parsed = self.parser(data)
        if not parsed:
            return

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

async def session_watchdog():
    while True:
        await asyncio.sleep(2)
        to_close = [
            game for game, session in active_sessions.items()
            if session.is_timed_out()
        ]
        for game in to_close:
            session = active_sessions.pop(game)
            session.close()
            if not active_sessions:
                state["status"] = "idle"
                state["game"]   = None
                log.info("All sessions closed. Listening...")

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
</style>
"""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SimTelemetry</title>
""" + _PAGE_STYLE + r"""
</head>
<body>
<div class="topbar">
  <h1>SimTelemetry <span class="status-dot idle" id="dot"></span><span id="status-text">idle</span></h1>
  <nav>
    <a href="/" class="active">Live</a>
    <a href="/sessions">Sessions</a>
    <a href="/setup">Setup</a>
  </nav>
</div>
<div class="meta">
  <span id="game-meta">—</span>
  <span id="track-meta">—</span>
  <span id="lap-meta">lap —</span>
  <span id="best-meta">best —</span>
  <span id="compound-meta"></span>
  <span id="drs-badge" class="off">DRS</span>
</div>
<div class="grid">
  <div class="card"><div class="label">Speed</div><div class="value" id="speed">—</div><span class="unit">mph</span></div>
  <div class="card"><div class="label">Gear</div><div class="value" id="gear">—</div></div>
  <div class="card"><div class="label">RPM</div><div class="value" id="rpm">—</div></div>
  <div class="card"><div class="label">G-Lat</div><div class="value" id="glat">—</div><span class="unit">g</span></div>
  <div class="card"><div class="label">G-Lon</div><div class="value" id="glon">—</div><span class="unit">g</span></div>
</div>
<div class="bar-row bar-throttle">
  <div class="bar-label"><span>Throttle</span><span id="thr-pct">0%</span></div>
  <div class="bar-bg"><div class="bar-fill" id="thr-bar" style="width:0%"></div></div>
</div>
<div class="bar-row bar-brake">
  <div class="bar-label"><span>Brake</span><span id="brk-pct">0%</span></div>
  <div class="bar-bg"><div class="bar-fill" id="brk-bar" style="width:0%"></div></div>
</div>
<div class="bar-row bar-clutch">
  <div class="bar-label"><span>Clutch</span><span id="clt-pct">0%</span></div>
  <div class="bar-bg"><div class="bar-fill" id="clt-bar" style="width:0%"></div></div>
</div>
<div class="slip-grid">
  <div class="slip-box"><div class="pos">FL slip</div><div class="val" id="slip-fl">—</div></div>
  <div class="slip-box"><div class="pos">FR slip</div><div class="val" id="slip-fr">—</div></div>
  <div class="slip-box"><div class="pos">RL slip</div><div class="val" id="slip-rl">—</div></div>
  <div class="slip-box"><div class="pos">RR slip</div><div class="val" id="slip-rr">—</div></div>
</div>
<script>
const $ = id => document.getElementById(id);
const es = new EventSource('/stream');
es.onmessage = e => {
  const d = JSON.parse(e.data);
  $('dot').className = 'status-dot ' + (d.status || 'idle');
  $('status-text').textContent = d.status || 'idle';
  $('speed').textContent = d.speed_mph != null ? d.speed_mph.toFixed(1) : '—';
  $('gear').textContent = d.gear != null ? (d.gear === 0 ? 'N' : d.gear === -1 ? 'R' : d.gear) : '—';
  $('rpm').textContent = d.rpm != null ? Math.round(d.rpm) : '—';
  $('glat').textContent = d.g_lat != null ? d.g_lat.toFixed(2) : '—';
  $('glon').textContent = d.g_lon != null ? d.g_lon.toFixed(2) : '—';
  const thr = d.throttle_pct || 0, brk = d.brake_pct || 0, clt = d.clutch_pct || 0;
  $('thr-bar').style.width = thr + '%'; $('thr-pct').textContent = thr.toFixed(0) + '%';
  $('brk-bar').style.width = brk + '%'; $('brk-pct').textContent = brk.toFixed(0) + '%';
  $('clt-bar').style.width = clt + '%'; $('clt-pct').textContent = clt.toFixed(0) + '%';
  $('slip-rl').textContent = d.slip_rl != null ? d.slip_rl.toFixed(3) : '—';
  $('slip-rr').textContent = d.slip_rr != null ? d.slip_rr.toFixed(3) : '—';
  $('slip-fl').textContent = '—'; $('slip-fr').textContent = '—';
  $('game-meta').textContent = d.game || '—';
  $('track-meta').textContent = d.track || '—';
  $('lap-meta').textContent = 'lap ' + (d.lap != null ? d.lap : '—');
  $('best-meta').textContent = d.best_lap_time_s ? 'best ' + d.best_lap_time_s.toFixed(3) + 's' : 'best —';
  $('compound-meta').textContent = d.tyre_compound || '';
  $('drs-badge').className = d.drs ? 'on' : 'off';
};
es.onerror = () => { $('dot').className = 'status-dot idle'; };
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
  </nav>
</div>

<div class="section">
  <div class="section-title">Storage</div>
  <div class="field">
    <label>Storage path — where raw archives and session JSON files are saved</label>
    <input type="text" id="storage_path" placeholder="/mnt/usb/simtelemetry">
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
async function load() {
  const r = await fetch('/config');
  const d = await r.json();
  document.getElementById('storage_path').value     = d.storage_path || '';
  document.getElementById('session_timeout_s').value = d.session_timeout_s || 10;
  document.getElementById('port_forza').value        = (d.ports || {}).forza_motorsport || 5300;
  document.getElementById('port_acc').value          = (d.ports || {}).acc || 9996;
  document.getElementById('port_f1').value           = (d.ports || {}).f1 || 20777;
  renderDisk(d.disk);
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
  btn.disabled = true;
  toast.className = 'toast';
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
      toast.className = 'toast ok';
      toast.textContent = d.message || 'Saved.';
      renderDisk(d.disk);
    } else {
      toast.className = 'toast err';
      toast.textContent = d.error || 'Save failed.';
    }
  } catch(e) {
    toast.className = 'toast err';
    toast.textContent = 'Network error: ' + e.message;
  }
  btn.disabled = false;
}

load();
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
        raw = await asyncio.wait_for(reader.read(8192), timeout=5)
        request_str = raw.decode("utf-8", errors="ignore")
        lines = request_str.split("\r\n")
        request_line = lines[0] if lines else ""
        parts = request_line.split(" ")
        method = parts[0] if parts else "GET"
        path   = parts[1].split("?")[0] if len(parts) > 1 else "/"

        # Split headers from body for POST
        header_end = request_str.find("\r\n\r\n")
        raw_body = request_str[header_end + 4:] if header_end != -1 else ""

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

        elif path == "/sessions":
            sessions_dir = storage_path() / "sessions"
            files = sorted(sessions_dir.glob("*_[!l]*.json"))  # exclude _laps.json
            result = []
            for f in files[-50:]:
                try:
                    result.append(json.loads(f.read_text()))
                except Exception:
                    pass
            writer.write(_http_response("200 OK", "application/json", json.dumps(result, indent=2).encode()))

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
    log.info(f"Status API at http://pi.local:{STATUS_PORT}/status")

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
