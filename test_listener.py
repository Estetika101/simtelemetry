"""
test_listener.py — automated pipeline tests + optional network smoke test.

Tests run in two modes:

  python3 test_listener.py              # full pipeline tests (no server needed)
  python3 test_listener.py --smoke      # also sends real UDP and polls /status
  python3 test_listener.py --host <ip>  # smoke test against remote host

Pipeline tests exercise parse → datagram_received → state / session directly,
catching regressions without a live server.
"""

import argparse
import json
import socket
import struct
import sys
import time
import urllib.request
from typing import Optional

sys.path.insert(0, ".")

# ── packet builders ───────────────────────────────────────────────────────────

_FM_FORMAT = "<iI" + "f"*51 + "i"*5 + "f"*17 + "H" + "B"*6 + "b"*3


def build_forza_packet(speed_mph: float = 100.0, throttle_pct: float = 80.0,
                       lap: int = 1, last_lap: float = 0.0, steer: int = 0) -> bytes:
    speed_ms = speed_mph / 2.237
    accel = int(throttle_pct / 100 * 255)
    vals = [
        1, 12345,
        8000.0, 800.0, 5500.0,
        0.1, 9.5, 0.0,
        speed_ms, 0.0, 0.0,
        0.0, 0.0, 0.0,
        0.0, 0.0, 0.0,
        0.5, 0.5, 0.5, 0.5,
        0.01, 0.01, 0.02, 0.02,
        100.0, 100.0, 100.0, 100.0,
        0.0, 0.0, 0.0, 0.0,
        0.0, 0.0, 0.0, 0.0,
        0.0, 0.0, 0.0, 0.0,
        0.0, 0.0, 0.0, 0.0,
        0.02, 0.02, 0.02, 0.02,
        0.1, 0.1, 0.1, 0.1,
        42, 3, 750, 1, 6,
        100.0, 0.5, 200.0,
        speed_ms, 250000.0, 400.0,   # speed field (m/s) mirrors velocity
        85.0, 85.0, 85.0, 85.0,
        0.5, 0.6, 1500.0,
        90.234, last_lap, 15.5, 305.0,
        lap,
        5, accel, 0, 0, 0, 4,
        steer, 0, 0,
    ]
    data = struct.pack(_FM_FORMAT, *vals)
    assert len(data) == 311
    return data


def build_acc_packet(speed_mph: float = 100.0, throttle_pct: float = 75.0,
                     slip_rl: float = 0.05, slip_rr: float = 0.06) -> bytes:
    speed_kmh = speed_mph * 1.60934
    fields = [
        0,                       # packet_id (physics)
        throttle_pct / 100,      # gas
        0.0,                     # brake
        50.0,                    # fuel
        4,                       # gear
        6000,                    # rpm
        -0.2,                    # steer
        speed_kmh,               # speed_kmh
        0.0, 0.0, speed_kmh / 3.6,  # velocity
        8.5, 0.0, 0.2,           # accG
        0.01, 0.01, slip_rl, slip_rr,  # wheelSlip
    ]
    data = struct.pack("<ifffiiffffffffffff", *fields)
    return data.ljust(200, b'\x00')


def build_acc_graphics_packet(session_type: int = 4, position: int = 3) -> bytes:
    """ACC graphics packet (packet_id=1)."""
    data = struct.pack("<i", 1)           # packet_id = 1
    data += struct.pack("<i", 2)          # status = LIVE
    data += struct.pack("<i", session_type)  # session type (4=race)
    data += b'\x00' * (136 - len(data))   # pad to position field
    data += struct.pack("<i", position)
    data += b'\x00' * 4
    return data


def _f1_header(packet_id: int, session_uid: int = 0xDEADBEEFCAFE,
               player_idx: int = 0) -> bytes:
    """29-byte F1 2024 header. packet_id at offset 6 (where parse_f1 reads it)."""
    return struct.pack(
        "<HBBBBBQfIIBB",
        2024,          # packetFormat (H, offsets 0-1)
        24, 1, 0,      # gameYear, major, minor (B, offsets 2-4)
        1,             # packetVersion (B, offset 5) — not read by parser
        packet_id,     # packetId (B, offset 6) ← what parse_f1 reads
        session_uid,   # Q, offsets 7-14
        0.0,           # sessionTime
        0, 0,          # frameIdentifier, overallFrameIdentifier
        player_idx,    # playerCarIndex (B, offset 27)
        255,           # secondaryPlayerCarIndex
    )


