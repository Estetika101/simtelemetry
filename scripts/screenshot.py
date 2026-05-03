"""
Capture screenshots of the running Pacefinder app for marketing use.

Usage:
    python3 scripts/screenshot.py --port 8765 --out screenshots/

Requires: playwright (pip install playwright && playwright install chromium)
The app must already be running at the given port (demo mode).
"""

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile":  {"width": 390,  "height": 844},
}


def wait_for_app(base_url: str, retries: int = 20, delay: float = 0.5):
    for i in range(retries):
        try:
            urllib.request.urlopen(f"{base_url}/health", timeout=2)
            return
        except Exception:
            if i == retries - 1:
                raise RuntimeError(f"App not responding at {base_url} after {retries} attempts")
            time.sleep(delay)


def get_best_session_id(base_url: str) -> str | None:
    """Return session_id of the session with the lowest best_lap_time_s."""
    try:
        with urllib.request.urlopen(f"{base_url}/sessions/data", timeout=5) as r:
            sessions = json.loads(r.read())
        if not sessions:
            return None
        best = min(sessions, key=lambda s: s.get("best_lap_time_s") or 9999)
        return best["session_id"]
    except Exception as e:
        print(f"  [warn] Could not fetch sessions: {e}", file=sys.stderr)
        return None


def take_screenshots(base_url: str, out_dir: Path):
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    best_sid = get_best_session_id(base_url)

    pages_desktop = [
        ("live-dashboard",  f"{base_url}/"),
        ("sessions-home",   f"{base_url}/sessions"),
        ("sessions-forza",  f"{base_url}/sessions/game?name=forza_motorsport"),
        ("sessions-track",  f"{base_url}/sessions/track?name=Suzuka+Circuit&game=forza_motorsport"),
    ]
    if best_sid:
        pages_desktop.append(
            ("telemetry", f"{base_url}/sessions/session?id={best_sid}")
        )

    with sync_playwright() as pw:
        for vp_name, vp in VIEWPORTS.items():
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                viewport=vp,
                device_scale_factor=2,
                color_scheme="dark",
            )
            page = ctx.new_page()

            suffix = "" if vp_name == "desktop" else "-mobile"

            for name, url in pages_desktop:
                try:
                    print(f"  [{vp_name}] {name} → {url}")
                    page.goto(url, wait_until="networkidle", timeout=15_000)
                    # Extra pause for JS-rendered charts/tables
                    page.wait_for_timeout(1200)

                    fname = out_dir / f"{name}{suffix}.png"
                    page.screenshot(path=str(fname), full_page=False)
                    print(f"           saved {fname}")
                except Exception as e:
                    print(f"  [error] {name}{suffix}: {e}", file=sys.stderr)

            browser.close()

    print(f"\nDone. Screenshots written to {out_dir}/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--out",  type=str, default="screenshots")
    args = parser.parse_args()

    base_url = f"http://localhost:{args.port}"
    out_dir  = Path(args.out)

    print(f"Waiting for app at {base_url}…")
    wait_for_app(base_url)
    print("App is up. Capturing screenshots…\n")
    take_screenshots(base_url, out_dir)
