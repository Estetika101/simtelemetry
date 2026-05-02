# Pacefinder

Records UDP telemetry from Forza Motorsport, ACC, and F1. Saves every session automatically and serves a live dashboard and analysis tools to any device on your network.

Start a race. It records. Stop racing. It stops.

Runs on any always-on machine — Mac, Windows, Linux, Raspberry Pi. Pure Python 3.9+, one external dependency (`anthropic`, only needed for AI coaching).

**[pacefinder.app](https://pacefinder.app)**

---

## What's included

**Always on, no configuration required:**
- **Live dashboard** — speed, throttle, brake, rear slip, tyre temps streamed to any device at 10Hz
- **Session recording** — every session archived automatically to disk and SQLite
- **Session history** — browse by game, circuit, and date with lap trends and career KPIs
- **Lap comparison** — compare any lap against your personal best or theoretical best

**Optional — bring your own Anthropic API key:**
- **Spotter** — post-race AI coaching based on your actual telemetry and historical baseline at each circuit. Add your key in the setup page at `http://localhost:8000/setup`. Costs pennies per session.

---

## Ports

| Game | UDP Port |
|------|----------|
| Forza Motorsport | 5300 |
| Assetto Corsa Competizione | 9996 |
| F1 2023 / 2024 | 20777 |

All three listen simultaneously. Switch games without changing anything.

---

## Quick Start

```bash
git clone https://github.com/Estetika101/pacefinderapp
cd pacefinderapp
python3 listener.py
```

Open `http://localhost:8000` and point your game's Data Out to this machine's IP.

Full setup guide at [pacefinder.app](https://pacefinder.app#install)

---

## Game Setup

### Forza Motorsport
Settings → HUD and Gameplay → Data Out
- Data Out: **ON**
- Data Out IP Address: this machine's IP
- Data Out IP Port: **5300**
- Data Out Packet Format: **Car Dash**

### ACC
Settings → General → UDP Telemetry
- UDP Port: **9996**
- IP: this machine's IP

### F1 2023 / 2024
Settings → Telemetry Settings
- UDP Telemetry: **On**
- UDP Broadcast Mode: **Off**
- UDP IP Address: this machine's IP
- UDP Port: **20777**
- UDP Send Rate: **60Hz**
- UDP Format: **2023** or **2024**

---

## Data Captured

### All games
- Speed, throttle %, brake %, clutch %, gear, RPM, steering
- Rear slip ratios (RL, RR)
- Lateral / longitudinal G-forces

### Forza Motorsport (additional)
- All 4 tyre slip ratios and angles
- Tyre temps (FL/FR/RL/RR)
- Suspension travel, wheel rotation speeds
- Boost, fuel, best/last/current lap time
- Car ordinal, class, performance index, drivetrain

### ACC (additional)
- Tyre pressures and brake temps (4 corners)
- Tyre core temps (4 corners)
- 3-axis G-forces and velocity vector

### F1 2023 / 2024 (additional)
- Track name and session type
- Tyre compound, tyre age, fuel remaining
- Brake temps and tyre surface/inner temps
- Engine temp, DRS status

---

## File Structure

```
{storage_path}/
  simtelemetry.db           ← SQLite (sessions + AI coaching cache)
  raw/                      ← raw UDP packet archives (.bin)
  sessions/
    <session_id>.json       ← session summary + lap times
    <session_id>_laps.json  ← full per-sample lap data
  logs/
    listener.log
```

Default storage path is `./data/`. Change it in the setup page or edit `simtelemetry.config.json`.

---

## Running as a Background Service (Linux / Pi)

```bash
sudo cp simtelemetry.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable simtelemetry
sudo systemctl start simtelemetry
```

Check status: `sudo systemctl status simtelemetry`

---

## Replay & Analysis

Re-parse a raw `.bin` archive without the listener running:

```bash
# Per-lap summary (auto-detects game)
python3 replay.py path/to/archive.bin --summary

# Export as CSV
python3 replay.py archive.bin --csv > session.csv

# Export as JSON
python3 replay.py archive.bin > session.json
```

---

## Troubleshooting

**No data received:**
- Confirm game Data Out IP matches this machine's IP
- Check firewall: `sudo ufw allow 5300/udp`
- Verify with: `sudo tcpdump -i any udp port 5300`

**Port already in use:**
```bash
sudo lsof -i :5300
```

**Service not starting:**
```bash
sudo journalctl -u simtelemetry -f
```

---

## License

<<<<<<< HEAD
MIT

---

*Forza Motorsport is a trademark of Microsoft. Pacefinder is not affiliated with Microsoft, Codemasters, or Kunos Simulazioni.*
=======
MIT — read it, run it, fork it.

---

*Forza Motorsport is a trademark of Microsoft. Pacefinder is not affiliated with Microsoft, Codemasters, or Kunos Simulazioni.*
>>>>>>> 04b817f (f1 and acc listener fixes)