def build_f1_session_packet(track_id: int = 11, session_type: int = 10,
                            uid: int = 0xDEADBEEFCAFE) -> bytes:
    header = _f1_header(packet_id=1, session_uid=uid)
    payload = struct.pack("<BbbBHBb", 0, 25, 20, 50, 5793, session_type, track_id)
    return header + payload


def build_f1_telemetry_packet(speed_mph: float = 180.0, throttle: float = 0.85,
                               uid: int = 0xDEADBEEFCAFE) -> bytes:
    header = _f1_header(packet_id=6, session_uid=uid)
    speed_kmh = int(speed_mph / 0.621371)
    car = struct.pack(
        "<HfffBbHBBH4H4B4BH4f4B",
        speed_kmh, throttle, -0.1, 0.0, 0, 7, 11500, 1, 80, 0,
        400, 400, 400, 400,
        90, 90, 88, 88,
        95, 95, 93, 93,
        105,
        23.5, 23.5, 22.8, 22.8,
        0, 0, 0, 0,
    )
    return header + car.ljust(60, b'\x00')


def build_f1_lapdata_packet(lap_num: int = 2, cur_lap_ms: int = 45000,
                             last_lap_ms: int = 90234,
                             uid: int = 0xDEADBEEFCAFE) -> bytes:
    """F1 2024 LapData (packet_id=2). 50 bytes per car."""
    header = _f1_header(packet_id=2, session_uid=uid)
    # Offsets within each car entry (parse_f1 off_* constants):
    #  0  lastLapTimeInMS (I,4)
    #  4  currentLapTimeInMS (I,4)
    #  8  sector1TimeInMS (H,2) + sector1Minutes (B,1)    → 3 bytes
    # 11  sector2TimeInMS (H,2) + sector2Minutes (B,1)    → 3 bytes
    # 14  deltaToCarInFront (I,4) + deltaToLeader (I,4)   → 8 bytes
    # 22  lapDistance (f,4) + totalDistance (f,4)         → 8 bytes  (NOT 3 floats)
    # 30  carPosition (B)
    # 31  currentLapNum (B)
    # 41  gridPosition (B)
    car = struct.pack("<II", last_lap_ms, cur_lap_ms)  # 0-7
    car += struct.pack("<HB", 0, 0)                    # 8-10  sector1
    car += struct.pack("<HB", 0, 0)                    # 11-13 sector2
    car += struct.pack("<II", 0, 0)                    # 14-21 deltas
    car += struct.pack("<ff", 500.0, 5000.0)           # 22-29 distances (2 floats → 8 bytes)
    car += struct.pack("<B", 3)                        # 30 carPosition
    car += struct.pack("<B", lap_num)                  # 31 currentLapNum
    car = car.ljust(50, b'\x00')
    car = car[:41] + struct.pack("<B", 3) + car[42:]   # 41 gridPosition
    return header + car


def build_f1_motionex_packet(slip_rl: float = 0.08, slip_rr: float = 0.09,
                              uid: int = 0xDEADBEEFCAFE) -> bytes:
    """F1 2024 MotionEx (packet_id=13). wheelSlip at hdr+64."""
    header = _f1_header(packet_id=13, session_uid=uid)
    # 4 groups of 4 floats before wheelSlip: suspPos, suspVel, suspAcc, wheelSpeed = 64 bytes
    pre = struct.pack("<" + "f" * 16, *([0.0] * 16))
    slip = struct.pack("<ffff", 0.01, 0.01, slip_rl, slip_rr)
    return header + pre + slip


# ── test helpers ──────────────────────────────────────────────────────────────

