"""
test_listener.py — smoke-test the SimTelemetry UDP listener.

Builds a valid packet for each game (Forza, ACC, F1), verifies each
parses correctly locally, sends them to the configured UDP ports, then
polls /status to confirm the listener registered the packets.

Usage:
    python test_listener.py [--host HOST] [--status-port PORT]

Defaults assume the listener is running locally (127.0.0.1, port 8000).
"""

import argparse
import json
import socket
import struct
import sys
import time
import urllib.request

# ── packet builders ───────────────────────────────────────────────────────────

# i I [51×f] [5×i] [17×f] H [6×B] [3×b]  → 85 fields, 311 bytes
_FM_FORMAT = "<iI" + "f"*51 + "i"*5 + "f"*17 + "H" + "B"*6 + "b"*3

def build_forza_packet() -> bytes:
    """311-byte Forza Motorsport Car Dash packet with plausible values."""
    values = [
        # i, I
        1, 12345,
        # engine (3f)
        8000.0, 800.0, 5500.0,
        # accel xyz (3f)
        0.1, 9.5, 0.0,
        # velocity xyz (3f)
        10.0, 0.0, 0.0,
        # angular velocity xyz (3f)
        0.0, 0.0, 0.0,
        # yaw pitch roll (3f)
        0.0, 0.0, 0.0,
        # normalized_suspension_travel x4 (4f)
        0.5, 0.5, 0.5, 0.5,
        # tire_slip_ratio x4 (4f)
        0.01, 0.01, 0.02, 0.02,
        # wheel_rotation_speed x4 (4f)
        100.0, 100.0, 100.0, 100.0,
        # wheel_on_rumble_strip x4 (4f)
        0.0, 0.0, 0.0, 0.0,
        # wheel_in_puddle x4 (4f)
        0.0, 0.0, 0.0, 0.0,
        # surface_rumble x4 (4f)
        0.0, 0.0, 0.0, 0.0,
        # tire_slip_angle x4 (4f)
        0.0, 0.0, 0.0, 0.0,
        # tire_combined_slip x4 (4f)
        0.02, 0.02, 0.02, 0.02,
        # suspension_travel_meters x4 (4f)
        0.1, 0.1, 0.1, 0.1,
        # car_ordinal, car_class, car_performance_index, drivetrain_type, num_cylinders (5i)
        42, 3, 750, 1, 6,
        # position xyz (3f)
        100.0, 0.5, 200.0,
        # speed (m/s≈100mph), power (W), torque (Nm) (3f)
        44.7, 250000.0, 400.0,
        # tire_temp x4 (4f)
        85.0, 85.0, 85.0, 85.0,
        # boost, fuel, distance_traveled (3f)
        0.5, 0.6, 1500.0,
        # best_lap, last_lap, current_lap, current_race (4f)
        90.234, 91.001, 15.5, 305.0,
        # lap_number (H)
        3,
        # race_position, accel, brake, clutch, handbrake, gear (6B)
        5, 200, 0, 0, 0, 4,
        # steer, normalized_driving_lane, normalized_ai_brake_difference (3b)
        50, 0, 0,
    ]
    data = struct.pack(_FM_FORMAT, *values)
    assert len(data) == 311
    return data


def build_acc_packet() -> bytes:
    """Minimal ACC physics packet (≥ 100 bytes).

    Field order matches parse_acc's sequential ri() calls:
    packet_id(i), gas(f), brake(f), fuel(f), gear(i), rpm(i),
    steer(f), speed_kmh(f), vel xyz(fff), acc xyz(fff), wheelSlip(ffff)
    """
    fields = [
        0,      # packet_id (i)
        0.75,   # gas (f)
        0.0,    # brake (f)
        50.0,   # fuel (f)
        4,      # gear (i)
        6000,   # rpm (i)
        -0.2,   # steer (f)
        180.0,  # speed_kmh (f)
        0.0, 0.0, 50.0,  # vel xyz
        8.5, 0.0, 0.2,   # acc xyz (g_lat = 8.5/9.81)
        0.01, 0.01, 0.02, 0.02,  # wheelSlip
    ]
    data = struct.pack("<ifffiiffffffffffff", *fields)
    return data.ljust(200, b'\x00')


def _f1_header(packet_id: int, session_uid: int = 0xDEADBEEFCAFE) -> bytes:
    """Build a 29-byte F1 header.

    Field layout the listener actually reads:
      offset 0: H  packetFormat
      offset 2: B  (gameYear / ignored)
      offset 3: B  (major   / ignored)
      offset 4: B  (minor   / ignored)
      offset 5: B  packetId          ← struct.unpack_from("<B", data, 5)
      offset 6: B  (extra   / ignored)
      offset 7: Q  sessionUID        ← struct.unpack_from("<Q", data, 7)
      offset 15: f sessionTime
      offset 19: I frameIdentifier
      offset 23: I overallFrameIdentifier
      offset 27: B playerCarIndex    ← F1_HEADER_SIZE - 2
      offset 28: B secondaryPlayerCarIndex
    """
    return struct.pack(
        "<HBBBBBQfIIBB",
        2024,        # packetFormat
        24, 1, 0,    # gameYear, major, minor (ignored by parser)
        packet_id,   # at offset 5 — what the parser reads
        0,           # extra byte at offset 6
        session_uid, # Q at offset 7
        0.0,         # sessionTime
        0, 0,        # frameIdentifier, overallFrameIdentifier
        0,           # playerCarIndex at offset 27
        255,         # secondaryPlayerCarIndex
    )


