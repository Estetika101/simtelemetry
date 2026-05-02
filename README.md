# Pacefinder

UDP telemetry listener and live dashboard for Forza Motorsport, ACC, and F1. Captures every session automatically, serves live data to any device on your network, and archives raw packets for post-race analysis. Runs on a Raspberry Pi or any always-on machine.

<!-- Add a screenshot of the live dashboard here -->

## Ports
| Game | UDP Port |
|------|----------|
| Forza Motorsport | 5300 |
| ACC | 9996 |
| F1 (Codemasters) | 20777 |

## Quick Start (Pi)

### 1. Mount USB storage
```bash
sudo mkdir -p /mnt/usb
sudo mount /dev/sda1 /mnt/usb
```

Add to `/etc/fstab` for auto-mount on boot:
```
/dev/sda1 /mnt/usb ext4 defaults,auto 0 0
```

### 2. Install dependencies
No external dependencies — stdlib only (Python 3.9+).

### 3. Copy files
```bash
mkdir ~/simtelemetry
cp listener.py replay.py ~/simtelemetry/
```

### 4. Edit storage path
Open `listener.py` and set:
```python
STORAGE_PATH = Path("/mnt/usb/simtelemetry")
```

### 5. Install as system service (auto-starts on boot)
```bash
sudo cp simtelemetry.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable simtelemetry
sudo systemctl start simtelemetry
```

### 6. Check it's running
```bash
sudo systemctl status simtelemetry
curl http://localhost:8000/health
```

## Game Setup

### Forza Motorsport
Settings → HUD and Gameplay → Data Out
- Data Out: ON
- Data Out IP Address: your Pi's IP (`hostname -I`)
- Data Out IP Port: 5300
- Data Out Packet Format: **Car Dash**

### ACC
Settings → General → UDP Telemetry
- UDP Port: 9996
- IP: your Pi's IP

### F1 2023/2024
Settings → Telemetry Settings
- UDP Telemetry: On
- UDP Broadcast Mode: Off
- UDP IP Address: your Pi's IP
- UDP Port: 20777
- UDP Send Rate: 60Hz
- UDP Format: 2023 or 2024

## Web Dashboard & API

| Endpoint | Description |
|----------|-------------|
| `http://pi.local:8000/` | Live dashboard (speed, throttle/brake traces, G-forces, slip, DRS) |
| `http://pi.local:8000/status` | JSON snapshot of current telemetry state |
| `http://pi.local:8000/stream` | Server-Sent Events live stream (10 Hz) |
| `http://pi.local:8000/sessions` | JSON list of last 50 completed sessions |
| `http://pi.local:8000/health` | Health check |

## Data Captured

### All games
- Speed (mph), throttle %, brake %, clutch %, gear, RPM, steering
- Rear slip ratios (RL, RR)
- Lateral / longitudinal G-forces

### Forza Motorsport (additional)
- All 4 tyre slip ratios and angles
- Tyre temps (FL/FR/RL/RR)
- Suspension travel, wheel rotation speeds
- Boost, fuel, best/last/current lap time
- Car ordinal, class, performance index, drivetrain

### ACC (additional)
- Tyre pressures (4 corners)
- Brake temps (4 corners)
- Tyre core temps (4 corners)
- 3-axis G-forces and velocity vector

### F1 2023/2024 (additional)
- Track name and session type (Race, Qualifying, etc.)
- Tyre compound, tyre age (laps), fuel remaining (laps)
- Brake temps and tyre surface/inner temps (4 corners)
- Tyre pressures (4 corners)
- Engine temp, DRS status
- Position and velocity (Motion packet)

## File Structure
```
/mnt/usb/simtelemetry/
  raw/                    ← raw UDP packet archives (.bin)
  sessions/
    <session_id>.json     ← session summary + lap times
    <session_id>_laps.json ← full per-sample lap data
  logs/                   ← listener.log
```

### Session JSON format
```json
{
  "session_id": "2024-06-01T14-30-00_forza_motorsport",
  "game": "forza_motorsport",
  "track": "unknown",
  "car": "12345",
  "session_type": "unknown",
  "started_at": "2024-06-01T14:30:00",
  "ended_at": "2024-06-01T14:55:00",
  "packet_count": 90000,
  "best_lap_time_s": 82.341,
  "laps": [
    { "lap_number": 1, "lap_time_s": 84.12, "max_speed_mph": 142.3, "sample_count": 5040 }
  ]
}
```

## Replay & Analysis

Re-parse a raw `.bin` archive without the listener running:

```bash
# Print per-lap summary stats (auto-detects game)
python3 replay.py /mnt/usb/simtelemetry/raw/2024-06-01T14-30-00_forza_motorsport.bin --summary

# Export all samples as CSV
python3 replay.py my_session.bin --game forza_motorsport --csv > session.csv

# Export as JSON (default)
python3 replay.py my_session.bin > session.json
```

## Running on Mac (dev)
```bash
python3 listener.py
```
Change STORAGE_PATH to a local path:
```python
STORAGE_PATH = Path("/Users/yourname/simtelemetry")
```

## Running on Windows
```bash
python listener.py
```
```python
STORAGE_PATH = Path("C:/simtelemetry")
```

## Troubleshooting

**Port already in use:**
```bash
sudo lsof -i :5300
```

**USB not mounting:**
```bash
lsblk
sudo mount /dev/sda1 /mnt/usb
```

**Service not starting:**
```bash
sudo journalctl -u simtelemetry -f
```

**No data received:**
- Check game Data Out IP matches Pi IP (`hostname -I`)
- Check Pi firewall: `sudo ufw allow 5300/udp`
- Check with: `sudo tcpdump -i any udp port 5300`