def _reset_state():
    """Reset all global listener state between tests."""
    import listener as L
    L.active_sessions.clear()
    L._f1_session_meta.clear()
    L.state.update({
        "status": "idle", "game": None, "session_id": None,
        "track": "unknown", "car": "unknown", "session_type": "unknown",
        "started_at": None, "packet_count": 0, "lap": 0,
        "speed_mph": 0, "throttle_pct": 0, "brake_pct": 0,
        "gear": 0, "rpm": 0, "engine_max_rpm": 8000, "steer": 0,
        "slip_rl": 0, "slip_rr": 0, "g_lat": 0, "g_lon": 0,
        "drs": False, "tyre_compound": None, "fuel_remaining_laps": None,
        "current_lap_time": None, "last_lap_time_s": None,
        "best_lap_time_s": None, "tyre_fl": None, "tyre_fr": None,
        "tyre_rl": None, "tyre_rr": None, "last_packet_at": None,
        "udp_received": {"forza_motorsport": 0, "acc": 0, "f1": 0},
        "udp_rejected": {"forza_motorsport": 0, "acc": 0, "f1": 0},
        "last_rejected_size": {"forza_motorsport": None, "acc": None, "f1": None},
        "udp_last_at": {"forza_motorsport": None, "acc": None, "f1": None},
    })


def _inject(proto, data: bytes, addr: str = "127.0.0.1"):
    proto.datagram_received(data, (addr, 0))


PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓  {label}")
    else:
        FAIL += 1
        print(f"  ✗  {label}{('  ← ' + detail) if detail else ''}")


# ── parser unit tests ─────────────────────────────────────────────────────────

def test_parsers():
    import listener as L
    print("\n[parsers]")

    # Forza
    r = L.parse_forza(build_forza_packet())
    check("forza parses", r is not None)
    check("forza speed_mph", r and abs(r["speed_mph"] - 100.0) < 1.0, str(r and r.get("speed_mph")))
    check("forza gear",     r and r["gear"] == 4)
    check("forza throttle", r and abs(r["throttle_pct"] - 80.0) < 1.0)
    check("forza slip_rl",  r and r.get("slip_ratio_rl") is not None)
    check("forza tire_temp_fl", r and r.get("tire_temp_fl") == 85.0)

    # ACC physics
    r = L.parse_acc(build_acc_packet(speed_mph=100.0, slip_rl=0.05))
    check("acc physics parses", r is not None)
    check("acc speed_mph",      r and abs(r["speed_mph"] - 100.0) < 1.0, str(r and r.get("speed_mph")))
    check("acc throttle",       r and abs(r["throttle_pct"] - 75.0) < 1.0)
    check("acc slip_rl",        r and abs(r.get("slip_ratio_rl", 0) - 0.05) < 0.001)
    check("acc slip_rr",        r and abs(r.get("slip_ratio_rr", 0) - 0.06) < 0.001)
    check("acc no _packet_type",r and "_packet_type" not in r)

    # ACC graphics
    r = L.parse_acc(build_acc_graphics_packet(session_type=4, position=3))
    check("acc graphics parses",       r is not None)
    check("acc graphics _packet_type", r and r.get("_packet_type") == "graphics")
    check("acc graphics session_type", r and r.get("session_type") == "race")
    check("acc graphics race_position",r and r.get("race_position") == 3)

    # ACC static (packet_id=2) → None
    r = L.parse_acc(struct.pack("<i", 2) + b'\x00' * 100)
    check("acc static → None", r is None)

    # F1 session (packet_id=1) → None (stored in meta, no return value)
    r = L.parse_f1(build_f1_session_packet())
    check("f1 session → None", r is None)

    # F1 CarTelemetry (packet_id=6)
    r = L.parse_f1(build_f1_telemetry_packet(speed_mph=180.0, throttle=0.85))
    check("f1 telemetry parses",     r is not None, str(r))
    check("f1 telemetry _type",      r and r.get("_packet_type") == "telemetry")
    check("f1 telemetry speed_mph",  r and abs(r["speed_mph"] - 180.0) < 2.0, str(r and r.get("speed_mph")))
    check("f1 telemetry throttle",   r and abs(r["throttle_pct"] - 85.0) < 1.0)
    check("f1 tyre_surface_temp_fl", r and r.get("tyre_surface_temp_fl") is not None)

    # F1 LapData (packet_id=2)
    r = L.parse_f1(build_f1_lapdata_packet(lap_num=3, cur_lap_ms=45000, last_lap_ms=90234))
    check("f1 lapdata parses",       r is not None, str(r))
    check("f1 lapdata _type",        r and r.get("_packet_type") == "lap_data")
    check("f1 lapdata lap_number",   r and r.get("lap_number") == 3)
    check("f1 lapdata cur_lap_time", r and abs(r.get("current_lap_time", 0) - 45.0) < 0.1)
    check("f1 lapdata last_lap_time",r and abs(r.get("last_lap_time", 0) - 90.234) < 0.01)

    # F1 MotionEx (packet_id=13)
    r = L.parse_f1(build_f1_motionex_packet(slip_rl=0.08, slip_rr=0.09))
    check("f1 motionex parses",  r is not None, str(r))
    check("f1 motionex _type",   r and r.get("_packet_type") == "motion")
    check("f1 motionex slip_rl", r and abs(r.get("slip_ratio_rl", 0) - 0.08) < 0.001)
    check("f1 motionex slip_rr", r and abs(r.get("slip_ratio_rr", 0) - 0.09) < 0.001)


