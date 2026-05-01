# SimTelemetry Listener

UDP telemetry capture for Forza Motorsport, ACC, and F1. Runs on Pi, Mac, Windows, Linux.

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
```bash
pip3 install asyncio
```
No external dependencies — asyncio is stdlib in Python 3.9+.

### 3. Copy files
```bash
mkdir ~/simtelemetry
cp listener.py ~/simtelemetry/
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
curl http://localhost:8000/status
```

## Forza Motorsport Setup
Settings → HUD and Gameplay → Data Out
- Data Out: ON
- Data Out IP Address: your Pi's IP (find with `hostname -I`)
- Data Out IP Port: 5300
- Data Out Packet Format: Car Dash

## ACC Setup
Settings → General → UDP Telemetry
- UDP Port: 9996
- IP: your Pi's IP

## F1 Setup
Settings → Telemetry Settings
- UDP Telemetry: On
- UDP Broadcast Mode: Off
- UDP IP Address: your Pi's IP
- UDP Port: 20777
- UDP Send Rate: 60Hz
- UDP Format: 2023 or 2024

## Status Endpoints
- `http://pi.local:8000/status` — JSON snapshot of current state
- `http://pi.local:8000/stream` — Server-Sent Events live stream
- `http://pi.local:8000/health` — Health check

## File Structure
```
/mnt/usb/simtelemetry/
  raw/       ← raw UDP packet archives (.bin)
  sessions/  ← structured session JSON files
  logs/      ← listener.log
```

## Running on Mac (dev/migration)
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
Change STORAGE_PATH:
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
- Check Forza Data Out IP matches Pi IP (`hostname -I`)
- Check Pi firewall: `sudo ufw allow 5300/udp`
- Check with: `sudo tcpdump -i any udp port 5300`
