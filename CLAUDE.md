# SimTelemetry — Claude Code Context

## Project Overview

UDP telemetry capture and analysis system for sim racing. Runs on a Raspberry Pi on the local network. The Pi listens for telemetry broadcasts from the game console/PC, records sessions, and serves a live web dashboard + API.

**Stack:** Pure Python 3.9+, stdlib only. No external dependencies. Single file: `listener.py`.

**Target hardware:** Raspberry Pi (any model with network). Also runs on Mac/Windows for development.

---

## Architecture

```
Game Console/PC  ──UDP──►  listener.py (Pi)
                               │
                    ┌──────────┼──────────┐
                    │          │          │
               Raw .bin    Session    HTTP :8000
               archive      JSON     (dashboard,
                                     SSE stream,
                                      API)
```

All logic lives in `listener.py`:
- `asyncio.DatagramProtocol` subclass per game, bound to its UDP port
- `Session` class — manages lifecycle, writes raw archive + JSON
- `session_watchdog` coroutine — closes idle/stale sessions every 2s
- Minimal HTTP server (raw TCP, no framework) — serves dashboard HTML + API endpoints

---

## Supported Games & Ports

| Game | UDP Port | Packet Size |
|------|----------|-------------|
| Forza Motorsport 2023 (FM) | 5300 | 311 bytes |
| Forza Horizon 5 (FH5) | 5300 | 331 bytes (FH5 extension: +tire wear ×4 + track_ordinal) |
| Assetto Corsa Competizione (ACC) | 9996 | ≥100 bytes |
| F1 2023/2024 (Codemasters) | 20777 | variable, header at offset 5 |

Both Forza formats are auto-detected by packet size in `parse_forza()`.

---

## Key Implementation Details

### Packet Formats

```python
FM_PACKET_SIZE    = 311   # Forza Motorsport 2023
FM_PACKET_SIZE_FH = 331   # Forza Horizon 5 (20 extra bytes)
FM_FORMAT_FH = FM_FORMAT + "ffffi"  # +tire_wear_fl/fr/rl/rr + track_ordinal
```

### Track Name Resolution (Forza)

FH5 packets contain a `track_ordinal` integer at the end. Resolved via `FORZA_TRACKS` dict. Ordinals are **non-sequential broadcast IDs** (not 0–N), e.g. Spa=23, Silverstone GP=53, Brands Hatch GP=60 — gaps between IDs are valid. FM2023 does NOT include track in the UDP stream — it's not available in the protocol.

### Driving Detection

```python
def _is_driving(parsed: dict) -> bool:
    return (
        parsed.get("speed_mph", 0) > 2 or
        parsed.get("throttle_pct", 0) > 2 or
        parsed.get("brake_pct", 0) > 2 or
        abs(parsed.get("steer", 0)) > 5
    )
```

Used to suppress idle broadcast packets from creating sessions or recording data.

### Session Lifecycle

1. First driving packet → `Session` created, `last_activity` set
2. Driving packets → `session.ingest()`, `last_activity` updated
3. Idle packets → `last_packet` updated (keepalive), NOT recorded
4. `session_watchdog` checks every 2s:
   - `is_timed_out()` → no packets for 10s → close
   - `is_idle_timed_out()` → no driving for 30s → close
5. On close → `state["status"] = "race_ended"` → clears to "idle" after 30s

### Debug Console

All log lines are fanned out to connected `/debug-stream` SSE clients via `_debug_push()`. A `_DebugLogHandler` hooks into Python's logging system so all `log.*()` calls appear in the browser console.

### HTTP Server

Raw asyncio TCP. No framework. `_http_response()` adds:
- `Access-Control-Allow-Origin: *`
- `Access-Control-Allow-Private-Network: true` ← required for Chrome on local network

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Live dashboard (full-viewport racing display) |
| `/status` | GET | JSON snapshot of current telemetry state |
| `/stream` | GET | SSE live stream at 10 Hz |
| `/sessions` | GET | Sessions page (HTML with game tabs, charts) |
| `/sessions/data` | GET | JSON list of last 100 sessions |
| `/sessions/laps?id=X` | GET | Per-lap JSON for session X |
| `/admin` | GET | Admin page (inject fake UDP, config, debug) |
| `/admin/inject` | POST | Send mock telemetry packets for testing |
| `/debug-stream` | GET | SSE live debug/log console |
| `/reset` | POST | Zero UDP packet counters |
| `/health` | GET | Health check |
| `/config` | GET/POST | Read/write config JSON |

---

## Data Storage

```
/mnt/usb/simtelemetry/          ← configurable via simtelemetry.config.json
  raw/
    <session_id>_<game>.bin     ← raw UDP packet archive
  sessions/
    <session_id>.json           ← session summary + lap times
    <session_id>_laps.json      ← full per-sample lap data
  logs/
    listener.log
```

Falls back to `./data/` if USB isn't mounted.

### Session ID format
`YYYY-MM-DDTHH-MM-SS_<game>` e.g. `2026-05-01T14-32-00_forza_motorsport`

---

## Config File

`simtelemetry.config.json` (gitignored) — runtime config, editable via `/admin`:

```json
{
  "storage_path": "/mnt/usb/simtelemetry",
  "session_timeout_s": 10,
  "idle_timeout_s": 30,
  "status_port": 8000,
  "ports": {
    "forza_motorsport": 5300,
    "acc": 9996,
    "f1": 20777
  }
}
```

---

## Planned: AI Race Analysis

**Goal:** Post-session analysis via Claude API — compare lap performance against historical sessions on the same track.

**Approach:**
- Summarize session data (don't send raw Hz-level data — too large)
- Per-lap metrics: lap time, avg throttle/brake %, top speed, tyre temp range, consistency score
- Historical context: pull all sessions for the same track, compute baseline (best lap, avg lap time)
- Call Anthropic API with structured prompt, return analysis

**Implementation options:**
1. **`analyze.py` script** — standalone, reads `{session_id}_laps.json`, calls API, prints report
2. **`/analyze?id=SESSION_ID` endpoint** — in-browser, returns HTML analysis

**Recommended start:** Script first (~80 lines, stdlib + `anthropic` SDK), then promote to endpoint once the prompt is validated.

**Example prompt shape:**
```
Track: Spa-Francorchamps | Car class: S1 | Session: 2026-05-01 14:32

LAP TABLE:
Lap  Time     Throttle  Brake  MaxSpd  FLtemp  RRtemp
1    2:18.4   71%       18%    241     82°     91°
...

HISTORICAL (same track, last 4 sessions):
Best lap: 2:14.8 (2026-03-12) | Avg: 2:17.2

Analyze this driver's performance. Focus on consistency, tyre management,
and specific areas to improve compared to their historical baseline.
```

---

## Development Notes

- Run locally: `python3 listener.py` — dashboard at `http://localhost:8000`
- Test parsers without a running listener: `python3 test_listener.py --parse-only`
- Send mock packets to a running listener: `python3 test_listener.py`
- Replay a raw `.bin` archive: `python3 replay.py <file.bin> --summary`
- Service file: `simtelemetry.service` → `/etc/systemd/system/` on Pi

### Forza game setup
Settings → HUD and Gameplay → Data Out  
- Data Out IP: Pi's IP (`hostname -I`)  
- Port: 5300  
- Format: **Car Dash**

### Chrome on local network
Chrome enforces Private Network Access. Listener responds with `Access-Control-Allow-Private-Network: true`. If Chrome shows "site can't be reached" on LAN, try incognito (rules out proxy extensions).