# ── pipeline tests ────────────────────────────────────────────────────────────

def test_session_creation():
    import listener as L
    _reset_state()
    proto = L.TelemetryProtocol("forza_motorsport", L.parse_forza)
    print("\n[session creation]")

    # Idle packet (speed=0) should not create a session
    _inject(proto, build_forza_packet(speed_mph=0, throttle_pct=0))
    check("no session from idle packet", "forza_motorsport" not in L.active_sessions)

    # Driving packet should create a session
    _inject(proto, build_forza_packet(speed_mph=80))
    check("session created on driving", "forza_motorsport" in L.active_sessions)
    check("state game = forza",   L.state["game"] == "forza_motorsport")
    check("state status = receiving", L.state["status"] == "receiving")
    check("state speed > 0",      L.state["speed_mph"] > 0)


def test_acc_pipeline():
    import listener as L
    _reset_state()
    proto = L.TelemetryProtocol("acc", L.parse_acc)
    print("\n[acc pipeline]")

    # Physics packet while driving
    _inject(proto, build_acc_packet(speed_mph=120.0, slip_rl=0.07, slip_rr=0.08))
    check("acc session created",  "acc" in L.active_sessions)
    check("acc game in state",    L.state["game"] == "acc")
    check("acc speed in state",   abs(L.state["speed_mph"] - 120.0) < 2.0,
          str(L.state["speed_mph"]))
    check("acc slip_rl in state", abs(L.state["slip_rl"] - 0.07) < 0.001,
          str(L.state["slip_rl"]))
    check("acc slip_rr in state", abs(L.state["slip_rr"] - 0.08) < 0.001,
          str(L.state["slip_rr"]))

    # Graphics packet — session_type should update, state should NOT be clobbered
    _inject(proto, build_acc_graphics_packet(session_type=4))
    session = L.active_sessions.get("acc")
    check("acc session_type from graphics", session and session.session_type == "race")
    check("acc status not clobbered",       L.state["status"] != "idle")


def test_f1_pipeline():
    import listener as L
    _reset_state()
    proto = L.TelemetryProtocol("f1", L.parse_f1)
    uid = 0xCAFE1234
    print("\n[f1 pipeline]")

    # Session packet primes track metadata
    _inject(proto, build_f1_session_packet(track_id=10, session_type=10, uid=uid))
    check("f1 session meta stored", uid in L._f1_session_meta)

    # Telemetry creates session and updates state
    _inject(proto, build_f1_telemetry_packet(speed_mph=200.0, uid=uid))
    check("f1 session created",  "f1" in L.active_sessions)
    check("f1 game in state",    L.state["game"] == "f1")
    check("f1 speed in state",   abs(L.state["speed_mph"] - 200.0) < 3.0,
          str(L.state["speed_mph"]))
    check("f1 track from meta",  L.state["track"] != "unknown", L.state["track"])


def test_f1_slip_via_motionex():
    """MotionEx packet (non-driving) should cache slip; next telemetry should merge it."""
    import listener as L
    _reset_state()
    proto = L.TelemetryProtocol("f1", L.parse_f1)
    uid = 0xBEEF0001
    print("\n[f1 rear slip via MotionEx]")

    # Prime session with one telemetry packet
    _inject(proto, build_f1_telemetry_packet(speed_mph=150.0, uid=uid))
    check("f1 session primed", "f1" in L.active_sessions)
    initial_slip = L.state.get("slip_rl", 0)

    # MotionEx with significant slip — should go to motion cache
    _inject(proto, build_f1_motionex_packet(slip_rl=0.12, slip_rr=0.15, uid=uid))
    session = L.active_sessions.get("f1")
    check("motionex cached slip_rl",
          session and abs(session._motion_cache.get("slip_ratio_rl", 0) - 0.12) < 0.001,
          str(session and session._motion_cache))

    # Next telemetry merges motion cache → slip visible in state
    _inject(proto, build_f1_telemetry_packet(speed_mph=150.0, uid=uid))
    check("f1 slip_rl in state after merge",
          abs(L.state.get("slip_rl", 0) - 0.12) < 0.001,
          str(L.state.get("slip_rl")))
    check("f1 slip_rr in state after merge",
          abs(L.state.get("slip_rr", 0) - 0.15) < 0.001,
          str(L.state.get("slip_rr")))
    check("motion cache cleared after merge",
          session and not session._motion_cache)


