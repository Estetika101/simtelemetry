"""
SimTelemetry Listener
Supports: Forza Motorsport, Assetto Corsa Competizione, F1 (Codemasters)
Listens on all three ports simultaneously, auto-detects game from packet size.
Saves raw archives and structured JSON sessions to USB storage.
Exposes local web status server at http://pi.local:8000
"""

import asyncio
import json
import logging
import os
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─── Configuration ────────────────────────────────────────────────────────────

STORAGE_PATH = Path("/mnt/usb/simtelemetry")  # Change to your USB mount point

PORTS = {
    "forza_motorsport": 5300,
    "acc":              9996,
    "f1":               20777,
}

SESSION_TIMEOUT_S = 10      # Seconds of silence before session is closed
STATUS_PORT       = 8000    # Local web server port
LOG_LEVEL         = logging.INFO

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(STORAGE_PATH / "logs" / "listener.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("simtelemetry")

# ─── Storage Setup ────────────────────────────────────────────────────────────

def ensure_storage():
    for subdir in ["raw", "sessions", "logs"]:
        (STORAGE_PATH / subdir).mkdir(parents=True, exist_ok=True)

# ─── Forza Motorsport Parser ──────────────────────────────────────────────────
# FM2023 Data Out packet: 311 bytes
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
        # Convert speed from m/s to mph
        parsed["speed_mph"] = parsed["speed"] * 2.237
        parsed["throttle_pct"] = parsed["accel"] / 255 * 100
        parsed["brake_pct"] = parsed["brake"] / 255 * 100
        parsed["slip_ratio_rl"] = abs(parsed["tire_slip_ratio_rl"])
        parsed["slip_ratio_rr"] = abs(parsed["tire_slip_ratio_rr"])
        return parsed
    except struct.error:
        return None

# ─── ACC Parser ───────────────────────────────────────────────────────────────
# ACC UDP plugin format
# Reference: ACC UDP documentation

ACC_PHYSICS_SIZE = 216
ACC_GRAPHICS_SIZE = 912  # approximate, varies by version

def parse_acc(data: bytes) -> Optional[dict]:
    """Parse ACC physics packet."""
    if len(data) < 100:
        return None
    try:
        # ACC physics packet structure (key fields only)
        # Full struct: packetId(int), gas, brake, fuel, gear, rpm, 
        # steerAngle, speedKmh, velocity(3f), accG(3f), wheelSlip(4f)...
        offset = 0
        packet_id = struct.unpack_from("<i", data, offset)[0]
        offset += 4
        gas     = struct.unpack_from("<f", data, offset)[0]; offset += 4
        brake   = struct.unpack_from("<f", data, offset)[0]; offset += 4
        fuel    = struct.unpack_from("<f", data, offset)[0]; offset += 4
        gear    = struct.unpack_from("<i", data, offset)[0]; offset += 4
        rpm     = struct.unpack_from("<i", data, offset)[0]; offset += 4
        steer   = struct.unpack_from("<f", data, offset)[0]; offset += 4
        speed   = struct.unpack_from("<f", data, offset)[0]; offset += 4
        # velocity vector
        vel_x, vel_y, vel_z = struct.unpack_from("<fff", data, offset); offset += 12
        # accG vector  
        acc_x, acc_y, acc_z = struct.unpack_from("<fff", data, offset); offset += 12
        # wheelSlip (4 floats)
        slip_fl, slip_fr, slip_rl, slip_rr = struct.unpack_from("<ffff", data, offset)

        return {
            "packet_id":    packet_id,
            "throttle_pct": gas * 100,
            "brake_pct":    brake * 100,
            "fuel":         fuel,
            "gear":         gear,
            "rpm":          rpm,
            "steer":        steer,
            "speed_mph":    speed * 0.621371,
            "slip_ratio_rl": abs(slip_rl),
            "slip_ratio_rr": abs(slip_rr),
        }
    except struct.error:
        return None

# ─── F1 Parser ────────────────────────────────────────────────────────────────
# Codemasters F1 UDP format (F1 2023/2024)
# Packet ID 0 = Motion, ID 1 = Session, ID 6 = Car Telemetry

def parse_f1(data: bytes) -> Optional[dict]:
    """Parse F1 car telemetry packet (packet ID 6)."""
    if len(data) < 24:
        return None
    try:
        # Header: packetFormat(H), gameYear(B), gameMajorVersion(B),
        # gameMinorVersion(B), packetVersion(B), packetId(B), sessionUID(Q),
        # sessionTime(f), frameIdentifier(I), playerCarIndex(B), secondaryPlayerCarIndex(B)
        packet_id = struct.unpack_from("<B", data, 6)[0]

        if packet_id != 6:  # Only process car telemetry packets
            return None

        player_idx = struct.unpack_from("<B", data, 23)[0]

        # Car telemetry data starts at offset 24
        # Each car: speed(H), throttle(f), steer(f), brake(f), clutch(B),
        # gear(b), engineRPM(H), drs(B), revLightsPercent(B),
        # revLightsBitValue(H), brakesTemp(4H), tyresSurfaceTemp(4B),
        # tyresInnerTemp(4B), engineTemp(H), tyresPressures(4f),
        # surfaceType(4B)
        car_size = 60
        base = 24 + (player_idx * car_size)

        if len(data) < base + car_size:
            return None

        speed    = struct.unpack_from("<H", data, base)[0]
        throttle = struct.unpack_from("<f", data, base + 2)[0]
        steer    = struct.unpack_from("<f", data, base + 6)[0]
        brake    = struct.unpack_from("<f", data, base + 10)[0]
        gear     = struct.unpack_from("<b", data, base + 15)[0]
        rpm      = struct.unpack_from("<H", data, base + 16)[0]

        return {
            "speed_mph":    speed * 0.621371,
            "throttle_pct": throttle * 100,
            "brake_pct":    brake * 100,
            "steer":        steer,
            "gear":         gear,
            "rpm":          rpm,
        }
    except struct.error:
        return None

# ─── Session Manager ──────────────────────────────────────────────────────────

class Session:
    def __init__(self, game: str, started_at: datetime):
        self.game        = game
        self.started_at  = started_at
        self.session_id  = started_at.strftime("%Y-%m-%dT%H-%M-%S") + f"_{game}"
        self.samples     = []
        self.lap_number  = 0
        self.laps        = []
        self.track       = "unknown"
        self.car         = "unknown"
        self.last_packet = time.time()
        self.packet_count = 0

        # Open raw archive
        raw_path = STORAGE_PATH / "raw" / f"{self.session_id}.bin"
        self.raw_file = open(raw_path, "wb")
        log.info(f"Session started: {self.session_id}")

    def ingest(self, raw: bytes, parsed: dict):
        self.raw_file.write(struct.pack("<I", len(raw)) + raw)
        self.last_packet = time.time()
        self.packet_count += 1

        # Extract track/car from Forza ordinals if available
        if "car_ordinal" in parsed and self.car == "unknown":
            self.car = str(parsed.get("car_ordinal", "unknown"))

        # Track lap transitions
        lap = parsed.get("lap_number", 0)
        if lap and lap != self.lap_number and lap > 0:
            if self.lap_number > 0:
                self.laps.append({
                    "lap": self.lap_number,
                    "samples_count": len(self.samples),
                })
            self.lap_number = lap
            self.samples = []

        # Store sample (throttle, brake, speed, slip, gear)
        self.samples.append({
            "t":            parsed.get("current_lap_time", 0),
            "speed_mph":    round(parsed.get("speed_mph", 0), 1),
            "throttle_pct": round(parsed.get("throttle_pct", 0), 1),
            "brake_pct":    round(parsed.get("brake_pct", 0), 1),
            "gear":         parsed.get("gear", 0),
            "slip_rl":      round(parsed.get("slip_ratio_rl", 0), 3),
            "slip_rr":      round(parsed.get("slip_ratio_rr", 0), 3),
        })

    def is_timed_out(self) -> bool:
        return time.time() - self.last_packet > SESSION_TIMEOUT_S

    def close(self):
        self.raw_file.close()

        # Final lap
        if self.samples:
            self.laps.append({
                "lap": self.lap_number,
                "samples_count": len(self.samples),
            })

        session_data = {
            "session_id":    self.session_id,
            "game":          self.game,
            "track":         self.track,
            "car":           self.car,
            "started_at":    self.started_at.isoformat(),
            "ended_at":      datetime.now().isoformat(),
            "packet_count":  self.packet_count,
            "laps":          self.laps,
        }

        out_path = STORAGE_PATH / "sessions" / f"{self.session_id}.json"
        with open(out_path, "w") as f:
            json.dump(session_data, f, indent=2)

        log.info(f"Session closed: {self.session_id} | {self.packet_count} packets | {len(self.laps)} laps")
        return session_data

# ─── Shared State ─────────────────────────────────────────────────────────────

state = {
    "status":         "idle",
    "game":           None,
    "session_id":     None,
    "started_at":     None,
    "packet_count":   0,
    "lap":            0,
    "speed_mph":      0,
    "throttle_pct":   0,
    "brake_pct":      0,
    "gear":           0,
    "slip_rl":        0,
    "slip_rr":        0,
    "last_packet_at": None,
}

active_sessions: dict[str, Session] = {}

def update_state(game: str, session: Session, parsed: dict):
    state["status"]       = "receiving"
    state["game"]         = game
    state["session_id"]   = session.session_id
    state["started_at"]   = session.started_at.isoformat()
    state["packet_count"] = session.packet_count
    state["lap"]          = parsed.get("lap_number", session.lap_number)
    state["speed_mph"]    = round(parsed.get("speed_mph", 0), 1)
    state["throttle_pct"] = round(parsed.get("throttle_pct", 0), 1)
    state["brake_pct"]    = round(parsed.get("brake_pct", 0), 1)
    state["gear"]         = parsed.get("gear", 0)
    state["slip_rl"]      = round(parsed.get("slip_ratio_rl", 0), 3)
    state["slip_rr"]      = round(parsed.get("slip_ratio_rr", 0), 3)
    state["last_packet_at"] = datetime.now().isoformat()

# ─── UDP Protocol Handlers ────────────────────────────────────────────────────

class TelemetryProtocol(asyncio.DatagramProtocol):
    def __init__(self, game: str, parser):
        self.game   = game
        self.parser = parser

    def datagram_received(self, data: bytes, addr):
        parsed = self.parser(data)
        if not parsed:
            return

        # Session management
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
    """Check for timed-out sessions every 2 seconds."""
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

async def handle_status(reader, writer):
    """Minimal HTTP server — returns JSON status or SSE stream."""
    try:
        request = await asyncio.wait_for(reader.read(1024), timeout=5)
        request_str = request.decode("utf-8", errors="ignore")
        path = request_str.split(" ")[1] if " " in request_str else "/"

        if path == "/status" or path == "/":
            body = json.dumps(state, indent=2)
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
                + body
            )
            writer.write(response.encode())

        elif path == "/stream":
            # Server-Sent Events for real-time updates
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
                await asyncio.sleep(0.5)

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

    # Start UDP listeners
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

    # Start session watchdog
    asyncio.create_task(session_watchdog())

    # Start status server
    server = await asyncio.start_server(handle_status, "0.0.0.0", STATUS_PORT)
    log.info(f"Status server at http://pi.local:{STATUS_PORT}/status")

    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
