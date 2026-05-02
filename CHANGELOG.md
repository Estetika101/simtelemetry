# Changelog

## v0.5.0 — Sessions hierarchy + game selector (2026-05-02)

- `7f5eba8` Sessions nav is now a 4-level hierarchy: Sessions → Game → Track → Session
- `/sessions` serves a game selector (Forza / ACC / F1 cards with session/track counts)
- `/sessions/game?name=X` serves the tracks grid filtered by game
- `/sessions/track?name=X&game=Y` breadcrumb now shows game level with correct back-link
- `/sessions/session?id=X&game=Y&track=Z` breadcrumb reconstructs full chain
- `game` param threaded through all URL navigation and DB queries
- New `/sessions/games` JSON endpoint (`_db_games_index()`)
- `/sessions/tracks` and `/sessions/track/data` now accept optional `?game=` filter

## v0.4.0 — SQLite + sessions UI refactor (2026-05-02)

- `b88cf98` SQLite layer live: `_db_init()` at startup, DB write on session close, migrate existing JSON sessions
- `a9312d8` `/sessions/data` returns summary-only rows (no lap data embedded); Finish Race overlay uses `/sessions/session/data?id=X`
- Three-level sessions UI: tracks index → track detail → session detail
- Per-track AI coaching tip (one sentence, cached in `track_tips` DB table)
- Track detail page: inline SVG spark graphs for lap time trends, best lap highlight, tip bar
- Session detail page: full lap table with slip metrics (avg slip, peak slip, slip > 0.1%)
- Slip stats computed on-demand from `_laps.json` samples (not stored in DB)
- AI analysis result cached in `sessions.ai_analysis` / `ai_analyzed_at` / `ai_model` columns

## v0.3.0 — Live dashboard redesign (2026-05-02)

- `f70d813` / `16ec1a6` Full-viewport 4-column layout: throttle | brake | rear slip | lap timing
- Vertical fill bars for throttle, brake, slip (height-based, not width)
- Gear / speed / RPM demoted to bottom strip (68px) — visible in cockpit, not primary UI
- Flash alerts: red border pulse on conflicting inputs, near-lockup, oversteer, pace delta > 1.5s
- `57df54b` Finish Race overlay on live dashboard showing final lap summary

## v0.2.0 — AI analysis + admin tools (2026-04-xx)

- `77f5c8c` `/analyze?id=X` endpoint — Claude API post-session analysis with historical baseline
- Prompt: per-lap table + last 3 sessions at same track for comparison
- `ca17928` `/admin` page — inject fake UDP packets, sliders for speed/throttle/brake/gear, presets
- `bd74c64` Debug console — SSE stream of all log output, color-coded, filter dropdown, autoscroll
- `311b1f5` Admin nav hidden by default; revealed with `?debug=true` on any page

## v0.1.0 — Core listener + dashboard (2026-04-xx)

- `b7abbc9` Initial commit: UDP listener for Forza Motorsport
- `2324d50` Full parser coverage (FM/FH5/ACC/F1), session lifecycle, lap tracking, live dashboard, replay tool
- `7a9d1d9` Setup page + persistent config (`simtelemetry.config.json`)
- `1acd604` Fix: read full POST body before parsing JSON config
- `807b928` `.gitignore` for config, local data, logs, raw archives
- `bc4a667` Idle detection — idle packets no longer create sessions or duplicate records
- `23b392f` Session idle timeout (30s no driving → auto-close); status dot shows active vs idle
- `b745b97` / `df43fe7` Forza track name resolution via `FORZA_TRACKS` ordinal map
- `38fa3cf` Storage path browser in Setup page
- FH5 auto-detect by packet size (311 vs 331 bytes)
- `56941f0` / `5fdb681` Topbar and sessions UI contrast improvements