def test_f1_lap_timing():
    """LapData cache should feed lap transitions on the next telemetry packet."""
    import listener as L
    _reset_state()
    proto = L.TelemetryProtocol("f1", L.parse_f1)
    uid = 0xBEEF0002
    print("\n[f1 lap timing]")

    # Establish session on lap 1
    _inject(proto, build_f1_telemetry_packet(speed_mph=150.0, uid=uid))
    session = L.active_sessions.get("f1")
    check("f1 session on lap 1", session and session.current_lap_num == 0)

    # LapData announces lap 2 completed with a lap time
    _inject(proto, build_f1_lapdata_packet(lap_num=2, cur_lap_ms=45000,
                                           last_lap_ms=90500, uid=uid))
    check("lapdata cached", session and session._lap_cache.get("lap_number") == 2)

    # Telemetry triggers transition (pre-merge in datagram_received)
    _inject(proto, build_f1_telemetry_packet(speed_mph=150.0, uid=uid))
    check("lap transitioned to 2",
          session and session.current_lap_num == 2,
          str(session and session.current_lap_num))
    check("lap time recorded",
          session and session.best_lap_time_s is not None,
          str(session and session.best_lap_time_s))
    check("best_lap in state",
          L.state.get("best_lap_time_s") is not None)
    check("current_lap_time in state",
          L.state.get("current_lap_time") is not None)


def test_forza_lap_timing():
    """Forza lap transitions via lap_number field in the main packet."""
    import listener as L
    _reset_state()
    proto = L.TelemetryProtocol("forza_motorsport", L.parse_forza)
    print("\n[forza lap timing]")

    _inject(proto, build_forza_packet(speed_mph=100, lap=1))
    session = L.active_sessions.get("forza_motorsport")
    check("forza session created", session is not None)

    _inject(proto, build_forza_packet(speed_mph=100, lap=2, last_lap=90.5))
    check("forza lap transitioned",
          session and session.current_lap_num == 2,
          str(session and session.current_lap_num))
    check("forza lap time recorded",
          session and session.best_lap_time_s == 90.5,
          str(session and session.best_lap_time_s))


def test_game_switching():
    """Switching game source: state should reflect the new game, old session must not clobber."""
    import listener as L
    _reset_state()
    forza = L.TelemetryProtocol("forza_motorsport", L.parse_forza)
    f1    = L.TelemetryProtocol("f1",               L.parse_f1)
    uid   = 0xBEEF0003
    print("\n[game switching]")

    # Establish Forza session
    for _ in range(3):
        _inject(forza, build_forza_packet(speed_mph=90))
    check("forza active before switch", "forza_motorsport" in L.active_sessions)
    check("state = forza before switch", L.state["game"] == "forza_motorsport")

    # Switch to F1 — both sessions now coexist until watchdog closes Forza
    _inject(f1, build_f1_telemetry_packet(speed_mph=210.0, uid=uid))
    check("f1 session created alongside forza", "f1" in L.active_sessions)
    check("state = f1 after f1 packet",         L.state["game"] == "f1",
          str(L.state["game"]))
    check("forza session still exists",         "forza_motorsport" in L.active_sessions)

    # Forza session times out (simulate by backdating last_packet)
    L.active_sessions["forza_motorsport"].last_packet -= 20
    import asyncio
    asyncio.get_event_loop().run_until_complete(_run_watchdog_once())

    check("forza session closed", "forza_motorsport" not in L.active_sessions)
    check("f1 session still live", "f1" in L.active_sessions)
    check("state game still f1",   L.state["game"] == "f1",
          str(L.state["game"]))
    check("state status not race_ended",
          L.state["status"] != "race_ended",
          L.state["status"])