def build_f1_session_packet() -> bytes:
    """F1 Session packet (packet_id=1) to prime track metadata."""
    header = _f1_header(packet_id=1)
    payload = struct.pack("<BbbBHBb",
        0, 25, 20, 50, 5793,
        10,   # sessionType: Race
        11,   # trackId: Monza
    )
    return header + payload


def build_f1_telemetry_packet() -> bytes:
    """F1 CarTelemetry packet (packet_id=6) for player car at index 0."""
    header = _f1_header(packet_id=6)
    # 60-byte car entry (player car at index 0):
    # speed(H), throttle(f), steer(f), brake(f), clutch(B), gear(b),
    # engineRPM(H), drs(B), revLightsPercent(B), revLightsBitValue(H),
    # brakesTemp(4H), tyresSurfaceTemp(4B), tyresInnerTemp(4B),
    # engineTemp(H), tyresPressure(4f), surfaceType(4B)
    car = struct.pack(
        "<HfffBbHBBH4H4B4BH4f4B",
        290, 0.85, -0.1, 0.0, 0, 7, 11500, 1, 80, 0,
        400, 400, 400, 400,
        90, 90, 88, 88,
        95, 95, 93, 93,
        105,
        23.5, 23.5, 22.8, 22.8,
        0, 0, 0, 0,
    )
    return header + car.ljust(60, b'\x00')


# ── local parse verification ──────────────────────────────────────────────────

def verify_parsers():
    """Import listener parsers and confirm each packet round-trips correctly."""
    sys.path.insert(0, ".")
    try:
        import listener as L
    except Exception as e:
        print(f"  ⚠  Could not import listener.py for local verification: {e}")
        return

    results = {}

    pkt = build_forza_packet()
    r = L.parse_forza(pkt)
    results["forza_motorsport"] = r is not None
    if r:
        print(f"  ✓ forza_motorsport  parses OK  speed_mph={r['speed_mph']:.1f}  gear={r['gear']}")
    else:
        print(f"  ✗ forza_motorsport  parse returned None")

    pkt = build_acc_packet()
    r = L.parse_acc(pkt)
    results["acc"] = r is not None
    if r:
        print(f"  ✓ acc               parses OK  speed_mph={r['speed_mph']}  rpm={r['rpm']}")
    else:
        print(f"  ✗ acc               parse returned None")

    # Session packet (returns None by design), then telemetry
    L.parse_f1(build_f1_session_packet())
    r = L.parse_f1(build_f1_telemetry_packet())
    results["f1"] = r is not None
    if r:
        print(f"  ✓ f1                parses OK  speed_mph={r['speed_mph']}  rpm={r['rpm']}")
    else:
        print(f"  ✗ f1                parse returned None")

    return results


# ── network send + status poll ────────────────────────────────────────────────

PORTS = {
    "forza_motorsport": 5300,
    "acc":              9996,
    "f1":               20777,
}

PACKETS = {
    "forza_motorsport": [build_forza_packet()],
    "acc":              [build_acc_packet()],
    "f1":               [build_f1_session_packet(), build_f1_telemetry_packet()],
}


def send_packets(host: str):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for game, packets in PACKETS.items():
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


def main():
    ap = argparse.ArgumentParser(description="SimTelemetry listener smoke test")
    ap.add_argument("--host",        default="127.0.0.1", help="Listener host")
    ap.add_argument("--status-port", default=8000, type=int, help="HTTP status port")
    ap.add_argument("--parse-only",  action="store_true", help="Only verify parsers, don't send")
    args = ap.parse_args()

    print(f"\nSimTelemetry listener smoke test")
    print(f"Target: {args.host}  status port: {args.status_port}\n")

    print("Step 1 — local parser verification:")
    parse_results = verify_parsers()

    if args.parse_only:
        return

    print()
    print("Step 2 — polling /status before sending...")
    before = poll_status(args.host, args.status_port)
    if not before:
        print("  ⚠  Could not reach /status — is the listener running?")
        print(f"     Start it with: python3 listener.py")
        print()
    else:
        print(f"  status={before.get('status')}  packet_count={before.get('packet_count', 0)}")

    print()
    print("Step 3 — sending mock telemetry packets:")
    send_packets(args.host)

    time.sleep(0.5)

    print()
    print("Step 4 — polling /status after sending...")
    after = poll_status(args.host, args.status_port)

    if not after:
        print("  ✗  Still can't reach /status.")
        print()
        print("Troubleshooting:")
        print("  • Confirm listener.py is running: ps aux | grep listener.py")
        print("  • Check listener logs for storage errors")
        print(f"  • Check firewall isn't blocking UDP ports {list(PORTS.values())}")
        print(f"  • For Pi: python3 test_listener.py --host <pi-ip>")
        return

    packets_before = before.get("packet_count", 0) if before else 0
    packets_after  = after.get("packet_count", 0)
    new_packets    = packets_after - packets_before

    print(f"  status={after.get('status')}  game={after.get('game')}")
    print(f"  packet_count={packets_after}  (+{new_packets} new this run)")
    print(f"  speed_mph={after.get('speed_mph')}  gear={after.get('gear')}  rpm={after.get('rpm')}")

    print()
    if new_packets > 0:
        print("✅  Listener is receiving and parsing packets.")
    else:
        print("❌  No new packets registered.")
        print("    Parsers OK locally but nothing arrived — check network/firewall.")


if __name__ == "__main__":
    main()
