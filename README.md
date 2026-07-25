# streamdeck-agentdeck

Turn an **Elgato Stream Deck XL+** into a hardware control surface for
[**agent-deck**](https://github.com/asheshgoplani/agent-deck) sessions — manage
your live Claude / GLM / GPT / local coding sessions from the deck: each session
is a key, the LCD strip renders a live-weather Ghibli panorama with overlay
knob zones, and the action row drives lifecycle and selection.

No GUI tool (OpenDeck / StreamController) required — this is a single Python
daemon talking straight to the device via
[python-elgato-streamdeck](https://github.com/abcminiuser/python-elgato-streamdeck),
and to agent-deck via its CLI.

```
                       36 keys · 9 × 4 grid
   ┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐
   │ s0   │ s1   │ s2   │ s3   │ s4   │ s5   │ s6   │ s7   │ s8   │  row 0 — sessions
   ├──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┤
   │  1   │  2   │  3   │  4   │  ·   │  ·   │  ·   │  ·   │  ·   │  row 1 — reply strip
   ├──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┤
   │ prev │ next │ save │ tool │ s9   │ s10  │ s11  │ s12  │ s13  │  row 2 — controls + sessions
   ├──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┤
   │ s14  │ s15  │ s16  │ s17  │ s18  │      │ cfg  │ bri  │ snd  │  row 3 — sessions + tools
   └──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┘
   ╔══════════════════════════════════════════════════════════════╗
   ║ sessions  │ replies  │ system  │  NWS scene   │  brightness  ║  LCD strip
   ╚══════════════════════════════════════════════════════════════╝
                      1200 × 100 px · 5 knob zones
```

## Features

### Session board (36 keys, 9×4 grid)
- Rows 0–3 hold up to 19 session slots (`XL_BOARD_SLOTS`) — all live from
  `agent-deck list --json`, refreshed every couple of seconds.
- Each key is **colored by session state**: amber=waiting · green=running ·
  slate=idle · red=error · blue=starting.
- **Reply strip** (row 1, keys 9–12) answers Claude's numbered permission
  prompts without touching the keyboard — tap `1`/`2`/`3`/`4` to send that
  reply to the active session's tmux pane.
- **Quick controls** (rows 2–3) drive lifecycle: focus previous/next,
  commit/resume, tool menu, status blast, voice dictation.
- Long-press **key 7** cycles the LCD panorama mode.

### LCD strip panorama (1200×100 px)
Three modes cycle on long-press of key 7:

| Mode | Description |
|---|---|
| `normal` | Dark fill + 5 knob zones (sessions/reply/system/brightness) |
| `laputa` | Ghibli-style Siege panorama — fantasy airship battle at golden hour |
| `beach`  | **Live-weather beach panorama** — reflects real GPS coordinates + current NWS conditions (sun position by local hour, animated weather icon, temp, grill-verdict badge) |

Default boot mode: `beach`. The 5 visual knob zones always render on top of
whichever panorama is active, with drop-shadows for readability over
photo-real backgrounds.

The weather zone (400 px wide) pulls from `api.weather.gov` every 15 min,
renders the live condition icon + temperature + a "grill verdict" badge that
says whether conditions are safe for outdoor grilling.

### Live weather + sun position
The `beach` panorama reflects:
- Real GPS coordinates (set via `./install.sh --configure` or directly in
  `config.toml` — defaults to a neutral placeholder)
- Current sun position by local hour (sunrise/sunset/twilight affect sky colors)
- Live NWS conditions (clear / cloudy / rain / storm / snow)

### Two-step spawn picker
Tap an empty session slot:
1. **Tool menu** — pick which agent to launch (default tools: `claude`,
   `claude-glm`, `claude-gpt`, `oc-start` — configurable in `config.toml`).
2. **Placement menu** — `Window` (new konsole) · `Tab` / `Split →` /
   `Split ↓` **inside the konsole you're focused on**, via Konsole's D-Bus API.

### Optional SSH routing
A tool entry can be `ssh -t <host> bash -lc <tool>` so the agent runs on
another machine while the session stays on your board and stays repliable.
Voice dictation works the same way — mic on the SSH host, transcript injected
into the active session via `tmux send-keys -l`.

### Host health monitoring
Configured SSH hosts ping every 2s; the LCD strip shows green/red reachability
dots next to the heartbeat row. Configure via `monitor.hosts` in `config.toml`
(empty list = monitoring disabled).

## Requirements

- Linux with an active local desktop session (X11 recommended for the konsole
  tab/split placement; the session board itself is display-agnostic).
- An **Elgato Stream Deck XL+** (USB vendor `0fd9`, 36 keys + LCD strip).
  The original **Stream Deck Plus** (8 keys + 4 dials + touchscreen) is also
  auto-detected and supported as a legacy target — the daemon picks the
  layout at runtime via `deck_type`.
- [agent-deck](https://github.com/asheshgoplani/agent-deck) on `PATH`
  (`~/.local/bin/agent-deck`), plus `tmux`.
- `python3` with `streamdeck` + `pillow`, and the `libhidapi-libusb0` backend.
- For tab/split placement: KDE **Konsole** + `qdbus` + `xdotool`.

## Install

```bash
git clone <this-repo> ~/streamdeck-agentdeck-src
cd ~/streamdeck-agentdeck-src

# System packages + udev rule + deck.py + systemd --user service
./install.sh

# First-time config (lat/lon for weather, city label, SSH host)
./install.sh --configure
```

`install.sh` is idempotent: it installs the udev rule (uaccess for vendor
`0fd9`, so no root is needed to drive the device), the Python deps, copies
`deck.py` into `~/streamdeck-agentdeck/`, and enables the systemd **user**
service (`--user`, with linger so it survives reboot).

Logs: `journalctl --user -u streamdeck-agentdeck -f`

## Configure

Configuration lives at `~/.config/streamdeck-agentdeck/config.toml` (or
`~/.streamdeck-agentdeck.toml`). Missing keys fall back to built-in defaults —
the file is optional. See `config.example.toml` for the full schema.

| Section | Field | Purpose |
|---|---|---|
| `[weather]` | `lat`, `lon`, `city_name` | GPS coordinates + label for the LCD weather tile. Run `./install.sh --configure` to set interactively. |
| `[deck]` | `ssh_host` | SSH host for remote agent sessions (omit for local-only). |
| `[deck]` | `tools` | `(label, command)` rows for the spawn tool menu. |
| `[deck]` | `placements` | Spawn placement options (Window / Tab / Split → / Split ↓). |
| `[deck]` | `reply_sets` | Bottom-row reply sets — `(label, tmux-key-sequence)` tuples per zone. |
| `[monitor]` | `hosts` | SSH host aliases to health-check on the LCD (empty = disabled). |
| `[terminal]` | `mode` | `konsole` (default) · `tmux` · `none`. |
| `[animations]` | `lcd_mode` | `normal` · `laputa` · `beach` (default). |
| `[pruning]` | `win_miss_threshold`, `idle_timeout_sec`, `prune_cooldown_sec` | Session pruning cadence. |

### Agent flavors

`contrib/claude-glm` is an example wrapper that points Claude Code at the Z.AI
GLM backend (reads its key from `~/.config/claude-glm/secrets`, never embedded).
Drop similar wrappers on `PATH` and add them to `[deck].tools`.

## How it works

- **State + list** come from `agent-deck list --json` (poll); the daemon never
  parses tmux directly for the board.
- **Replies / special keys** go through `tmux send-keys -t <tmux_session>` for
  instant, reliable input (numbers, Enter, Escape).
- **Spawning** is `agent-deck launch <dir> -cmd <tool> --json`; the returned
  session is then attached in the chosen placement.
- **Tab/split** use Konsole D-Bus on the *focused* window:
  `Window.newSession()` + `Session.runCommand()` for a tab; `activateAction`
  `split-view-left-right` / `top-bottom` + run-in-new-pane for a split.
- **LCD strip** renders at 20 fps: the panorama layer + overlay knob zones
  (sessions / reply / system / weather / brightness). Frames are deduped —
  identical frames skip the USB push.
- **Weather loop** polls NWS every 15 min in a daemon thread, writes the
  snapshot to a guarded global, and the render loop reads it.
- **Host monitor** pings configured hosts every 2s; reachability dots render
  on the LCD strip when no live menu is shown.

## Hardware variants

The daemon auto-detects the attached device and switches layout at runtime:

| Device | Keys | Dials/Touch | Layout |
|---|---|---|---|
| **Stream Deck XL+** (primary) | 36 (9×4) | LCD strip only | Sessions on rows 0/2/3, reply strip on row 1, 5 LCD knob zones |
| Stream Deck Plus (legacy) | 8 (4×2) | 4 dials + touchscreen | Sessions on top row, replies on touchscreen, dials for select/page/brightness |

Both share the same `agent-deck` CLI contract and the same `config.toml`
schema; the deck type only affects key layout and where the reply strip lives.

## License

**PolyForm Noncommercial** — personal, hobby, educational, research, and
nonprofit use is free. **Commercial use requires a separate paid license** —
contact the copyright holder. See [LICENSE](LICENSE) for the full terms.