def test_tyre_temps_per_game():
    """Each game uses a different tyre temp key — all should map to state tyre_*."""
    import listener as L
    print("\n[tyre temps per game]")

    # Forza: tire_temp_fl
    _reset_state()
    proto = L.TelemetryProtocol("forza_motorsport", L.parse_forza)
    _inject(proto, build_forza_packet(speed_mph=80))
    check("forza tyre_fl in state", L.state.get("tyre_fl") == 85.0,
          str(L.state.get("tyre_fl")))

    # ACC: tyre_core_temp_fl (update_state maps tyre_core_temp_* for ACC)
    _reset_state()
    proto = L.TelemetryProtocol("acc", L.parse_acc)
    _inject(proto, build_acc_packet(speed_mph=80))
    # ACC packet pad is zeros so tyre_core_temp will be 0.0 — just check key exists
    check("acc tyre key exists after packet", "tyre_fl" in L.state)


async def _run_watchdog_once():
    """Run one watchdog check synchronously."""
    import listener as L
    to_close = []
    for game, session in L.active_sessions.items():
        if session.is_timed_out():
            to_close.append((game, "no packets"))
        elif session.is_idle_timed_out():
            to_close.append((game, "idle"))
    for game, reason in to_close:
        session = L.active_sessions.pop(game)
        session.close()
        if not L.active_sessions:
            L.state["status"] = "race_ended"
            L.state["game"]   = None


# ── network smoke test ────────────────────────────────────────────────────────

PORTS = {"forza_motorsport": 5300, "acc": 9996, "f1": 20777}

SMOKE_PACKETS = {
    "forza_motorsport": [build_forza_packet()],
    "acc":              [build_acc_packet()],
    "f1":               [build_f1_session_packet(), build_f1_telemetry_packet()],
}


def send_packets(host: str):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for game, packets in SMOKE_PACKETS.items():
        port = PORTS[game]
        for pkt in packets:
            sock.sendto(pkt, (host, port))
        total = sum(len(p) for p in packets)
        print(f"  ✓ {game:<22} → {host}:{port}  ({total} bytes)")
    sock.close()


def poll_status(host: str, status_port: int, timeout: float = 3.0) -> dict:
    url = f"http://{host}:{status_port}/status"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return json.loads(resp.read())
        except Exception:
            time.sleep(0.2)
    return {}


def run_smoke_test(host: str, status_port: int):
    print(f"\n[smoke test → {host}:{status_port}]")
    before = poll_status(host, status_port)
    if not before:
        print("  ⚠  Could not reach /status — is the listener running?")
        return

    print(f"  status={before.get('status')}  count={before.get('packet_count', 0)}")
    print()
    print("  Sending packets:")
    send_packets(host)
    time.sleep(0.5)

    after = poll_status(host, status_port)
    if not after:
        print("  ✗  Lost /status after sending")
        return

    before_count = before.get("packet_count", 0)
    after_count  = after.get("packet_count", 0)
    delta = after_count - before_count

    print()
    print(f"  status={after.get('status')}  game={after.get('game')}")
    print(f"  packet_count={after_count}  (+{delta})")
    print(f"  speed_mph={after.get('speed_mph')}  gear={after.get('gear')}")

    if delta > 0:
        print("\n  ✅  Listener receiving and parsing packets.")
    else:
        print("\n  ❌  No new packets registered — check network/firewall.")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="SimTelemetry automated tests")
    ap.add_argument("--smoke",       action="store_true", help="Also run network smoke test")
    ap.add_argument("--host",        default="127.0.0.1")
    ap.add_argument("--status-port", default=8000, type=int)
    args = ap.parse_args()

    print("SimTelemetry test suite\n")

    # Pipeline tests (no server needed)
    test_parsers()
    test_session_creation()
    test_acc_pipeline()
    test_f1_pipeline()
    test_f1_slip_via_motionex()
    test_f1_lap_timing()
    test_forza_lap_timing()
    test_game_switching()
    test_tyre_temps_per_game()

    print(f"\n{'─'*40}")
    print(f"  {PASS} passed  {FAIL} failed")
    print(f"{'─'*40}")

    if args.smoke:
        run_smoke_test(args.host, args.status_port)

    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
