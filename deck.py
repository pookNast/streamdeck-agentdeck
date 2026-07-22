#!/usr/bin/env python3
"""
streamdeck-agentdeck v3 — Stream Deck Plus as an AI session board for agent-deck.

The 8 LCD keys ARE your sessions (live from `agent-deck list --json`), colored
by state so a session that needs you (waiting) glows amber. The touchscreen is a
quick-reply bar for the selected session; the dials select / restart / stop.

Spawning is a two-step on-key picker:
    tap empty "+"  ->  TOOL menu (claude / claude-glm / ...)
                   ->  PLACEMENT menu (window / tab / split L R T B)
                   ->  session spawns in that placement, board returns.

  TOUCH (4 zones) -> send to ACTIVE session: [ 1 ] [ 2 ] [ 3 ] [ Esc ]
  DIALS  D0 turn select · D1 turn reply-set · D2 turn page · D3 turn brightness
         push on any dial = that reply slot (D2 push = "Next" on the select set)

Config = the TOOLS / PLACEMENTS / REPLY_ZONES lists below. ponytail: state in
module globals + a lock, no config file — upgrade: external file only if these
must change without a restart.
"""
import os, re, sys, json, time, math, signal, shutil, subprocess, threading, logging, traceback, urllib.request, gzip

from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper
from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
from PIL import Image, ImageDraw, ImageFont
import ghibli_scenes as ghibli
import ghibli_beach

AD = os.path.expanduser("~/.local/bin/agent-deck")

# Shared UI text colors (extracted from repeated per-call-site literals).
TXT_DIM = (205, 210, 220)      # secondary / label text
TXT_BRIGHT = (220, 225, 235)   # primary text
TXT_BANNER = (255, 230, 180)   # banner / menu highlight text
NEW_SESSION_DIR = os.path.expanduser("~")
_WIN_MAP_PATH = os.path.expanduser("~/.cache/agentdeck/windows.json")
_PANE_ORDER_PATH = os.path.expanduser("~/.cache/agentdeck/pane_order.json")
_DBUS_MAP_PATH = os.path.expanduser("~/.cache/agentdeck/dbus_sessions.json")
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_R = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
if not os.path.exists(FONT_R):
    FONT_R = FONT_B
REFRESH_SECS = 2
MENU_TIMEOUT = 12          # seconds before an open picker reverts to the board
SLEEP_SECS = 3600         # idle seconds before the OLEDs blank (wake on any input)

# All agent commands run on the-deck-host over `ssh -t` (PTY): the agent-deck session
# lives on the-host (so it's on the board and you reply via tmux send-keys), but the
# agent process runs on the-deck-host where every tool's stack is installed. A login
# shell (`bash -lc`) puts ~/bin and ~/.local/bin on the remote PATH.
# ponytail: hardcoded tool list — upgrade: read from config.toml when it grows.
SSH_HOST = "the-deck-host"

# Weather + grilling display keys (keys 27 + 28, R3 far-left).
# ponytail: single city hardcoded — upgrade: list + tap-cycle when a 2nd location matters.
# NWS (api.weather.gov): free, no API key, US-gov, the-host-reachable.
# ponytail: 15-min cadence — NWS updates hourly; upgrade: adaptive on alert severity.
FORT_MYERS_LATLON = (0.0, 0.0)
WEATHER_REFRESH_SEC = 900       # 15 min
WEATHER_TIMEOUT_SEC = 8
NWS_USER_AGENT = "streamdeck-agentdeck (git-host:pook/streamdeck-agentdeck)"
GRILL_HORIZON_HOURS = 6
GRILL_WIND_MPH = 20             # sustained wind no-go threshold
GRILL_GUST_MPH = 30             # ponytail: NWS hourly often omits windGust — sustained is main guard
GRILL_HEAT_F = 100              # extreme heat no-go threshold

def _remote(tool):
    return "ssh -t %s bash -lc %s" % (SSH_HOST, tool)

# label doubles as the session title AND the tmux name slug:
# `-t glm` -> session "glm" -> tmux "agentdeck_glm_<rand>". Keep labels short so
# the deck button and the tmux session name correlate (agent-deck adds " (2)" for
# duplicates -> agentdeck_glm-2_<rand>).
TOOLS = [("claude", _remote("claude")),
         ("glm",    _remote("claude-glm")),
         ("gpt",    _remote("claude-gpt")),
         ("local",  _remote("oc-start"))]

# (label, mode). Tab/split act INSIDE the focused konsole window via D-Bus;
# "window" opens a fresh konsole. Konsole places the new split pane on the right
# (split-view-left-right) or bottom (split-view-top-bottom) — top/left placement
# isn't exposed by Konsole's API, so we offer right (→) and down (↓).
PLACEMENTS = [("Window", "window"), ("Tab", "tab"),
              ("Split →", "split-right"), ("Split ↓", "split-down")]
CANCEL_KEY = 7             # last key cancels any open menu

# Bottom-row reply sets: (name, [(label, tmux-keys-or-None) x4]). Pushing knob N
# sends slot N to the active session; knob 2 (dial 1) scroll cycles sets.
# "select" answers Claude's numbered permission MENUS, which are arrow-navigated
# (digits don't work) — send nav+Enter as ONE contiguous send-keys (a lone Enter
# or a long burst gets dropped by the TUI; Up resets to the top option).
# "type" types the literal digit (for plain text input fields). "keys" = misc.
REPLY_SETS = [
    ("select", [("1", ["Up", "Enter"]),
                ("2", ["Down", "Enter"]),
                ("3", ["Down", "Down", "Enter"]),
                ("4", ["Tab", "~0.5", "Enter"])]),
    ("keys",   [("Esc", ["Escape"]), ("Space", ["Space"]),
                ("S-Tab", ["BTab"]),
                ("Voice", ["!voice"])]),
    ("type",   [("1", ["1", "Enter"]), ("2", ["2", "Enter"]),
                ("3", ["3", "Enter"]), ("Esc", ["Escape"])]),
]

STATE_COLOR = {"waiting": (140, 90, 15), "running": (24, 88, 38),
               "idle": (32, 36, 44), "starting": (24, 56, 100),
               "queued": (24, 56, 100), "error": (105, 24, 24),
               "stopped": (26, 26, 32)}
EMPTY_COLOR = (14, 15, 20)
MENU_COLOR = (20, 32, 44)
CANCEL_COLOR = (72, 22, 22)
# Ghibli accent palette — used ONLY for animation layers (pulse/spinner/shimmer/
# sweep). Base STATE_COLOR stays cool dark per the "Ghibli accents on dark base"
# decision. Hex from HueHive Ghibli Palette + icolorpalette Studio Ghibli sets.
GHIBLI = {
    "meadow": (227, 193, 111),   # Golden Meadow   — urgent/menu pulse
    "rose":   (255, 158, 170),   # Spirited Rose   — suggest pulse + rec sweep
    "forest": (106, 130, 82),    # Enchanted Forest — running spinner
    "wind":   (210, 227, 239),   # Whispering Wind — queued shimmer
    "cloud":  (186, 199, 212),   # Castle Cloud    — idle shimmer (subtle)
    "coral":  (248, 131, 121),   # Muted Coral     — error pulse
}
STATE_RANK = {"waiting": 0, "error": 1, "running": 2, "starting": 3,
              "queued": 4, "idle": 5, "stopped": 6}
URG_RANK = {"menu": 0, "urgent": 1, "patient": 2}   # lower rank = higher focus priority

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("deck").info

_lock = threading.Lock()
_sessions = []
_active_id = None
# Top row (keys 0-3) shows a paginated 4-session window; bottom row (keys 4-7)
# always mirrors the touchscreen's 4 reply zones (1/2/Next/Go). Knob 3 (dial 2)
# turns pages. _page_synced_for is an edge-trigger: the page auto-jumps to
# wherever _active_id lives the FIRST time it changes, then leaves the user
# free to page away (e.g. to open a new session) without being fought back.
PAGE_SIZE = 4
MAX_PAGES = 3
MAX_SESSIONS = PAGE_SIZE * MAX_PAGES   # 12
_page = 0
_page_synced_for = None

# --- Stream Deck XL+ layout (36 keys, 9x4, no dials/touchscreen) -------------
# The XL+ (deck_type "Stream Deck XL+", 9 cols x 4 rows) replaces the Plus on
# this rig. It has no dials or touchscreen, so the Plus's 4 dial functions
# (select / cycle-reply-set / page / brightness) and its touchscreen reply-strip
# all move onto keys. Rows 0-2 (keys 0-26) hold the session board — all
# MAX_SESSIONS=12 fit with room, so pagination is dropped — and row 3
# (keys 27-35) is a fixed action row. Cinema mode spans the full 9x4 grid
# (_animate_cinema_xl slices the scene to 36 tiles); the per-key state-animation
# fallback is key_count()-driven and model-agnostic. Per-key image rotation
# (upright glyphs on this quarter-turn panel) lives in the StreamDeckXLPlus
# device class (KEY_ROTATION=270), so nothing here rotates.
# ponytail: page + manual-brightness dials have no XL key (27 slots need no
# paging; brightness auto-dims via sleep/wake) — upgrade: long-press a key if
# manual brightness is ever wanted.
IS_XL = False              # set in main() when the active deck is a Stream Deck XL(+)
HAS_DIALS = False          # set in main(): True for XL+ and Plus (have dials + touchscreen)
# Geometry is the 9x4 / 36-key "XL+" (deck_type "Stream Deck XL+", PID 0x00c6),
# laid out Plus-style: sessions on TOP, the answer/control strip in row 1 sitting
# directly under the session names, then overflow sessions below.
#   row 0  (keys 0-8):   session slots
#   row 1  (keys 9-17):  [1][2][3][4]  [·][·][·][·][·]   (keys 13-17 blank)
#   rows 2-3 (keys 18-35): overflow session slots (keys 18-21 = quick controls)
# So a numbered menu's answer keys are right beneath the sessions they answer
# (the Plus stacked sessions-over-replies the same way).
# ponytail: keys 13-17 (Next/◀/▶/Set/+) are dead — the user never used them.
# Constants retained to document physical positions; upgrade: revive any of them
# by re-adding its elif branch in _animate_xl / _overlay_xl_control / on_key_xl.
XL_BOARD_SLOTS = list(range(0, 9)) + list(range(22, 32))   # 19 session slots (row 0 + rows 2-3 minus quick/status/goal/tool keys)
XL_SLOT_OF_KEY = {k: j for j, k in enumerate(XL_BOARD_SLOTS)}  # key -> session index
XL_REPLY0 = 9              # keys 9-12: reply zones 0-3 (live reply set; select = 1/2/3/4); also menu-cancel
XL_NEXT = 13               # R1 C4 — was "Next"/dismiss; now /commit (M-SD2)
XL_SEL_PREV = 14           # R1 C5 — was select previous; now /resume (M-SD2)
XL_SEL_NEXT = 15           # R1 C6 — was select next; now /clear (M-SD2)
XL_CYCLE = 16              # R1 C7 — was cycle reply set; now /cost (M-SD2)
XL_NEW = 17                # R1 C8 — was new session; now /super-worker (M-SD2)
# M-SD1: control-key spec — spec-driven dispatch for the XL quick/control keys.
# Each spec is a dict with "label", "class", and class-specific payload keys.
# Dispatched by _fire_action(). Long-press behavior layers on top in M-SD3.
#
# Classes:
#   "tmux_keys"     — send tmux key sequence (existing XL_QUICK behavior)
#   "slash_command" — fire "/<cmd>" + Enter to the session (used by M-SD2)
#   "shell"         — run `bash -lc "<cmd>` (was the "!<cmd>" prefix convention)
#   "voice"         — toggle voice input (was the "!voice" special case)
#
# This refactor preserves XL_QUICK's existing behavior verbatim — the old
# ("!voice"/"!<cmd>"/tmux-keys) pseudo-spec collapses into explicit classes.
# ponytail: dict-of-kwargs over a dataclass — YAGNI; the spec never escapes
# this module. Defined before any consumer (XL_SLASH/XL_QUICK) since the
# list literals evaluate _ctrl at import time. Upgrade: dataclass if a 2nd consumer appears.
def _ctrl(label, cls, **payload):
    spec = {"label": label, "class": cls}
    spec.update(payload)
    return spec

XL_SLASH0 = XL_NEXT        # first key of the slash-command row (R1 C4-C8)
# M-SD2: 5 slash-command keys filling the former dead row 1 cols 4-8. Each
# fires "/<cmd>" + Enter to the active session via the slash_command spec
# class wired in M-SD1. cmd is stored without the leading "/" — _fire_action
# adds it. ponytail: no tool filter — if the active session is a shell
# (oc-start) the slash either hits a binary in PATH or no-ops with "command
# not found"; upgrade: gate by sess["tool"] in (claude, glm, gpt) if it bites.
XL_SLASH = [
    _ctrl("/commit",        "slash_command", cmd="commit"),
    _ctrl("/resume",        "slash_command", cmd="resume"),
    _ctrl("/clear",         "slash_command", cmd="clear"),
    _ctrl("/cost",          "slash_command", cmd="cost"),
    _ctrl("/super-worker",  "slash_command", cmd="super-worker"),
]
XL_QUICK0 = 18             # keys 18-21: always-visible quick controls (Esc, S-Tab, Voice, Go)
XL_QUICK = [
    _ctrl("Esc",   "tmux_keys", keys=["Escape"]),
    _ctrl("S-Tab", "tmux_keys", keys=["BTab"]),
    _ctrl("Voice", "voice"),
    _ctrl("Go",    "tmux_keys", keys=["Tab", "~0.5", "Enter"]),
]
XL_STATUS = 35           # R3 C8 — status blast key (M-SD5): opens tmux popup with homelab status
XL_GOAL0 = 33            # R3 C6 — first goal-loop key (M-SD11)
XL_GOAL = [              # M-SD11: goal-loop lifecycle pair beside the Status key.
    _ctrl("/goal",           "slash_command", cmd="goal"),
    _ctrl("/goal complete",  "slash_command", cmd="goal complete"),
]
XL_TOOL_SWAP = 32        # R3 C5 — cycle focused session's tool (M-SD6)
_cancel_key = CANCEL_KEY   # menu-cancel key; XL sets this to XL_REPLY0 (key 9)
_brightness = 60
_ui_mode = "board"         # board | tool | place
_pending_tool = None       # (label, command) chosen in the tool menu -> spawn new
_pending_session = None    # existing session chosen to (re)open in a placement
_menu_deadline = 0.0
_win_map = {}              # session id -> konsole process pid we opened for it
# session id -> Konsole D-Bus session id (the ksid returned by newSession /
# currentSession). Set on tab/split placements where ksid is known. New-window
# sessions rely on _win_map's whole-PID window check (Signal 3) since they're
# the sole session in their konsole process. Used by Signal 4 in _prune_dead
# to catch per-tab closes that leave the konsole PID alive.
_dbus_map = {}
# pid (str) -> [sid, ...] in the order each was placed into that konsole window —
# dict key order is window-open order, list order is top-to-bottom (split) /
# left-to-right (tab) placement order. We control all placement, so insertion
# order IS visual order: tabs only ever append rightward, splits only ever
# split-down. This drives the Stream Deck's session button ordering so it
# mirrors what's actually on screen instead of a stale hand-maintained list.
_pane_order = {}

# Host health + system load — populated by background threads, read by
# render_touchscreen at 20fps. ponytail: globals + a daemon thread each — no
# psutil dep, no new config — upgrade: psutil if the-host already has it.
_host_status = {"the-deck-host": True, "server-host": True, "nas-host": True, "git-host": True}
_host_status_ts = 0.0
_host_status_lock = threading.Lock()
_cpu_prev = None              # (idle_cum, total_cum) last /proc/stat sample
_cpu_ts = 0.0                 # timestamp of last cpu sample
_cpu_pct_cached = 0.0         # last computed cpu % (refreshed at most every 1s)

# Weather + grilling state — populated by _weather_loop (15-min cadence), read
# by render_touchscreen at 20fps. ponytail: globals + a daemon thread + a lock,
# mirrors _host_status pattern. upgrade: persistent cache file to survive restarts.
_weather = {"temp_f": None, "short": "", "icon_word": "", "ts": 0.0, "fail_streak": 0}
_weather_lock = threading.Lock()
_grill = {"ok": None, "reason": "", "ts": 0.0}   # ok=None unknown; True/False decided
_grill_lock = threading.Lock()
_nws_grid = None                                  # cached (gridId, gridX, gridY) tuple; never expires

def _weather_snapshot():
    """Copy of weather + grill state for render-time use (no lock held during draw)."""
    with _weather_lock:
        w = dict(_weather)
    with _grill_lock:
        g = dict(_grill)
    w["grill_ok"] = g.get("ok")
    w["grill_reason"] = g.get("reason", "")
    return w

def _save_win_map():
    try:
        os.makedirs(os.path.dirname(_WIN_MAP_PATH), exist_ok=True)
        with open(_WIN_MAP_PATH, "w") as f:
            json.dump(_win_map, f)
    except Exception as e:
        logging.warning("win_map save failed: %s", e)

def _load_win_map():
    try:
        with open(_WIN_MAP_PATH) as f:
            _win_map.update(json.load(f))
        logging.info("win_map loaded: %d entries", len(_win_map))
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.warning("win_map load failed: %s", e)

def _save_dbus_map():
    try:
        os.makedirs(os.path.dirname(_DBUS_MAP_PATH), exist_ok=True)
        with open(_DBUS_MAP_PATH, "w") as f:
            json.dump(_dbus_map, f)
    except Exception as e:
        logging.warning("dbus_map save failed: %s", e)

def _load_dbus_map():
    try:
        with open(_DBUS_MAP_PATH) as f:
            _dbus_map.update(json.load(f))
        logging.info("dbus_map loaded: %d entries", len(_dbus_map))
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.warning("dbus_map load failed: %s", e)

def _save_pane_order():
    try:
        os.makedirs(os.path.dirname(_PANE_ORDER_PATH), exist_ok=True)
        with open(_PANE_ORDER_PATH, "w") as f:
            json.dump(_pane_order, f)
    except Exception as e:
        logging.warning("pane_order save failed: %s", e)

def _load_pane_order():
    try:
        with open(_PANE_ORDER_PATH) as f:
            _pane_order.update(json.load(f))
        logging.info("pane_order loaded: %d windows", len(_pane_order))
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.warning("pane_order load failed: %s", e)

def _record_pane(pid, sid):
    """Note that session `sid` now lives in konsole window `pid`, at the end
    of that window's placement order (i.e. visually last/bottom-most so far).
    Moves `sid` out of any other window's list first, so a session that got
    reopened elsewhere doesn't appear in two places."""
    pid = str(pid)
    with _lock:
        for p, sids in list(_pane_order.items()):
            if sid in sids and p != pid:
                sids.remove(sid)
                if not sids:
                    del _pane_order[p]
        lst = _pane_order.setdefault(pid, [])
        if sid not in lst:
            lst.append(sid)
        _save_pane_order()

def _forget_pane(sid):
    with _lock:
        changed = False
        for p, sids in list(_pane_order.items()):
            if sid in sids:
                sids.remove(sid)
                changed = True
                if not sids:
                    del _pane_order[p]
        if changed:
            _save_pane_order()
_reply_set = 0             # index into REPLY_SETS (no longer cycled — Set key removed); default "select" 1/2/3/4
_activity = {}             # session id -> (label, needs_choice, rec_zone) from pane parsing
_needed_since = {}         # session id -> monotonic timestamp when first detected as needing input
_urgency = {}              # session id -> "menu" | "urgent" | "patient" (blink speed + focus)
# ponytail: separate dict rather than a 4th tuple element on _activity —
# avoids resizing every `_activity.get(sid, (None, False, None))` call site.
# Populated only when session_activity detects a live numbered menu; cleared
# on every other branch. Renderers read this to label row-1 reply keys.
_menu_opts = {}            # session id -> [str|None] * 4 (option labels for zones 0-3)
INPUT_TIMEOUT = 10.0       # seconds before text-input sessions slow-blink
# Animation: replaces the old _blink/_blink_slow booleans. _anim_phase is a
# monotonic seconds accumulator incremented by ANIM each render tick; each
# renderer derives its own 0..1 cycle phase from it, so all animations coexist
# without beating. _frame_cache skips re-pushing identical native frames so the
# 20fps loop doesn't flood HID for static slots (stopped/empty keys).
# ponytail: in-memory phase accumulator, no persist on SIGTERM — upgrade: save
# to ~/.local/state if resume-continuity ever matters.
_anim_phase = 0.0
_frame_cache = {}          # session id -> last native frame bytes (skip dup push)
_touch_frame_cache = None  # last native touchscreen frame bytes (skip dup push)
_ts_diag_logged = False    # one-shot diagnostic: log touchscreen image size on first render
_ts_send_logged = False    # one-shot diagnostic: log set_touchscreen_image call result
_manual_until = 0.0        # monotonic timestamp until which auto-focus is suppressed (knob 1 / sel keys)
# LCD panorama mode: "normal" = dark fill + 5 knob zones; "laputa" = Ghibli
# Siege panorama; "beach" = the city live-weather beach (sun position by
# local hour, conditions from NWS poll). Long-press key 7 cycles the order.
# ponytail: string enum over a class — keeps the 3-way branch readable at
# every callsite without a new dep; upgrade: enum.Enum if a 4th mode lands.
_lcd_mode = "beach"
# Auto-suggest dismissal: "Next" clears the input gate (stop blinking, drop
# from focus queue) WITHOUT sending keys to the agent. Time-based: holds until
# the agent goes busy again (spinner detected → rearm) or stops needing input.
# Content-fingerprint matching was too fragile — Claude Code's status footer
# ("· 1 shell · ← for agents") drifts every refresh, clearing the dismiss at
# once. Sticky-suggest bridges agent-deck's running↔waiting flicker so the
# "Next" label and a safe slot-2 press survive the noise.
# ponytail: _dismissed/_suggest_sticky/_urgency/_needed_since/_auto_restart_at are
# in-memory only (reset on service restart), unlike _win_map/_pane_order which
# persist — acceptable: transient UI state; upgrade: persist like _win_map if
# restart resets ever bite.
_dismissed = {}            # session id -> monotonic dismiss timestamp
_suggest_sticky = {}       # session id -> monotonic timestamp of last "suggest…" label
_pruned = {}               # session id -> monotonic timestamp (suppress re-prune noise)
_win_miss = {}             # session id -> consecutive confirmed-empty window polls
_WIN_MISS_THRESHOLD = 2    # confirmed misses needed before a window-close prune fires
DISMISS_TIMEOUT = 300.0    # safety max before a dismissed session auto-rearms
_last_input = 0.0          # monotonic time of last user input (for sleep timer)
_asleep = False            # True when the OLEDs are blanked

# Auto-remediation: when a session is in `error` state, verify the tool's
# prerequisites are met on the live environment and restart it. Cooldown per
# session prevents a restart loop; the slate clears when the session recovers,
# so a *new* error later gets a fresh attempt.
RESTART_COOLDOWN = 60      # seconds between auto-restart attempts for one session
_auto_restart_at = {}      # session id -> monotonic time the next retry is allowed

# Per-tool readiness probe (runs via the same SSH host the tool itself uses).
# Return 0 = environment is ready (restart is worth attempting).
# ponytail: hardcoded probes — upgrade: derive from tool command when it grows.
TOOL_READY = {
    "glm":    'test -f /opt/claude-glm/secrets && . /opt/claude-glm/secrets && test -n "$ZAI_API_KEY"',
    "local":  'curl -sf --max-time 3 http://localhost:11436/health >/dev/null 2>&1 || pgrep -x ollama >/dev/null 2>&1',
    "claude": 'which claude >/dev/null 2>&1',
    "gpt":    'which claude >/dev/null 2>&1',
}

def _tool_label_for(sess):
    """Map a session to its tool label by stripping dedup suffixes from the title
    (e.g. 'glm-2' -> 'glm'). Falls back to the raw title."""
    t = sess.get("title", "")
    return re.sub(r'-\d+$', '', t) if re.search(r'-\d+$', t) else t

def _env_ready(label):
    """True if the tool's prerequisites are met on the live environment."""
    probe = TOOL_READY.get(label)
    if not probe:
        return True                       # unknown tool — don't block a restart
    # Pass probe as a single ssh arg so the REMOTE shell expands any $vars
    # (a local `bash -c` wrapper mangled quoting and expanded $ZAI_API_KEY here).
    r = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4",
              SSH_HOST, probe], timeout=10)
    ok = bool(r and r.returncode == 0)
    if not ok:
        log("env check FAILED for '%s' — skipping auto-restart", label)
    return ok

def maybe_remediate(sessions):
    """Auto-restart sessions stuck in error state, gated by an environment probe
    and a per-session cooldown."""
    now = time.monotonic()
    for s in sessions:
        if s.get("status") != "error":
            if s["id"] in _auto_restart_at:
                del _auto_restart_at[s["id"]]   # recovered — reset the slate
            continue
        sid = s["id"]
        if now < _auto_restart_at.get(sid, 0):
            continue                               # still in cooldown
        label = _tool_label_for(s)
        if not _env_ready(label):
            _auto_restart_at[sid] = now + RESTART_COOLDOWN
            continue
        log("auto-restart errored '%s' (sid %s)", label, sid[:8])
        _run([AD, "session", "restart", sid], timeout=20)
        _auto_restart_at[sid] = now + RESTART_COOLDOWN

# Claude TUI scraping: working spinner ("✻ Vibing… (8m 23s)") and the arrow-nav
# permission menus ("❯ 1. Yes" / "Do you want to proceed?").
SPIN_RE = re.compile(r"[✻✢✶✳✽⋆✺✦✷✸✹*◉●○◐◑◒◓]\s+([A-Za-z][\w-]+?)…")
ELAPSED_RE = re.compile(r"\b(\d+m\s?\d+s|\d+m|\d+s)\b")
CHOICE_RE = re.compile(r"❯\s*\d+\.|Do you want to proceed|\b1\.\s+Yes\b")
# Recommended choice cursor: "❯ 2." — the option Claude Code highlights as the
# default. Group 1 is the option number (1-3 maps to touchscreen zones 0-2).
RECO_RE = re.compile(r"❯\s*(\d+)\.")
# Bare text-input prompt: "❯" at the start of a line (Claude Code's input
# cursor). Matched on the pane footer only, so it reflects the LIVE prompt and
# not a stale one in scrollback.
PROMPT_RE = re.compile(r"(?m)^\s*❯")
# Completion marker: "✻ Crunched for 1m 4s" / "◎ Sautéed for 1m 12s" — agent
# finished its turn and is at a bookmark/idle point (NOT asking a question).
DONE_RE = re.compile(r"[✻✢✶✳✽⋆✺✦✷✸✹*◉●○◐◑◒◓◎]\s+\S.*\bfor\b\s+\d+[ms]")
# Plain numbered prompt with NO ❯ cursor — non-shell agents and CLI subprompts
# that ask for a 1/2/3 choice without Claude Code's TUI chrome. The previous
# bare "line ends with : or ?" heuristic over-matched on agent prose (numbered
# lists + colons in normal explanations stole focus from sessions with real
# input demands). Tightened: the LAST non-empty line must START with a prompt
# keyword AND end with a prompt symbol, and a recent DONE_RE completion marker
# suppresses the whole branch (checked after DONE_RE in session_activity).
NUMBERED_RE = re.compile(r"(?m)^\s*[1-9]\.\s+\S")
# Menu option text extractor: "1. Yes" / "2. No" / "3. Don't ask again" lines
# near a live ❯ N. cursor. Used to surface option labels on the row-1 reply
# keys so the user sees "1 Yes / 2 No" instead of bare digits. Captures the
# option number (1-9) and the trailing label text. The leading [ \t❯>]*
# accepts the cursor char itself — Claude Code puts ❯ on the DEFAULT option's
# line ("❯ 1. Yes"), and without this the recommended option would never
# extract (which is exactly what glm-2 was hitting — its default is option 1).
MENU_OPT_RE = re.compile(r"(?m)^[ \t❯>]*([1-9])\.\s+(.+?)\s*$")
PROMPT_KW_RE = re.compile(
    r"(?i)^\s*(?:choose|select|enter|press|pick|option|input|reply|answer|your (?:choice|selection))\b[^:?\n]{0,40}[:?]\s*$")

# ---- plumbing -------------------------------------------------------------
def _run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        log("! %s failed: %s", " ".join(cmd), e); return None

def fetch_sessions():
    r = _run([AD, "list", "--json"], timeout=10)
    out = ((r.stdout if r else "") or "").strip()
    if not (out.startswith("[") or out.startswith("{")):
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    data = data if isinstance(data, list) else data.get("items", data.get("sessions", []))
    # Stream Deck button order mirrors actual on-screen Konsole layout:
    # left-to-right = window-open order, and within a window, top-to-bottom
    # (split) / left-to-right (tab) = placement order, both tracked live in
    # _pane_order as we place each session (see _record_pane). Sessions with
    # no recorded placement yet (e.g. just spawned, window not resolved)
    # sort to the end by creation time.
    with _lock:
        win_index = {pid: i for i, pid in enumerate(_pane_order.keys())}
        pane_rank = {sid: (win_index[pid], j)
                     for pid, sids in _pane_order.items()
                     for j, sid in enumerate(sids)}
    unranked = (len(win_index), 0)
    data.sort(key=lambda s: (pane_rank.get(s.get("id"), unranked), s.get("created_at", "")))
    return data

def _prune_dead(sessions):
    """Remove sessions whose backend has died. Two signals:
      1. tmux session gone entirely (user killed it)
      2. SSH-tunnel session whose remote process exited — the pane fell back
         from 'ssh' to 'bash' (shows 'Connection to ... closed' on screen).
    agent-deck's list keeps showing dead sessions with stale status.
    One batched `tmux list-panes -a` call checks all sessions at once."""
    tmux_sess = {s.get("tmux_session"): s for s in sessions
                 if s.get("tmux_session")}
    if not tmux_sess:
        return sessions
    r = _run(["tmux", "list-panes", "-a", "-F",
              "#{session_name}\t#{pane_current_command}\t#{pane_dead}"],
             timeout=5)
    if not r or r.returncode != 0:
        return sessions                        # tmux server down — can't check
    pane_info = {}                             # session_name -> (cmd, pane_dead)
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            pane_info[parts[0]] = (parts[1], parts[2])
    dead_ids = set()
    now = time.monotonic()
    for tname, s in tmux_sess.items():
        sid = s["id"]
        # Skip sessions we already pruned recently (agent-deck's remove may
        # take a couple of poll cycles to clear from list --json).
        if sid in _pruned:
            dead_ids.add(sid)
            continue
        if tname not in pane_info:
            dead_ids.add(sid)
            log("prune dead '%s' (tmux '%s' gone)", s.get("title"), tname)
            continue
        cmd, pane_dead = pane_info[tname]
        is_ssh = s.get("command", "").lstrip().startswith("ssh")
        # SSH-tunnel session dies when pane falls back from 'ssh' to anything
        # else (usually 'bash') — catches 'Connection to ... closed'.
        # Whitelist tool executable names: pane_current_command can show the
        # deep foreground (e.g. 'claude') on some tmux/kernel combinations even
        # while the outer ssh is alive — don't false-positive those as dead.
        _ALIVE_CMDS = frozenset(["ssh"] + [t[0] for t in TOOLS])
        if is_ssh and (pane_dead == "1" or cmd not in _ALIVE_CMDS):
            dead_ids.add(sid)
            log("prune dead '%s' (SSH closed, pane now %s/%s)",
                s.get("title"), cmd, pane_dead)
    # Third signal: tracked Konsole window was closed (user closed the terminal).
    # When the Konsole PID in _win_map has no X windows left, the terminal is gone.
    # We stop+remove the session so it clears off the Stream Deck immediately.
    for s in sessions:
        sid = s["id"]
        if sid in dead_ids or sid in _pruned:
            continue
        with _lock:
            konsole_pid = _win_map.get(sid)
        if not konsole_pid:
            continue
        wins = _windows_of_pid(konsole_pid)
        if wins is None:
            # Query failed (e.g. transient X BadWindow error) — unknown state,
            # NOT evidence the window closed. Leave the miss counter as-is so a
            # genuine close isn't masked by one flaky poll in the middle of it.
            continue
        if wins:
            _win_miss.pop(sid, None)
            continue
        misses = _win_miss.get(sid, 0) + 1
        _win_miss[sid] = misses
        if misses < _WIN_MISS_THRESHOLD:
            continue
        dead_ids.add(sid)
        _win_miss.pop(sid, None)
        with _lock:
            _win_map.pop(sid, None)
        _save_win_map()
        log("prune dead '%s' (konsole pid %s window closed)", s.get("title"), konsole_pid)
    # Signal 4: tracked Konsole D-Bus session path no longer exists in
    # sessionList. Catches per-tab close: closing one tab in a multi-tab
    # window leaves the konsole PROCESS alive (other tabs hold it open), so
    # Signal 3's whole-PID window check misses it. The D-Bus ksid is per-tab
    # — if it's gone from sessionList, that specific tab was closed (user
    # closed it, even though tmux + ssh underneath kept running). Batched:
    # one qdbus call per unique konsole PID.
    _dbus_live = {}                     # svc -> (ok, set-of-ksids) per prune
    for s in sessions:
        sid = s["id"]
        if sid in dead_ids or sid in _pruned:
            continue
        ksid_tracked = _dbus_map.get(sid)
        if not ksid_tracked:
            continue                    # new-window session — Signal 3 covers it
        pid = _win_map.get(sid)
        if not pid:
            continue
        svc = "org.kde.konsole-%s" % pid
        if svc not in _dbus_live:
            ok, out = _xrun_checked(
                ["qdbus", svc, "/Windows/1", "org.kde.konsole.Window.sessionList"])
            _dbus_live[svc] = (ok, {p.strip() for p in out.split() if p.strip()})
        ok, live = _dbus_live[svc]
        if ok and ksid_tracked not in live:
            dead_ids.add(sid)
            log("prune dead '%s' (konsole D-Bus session %s gone)",
                s.get("title"), ksid_tracked)
    if not dead_ids:
        return sessions
    for sid in dead_ids:
        if sid not in _pruned:
            _run([AD, "session", "stop", sid], timeout=10)
            _run([AD, "session", "remove", sid, "--force"], timeout=10)
            _pruned[sid] = now
            log("prune: stop+remove %s", sid[:8])
        _activity.pop(sid, None)
        _urgency.pop(sid, None)
        _needed_since.pop(sid, None)
        _dismissed.pop(sid, None)
        _suggest_sticky.pop(sid, None)
        _dbus_map.pop(sid, None)
        _save_dbus_map()
        _forget_pane(sid)
    # Expire stale prune cache entries (session could be re-created with same id)
    for sid in list(_pruned):
        if sid not in {s["id"] for s in sessions} and now - _pruned[sid] > 60:
            del _pruned[sid]
    return [s for s in sessions if s["id"] not in dead_ids]

def tmux_send(sess, keys):
    """Send tmux key tokens to the session's pane. Supports a pause token
    "~N" (sleep N seconds) between key chunks — needed for sequences like
    Tab-then-Enter where the TUI needs time to register the auto-fill."""
    t = sess.get("tmux_session")
    if not t:
        log("no tmux_session for %s", sess.get("title")); return
    chunk = []
    for k in keys:
        if k.startswith("~"):
            if chunk:
                _run(["tmux", "send-keys", "-t", t, *chunk], timeout=8)
                log("send-keys -> %s : %s", t, " ".join(chunk))
                chunk = []
            time.sleep(float(k[1:]))
        else:
            chunk.append(k)
    if chunk:
        _run(["tmux", "send-keys", "-t", t, *chunk], timeout=8)
        log("send-keys -> %s : %s", t, " ".join(chunk))

def tmux_send_text(sess, text):
    """Send literal text (no key interpretation) into the session's tmux pane."""
    t = sess.get("tmux_session")
    if not t:
        log("no tmux_session for %s", sess.get("title")); return
    _run(["tmux", "send-keys", "-t", t, "-l", text], timeout=8)
    log("send-text -> %s : %d chars", t, len(text))

def _fire_action(sess, spec):
    """Dispatch a control-key spec against `sess`. Returns True if the caller
    should advance focus to sess (tmux/slash classes — user intent is to land
    back in the session); False for voice/shell which don't target the pane.

    M-SD1: consolidates the old ad-hoc dispatch in on_key_xl (the "!voice"
    special case, the "!<cmd>" shell prefix, and plain tmux_keys) into a
    spec-driven dispatcher so new control classes (slash_command in M-SD2,
    long_press in M-SD3) can be added without touching the key handler."""
    cls = spec.get("class")
    if cls == "tmux_keys":
        tmux_send(sess, spec["keys"])
        return True
    if cls == "slash_command":
        # Fire "/<cmd> " (trailing space so the CLI registers the command name
        # as complete — required for multi-word commands like "/goal complete"
        # and for the CLI's command parser to recognize the boundary).
        tmux_send_text(sess, "/" + spec["cmd"] + " ")
        tmux_send(sess, ["Enter"])
        return True
    if cls == "shell":
        _run(["bash", "-lc", spec["cmd"]])
        return False
    if cls == "voice":
        _bg(_voice_toggle, sess)
        return False
    log("unknown control class: %r", cls)
    return False

def _voice_toggle(sess):
    """Voice dictation via the-deck-host (mic is there). First press starts recording;
    second press stops, transcribes on the-deck-host, and injects the result into the
    session via tmux send-keys -l (no Enter, so the user reviews before submit)."""
    ssh = ["ssh", "-o", "ConnectTimeout=5", SSH_HOST]
    # Are we currently recording? (PID file exists on the-deck-host)
    r = _run(ssh + ["test -f /tmp/voice-glm-rec.pid"], timeout=8)
    if r and r.returncode != 0:
        # Not recording — start
        _run(ssh + ["~/.local/bin/voice-glm.sh"], timeout=10)
        log("voice: recording started on %s", SSH_HOST); return
    # Recording — stop + transcribe (konsole-send will fail harmlessly on the-deck-host;
    # the transcript is written before that call)
    _run(ssh + ["~/.local/bin/voice-glm.sh"], timeout=90)
    r = _run(ssh + ["cat /tmp/voice-glm-transcript.txt"], timeout=8)
    text = (r.stdout.strip() if r else "")
    if text:
        tmux_send_text(sess, text)
    else:
        log("voice: empty transcript")

def _extract_menu_opts(pane, cursor_match):
    """Extract up to 4 numbered-option labels surrounding a live ❯ N. cursor.
    Returns a 4-element list (str|None) padded with None for missing slots, or
    None if no option lines were found. Used to label the row-1 reply keys with
    the actual menu text ("1 Yes / 2 No") instead of bare digits. Window is
    ±400 chars around the cursor — covers Claude Code's typical 4-option block
    without pulling in scrollback from earlier prompts."""
    start = max(0, cursor_match.start() - 400)
    end = min(len(pane), cursor_match.end() + 400)
    window = pane[start:end]
    opts = [None, None, None, None]
    for m in MENU_OPT_RE.finditer(window):
        n = int(m.group(1))
        if 1 <= n <= 4:
            # Clip to 18 chars so "1 Don't ask again" fits the XL tile at 13pt.
            opts[n - 1] = m.group(2).strip()[:18]
    return opts if any(opts) else None

def session_activity(sess):
    """Scrape the session's pane -> (label, needs_choice, rec_zone). label is
    the live action (e.g. 'Vibing 8m23s' while thinking, 'choose…' at a prompt)
    else the agent-deck status. needs_choice=True means blink for user input.
    rec_zone is the touchscreen zone index (0-2) Claude Code's ❯ cursor marks as
    the recommended pick in a numbered menu, else None. agent-deck can't track
    shell-tool activity (oc-start, plain shells), so also scrape idle shells.

    Side effect: populates _menu_opts[sid] when a live numbered menu is
    detected, so reply-key renderers can label themselves with the actual
    option text ("1 Yes / 2 No"). Cleared on every non-menu path."""
    sid = sess.get("id")
    _menu_opts.pop(sid, None)   # default: no menu — only the live-menu branch sets it
    st = sess.get("status", "idle")
    t = sess.get("tmux_session")
    tool = sess.get("tool", "")
    # Scrape when agent-deck reports an active state, OR for shell-tool sessions
    # where agent-deck has no visibility (status stuck at 'idle' while running).
    scrape = st in ("running", "starting", "waiting") or (
        st == "idle" and tool == "shell" and bool(t))
    if not t or not scrape:
        return (st, False, None)
    r = _run(["tmux", "capture-pane", "-p", "-t", t, "-S", "-20"], timeout=5)
    pane = (r.stdout if r else "") or ""
    # The footer = the live state (last 10 lines). Checking it here instead of
    # the full 20-line scrollback means stale completion markers / old menus
    # higher up can't mask the prompt that's actually on screen right now.
    footer = "\n".join(pane.splitlines()[-10:])
    # Spinner anywhere in the pane = agent is actively working, regardless of
    # what agent-deck reports (it flickers running↔waiting mid-turn). Never
    # blink a spinning pane. A spinner also means any prior "Next" dismissal is
    # stale (the agent acted) → rearm so the next prompt blinks normally.
    # (Safe to scan the full pane: a completed turn's marker has no trailing
    # '…', so SPIN_RE won't false-match it.)
    m = SPIN_RE.search(pane)
    if m:
        _dismissed.pop(sess.get("id"), None)
        el = ELAPSED_RE.search(pane)
        return ("%s %s" % (m.group(1), el.group(1)) if el else m.group(1), False, None)
    # Pane-driven prompt detection. agent-deck's status is NOT trusted here —
    # it reports "running" for Claude Code sessions sitting at an idle ❯ prompt,
    # which would otherwise fall through to "thinking" and hide the prompt from
    # the board entirely (the cause of "Next" not registering on claude-2).
    # Order matters: menu → completion → bare prompt.
    # Numbered permission menu ("Do you want to proceed? ❯ 1. Yes"). Claude Code
    # renders a multi-line task-list widget BELOW the menu, which pushes the ❯
    # cursor outside the 10-line footer — so glm-2 (which shows its todo list)
    # was never recognized as needing input. Scan the FULL pane for the LAST menu
    # cursor (RECO_RE = "❯ N."), not just the footer. Stale (already-answered)
    # menus are suppressed: if a completion marker (DONE_RE) or a newer prompt
    # (PROMPT_RE) appears below the cursor, the agent already moved on. Only the
    # task-widget / help chrome sits below a LIVE menu, and those match neither.
    menu_hits = list(RECO_RE.finditer(pane))
    if menu_hits:
        below = pane[menu_hits[-1].end():]
        if not (DONE_RE.search(below) or PROMPT_RE.search(below)):
            n = int(menu_hits[-1].group(1))
            rec = (n - 1) if 1 <= n <= 2 else None
            opts = _extract_menu_opts(pane, menu_hits[-1])
            if opts:
                _menu_opts[sid] = opts
            return ("choose…", True, rec)
    # Find the LAST ❯ in the footer — only the live prompt matters. Scrollback
    # ❯ lines (old commands like "❯ /clear", previous turns) sit ABOVE the live
    # prompt and would false-trigger on .search() (first match). Also require
    # non-whitespace text after the live ❯: an EMPTY ❯ (user idle, nothing
    # typed, no auto-suggest ghost) is NOT an input demand — it's just "waiting
    # for a new task". glm-2 sits at an empty ❯ and was stealing focus from
    # claude-2 / claude-glm(2) which have real ❯ <text> prompts.
    prompt_matches = list(PROMPT_RE.finditer(footer))
    if prompt_matches:
        after_line = footer[prompt_matches[-1].end():].split("\n", 1)[0]
        # Real auto-suggest / ghost text is IMMEDIATELY after ❯ (column 1-4).
        # The Knurl duck ASCII art sits at column ~100 — all whitespace between
        # the cursor and the art. Only treat near-text as input demand. Without
        # this, the duck art false-triggers "suggest…" and locks the session
        # in the focus queue permanently, blocking other sessions.
        if after_line[:5].strip():
            # Bare ❯ text prompt with content — auto-suggest ghost text or
            # free-text input. Checked BEFORE DONE_RE because Claude Code shows
            # "✻ Cooked for 2m" directly above the ❯ after every turn; if
            # DONE_RE ran first it would mask the live prompt. "Go" zone accepts
            # the suggestion; "Next" dismisses the gate.
            return ("suggest…", True, 3)
        # Empty live ❯ → session is idle, waiting for a new task. Fall through
        # to DONE_RE / idle so it does NOT enter the auto-focus queue.
    if DONE_RE.search(footer):
        # Recent completion marker with NO ❯ prompt below (e.g. raw output
        # pane, or a TUI that hasn't repainted its prompt yet) → between-turns
        # idle, don't blink.
        return ("idle", False, None)
    # Plain numbered prompt with no ❯ cursor (no spinner, no menu, no bare ❯,
    # no recent completion). Run AFTER DONE_RE so a finished turn's numbered
    # output (e.g. "1. did X  2. did Y  ◎ for 1m") can't trigger a false input
    # demand. The last non-empty line must start with a prompt keyword and end
    # with : or ?, AND numbered items must be present — this is what keeps
    # agent prose from stealing focus from sessions with real prompts.
    tail = [ln for ln in footer.splitlines() if ln.strip()]
    if (tail and NUMBERED_RE.search(footer) and PROMPT_KW_RE.match(tail[-1])):
        return ("input…", True, None)
    # Shell-tool idle without a spinner: detect when opencode is ASKING a
    # question (not merely done with its turn). Heuristics, in order:
    #   - ◎ ... for <elapsed>  = just-finished completion marker → idle, no blink
    #   - otherwise, a '?' in the pane = agent asked a question → blink
    # The [oc] footer alone is NOT enough — it's present in every oc state.
    if st == "idle" and tool == "shell" and re.search(r"\[oc\]\s+\d+:", pane):
        if re.search(r"◎\s+\S.*\bfor\b\s+\d+[ms]", pane):
            return ("idle", False, None)        # turn done, awaiting next instruction
        if "?" in pane:
            return ("input…", True, None)       # agent asked a question → blink
        return ("idle", False, None)            # plain oc prompt, no question
    # Shell-tool idle that doesn't match a spinner is genuinely idle (prompt
    # visible). Other active states without a spinner show as 'thinking'.
    return ("thinking" if st in ("running", "starting") else st, False, None)

# --- konsole control via D-Bus: tab/split happen INSIDE the focused window ----
def _dbus_env():
    env = os.environ.copy(); env.setdefault("DISPLAY", ":0"); return env

def _qdbus(*args, timeout=6):
    try:
        return subprocess.check_output(["qdbus", *args], env=_dbus_env(),
                                       text=True, timeout=timeout).strip()
    except Exception as e:
        log("qdbus %s failed: %s", " ".join(args), e); return ""

def _konsole_services():
    return re.findall(r"org\.kde\.konsole-\d+", _qdbus())

def _focused_konsole():
    """D-Bus service of the konsole window the user is focused on, or None."""
    if not (shutil.which("qdbus") and shutil.which("xdotool")):
        return None
    try:
        pid = subprocess.check_output(["xdotool", "getactivewindow", "getwindowpid"],
                                      env=_dbus_env(), text=True, timeout=5).strip()
        svc = "org.kde.konsole-%s" % pid
        if svc in _konsole_services():
            return svc
    except Exception:
        pass
    return None

def _agentdeck_konsole():
    """Return the Konsole D-Bus service that hosts tracked agent-deck sessions
    (i.e. one of the PIDs in _win_map still has an X window open).
    Preferred over _focused_konsole() for splits so we land in the right window
    even when the user has their own terminals in focus."""
    with _lock:
        pids = list(_win_map.values())
    svcs = set(_konsole_services())
    for pid in pids:
        svc = "org.kde.konsole-%s" % pid
        if svc in svcs and _windows_of_pid(pid):
            return svc
    return None

def _any_konsole():
    """A konsole service to reuse when none is focused. Prefers the service of a
    VISIBLE konsole window, else the first registered service. Returns None only
    when no konsole is running at all. Used as the split/tab fallback so a press
    while focus is elsewhere lands inside an existing window instead of spawning
    a new one."""
    svcs = _konsole_services()
    if not svcs:
        return None
    if shutil.which("xdotool"):
        for w in _xrun(["xdotool", "search", "--onlyvisible", "--class", "konsole"]).split():
            pid = _xrun(["xdotool", "getwindowpid", w]).strip()
            svc = "org.kde.konsole-%s" % pid
            if svc in svcs:
                return svc
    return svcs[0]

def _session_list(svc):
    return [x for x in _qdbus(svc, "/Windows/1",
                              "org.kde.konsole.Window.sessionList").split() if x.strip()]

def _run_in_session(svc, sid, cmd):
    _qdbus(svc, "/Sessions/%s" % sid, "org.kde.konsole.Session.runCommand", cmd)

def _tmux_client_count():
    """Total attached tmux clients across all sessions. Used to confirm a split
    pane's `agent-deck session attach` actually bound a terminal."""
    # ponytail: global count, not per-session — fine for sequential interactive
    # placement; upgrade: match the specific tmux_session if concurrent splits race
    r = _run(["tmux", "list-clients"], timeout=4)
    return len(r.stdout.splitlines()) if (r and r.returncode == 0) else 0

def _clients_on(tmux):
    """Set of tmux client names attached to session `tmux` (empty if tmux is None
    or unreachable). Per-session, unlike _tmux_client_count's global total — so a
    placement can prove IT bound the right session, not just any session. This is
    the precise signal that kills the mirrored-split false positive.
    Lists ALL clients with their session name and filters in Python: tmux 3.4's
    `list-clients -t <session>` returns empty even for attached sessions."""
    if not tmux:
        return set()
    r = _run(["tmux", "list-clients", "-F", "#{session_name}\t#{client_name}"], timeout=4)
    if not (r and r.returncode == 0):
        return set()
    out = set()
    for ln in r.stdout.splitlines():
        parts = ln.split("\t", 1)
        if len(parts) == 2 and parts[0] == tmux:
            out.add(parts[1])
    return out

def _close_session(svc, ksid):
    """Tear down an orphan split pane. Konsole exposes no D-Bus close method.
    Sending bare `exit` only works when the pane's SHELL is foreground — if a
    lingering `agent-deck session attach` holds the pane, the text is typed
    INTO the live agent instead (observed 2026-07-04: mirror pane survived,
    `exit` landed inside the mirrored session). So: SIGTERM whatever is
    foreground until the shell is, then `exit`, then verify the session left."""
    if not ksid:
        return
    path = "/Sessions/%s" % ksid
    shell_pid = _qdbus(svc, path, "org.kde.konsole.Session.processId").strip()
    for _ in range(10):
        fg = _qdbus(svc, path, "org.kde.konsole.Session.foregroundProcessId").strip()
        if not fg or fg in ("0", shell_pid):
            break
        try:
            os.kill(int(fg), signal.SIGTERM)
        except (OSError, ValueError):
            break
        time.sleep(0.2)
    _qdbus(svc, path, "org.kde.konsole.Session.sendText", "exit\n")
    time.sleep(0.3)
    if ksid in _session_list(svc):
        log("close_session: pane %s in %s did not close", ksid, svc)

def _xrun(args, timeout=5):
    return _xrun_checked(args, timeout)[1]

def _konsole_windows():
    return set(_xrun(["xdotool", "search", "--class", "konsole"]).split())

def _xrun_checked(args, timeout=5):
    """Like _xrun but reports whether the query itself succeeded, distinct from
    succeeding-with-no-output. Needed because xdotool can crash mid-query on a
    transient X protocol error (e.g. BadWindow on a window torn down by the WM
    while being enumerated) — that failure must not look identical to 'this
    window genuinely doesn't exist'."""
    try:
        r = subprocess.run(args, env=_dbus_env(), capture_output=True, text=True, timeout=timeout)
        return (True, r.stdout) if r.returncode == 0 else (False, "")
    except Exception:
        return (False, "")

def _windows_of_pid(pid):
    """Live konsole window id(s) belonging to konsole process `pid`. None if a
    query failed (unknown — callers must not treat this as 'closed'). Empty
    list only when both queries succeeded and genuinely found nothing."""
    if not pid:
        return []
    # If the konsole PROCESS itself is dead, the window is definitively gone.
    # xdotool exits 1 for BOTH "no matches" AND "query failed" (can't-open-
    # display, transient BadWindow) — same exit code, opposite meaning. So a
    # dead pid's `search --pid` looks identical to a flaky poll, returns None,
    # and `_prune_dead` skips it forever ("if wins is None: continue") — the
    # stale-session-on-deck bug. os.kill(0) is authoritative and needs no X
    # round-trip, resolving the ambiguity for the common dead-process case.
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return []                            # process gone -> window gone
    ok1, res = _xrun_checked(["xdotool", "search", "--pid", str(pid)])
    ok2, kons = _xrun_checked(["xdotool", "search", "--class", "konsole"])
    if not (ok1 and ok2):
        return None
    konsole = set(kons.split())
    return [w for w in res.split() if w in konsole]

def _window_visible(wid):
    return wid in _xrun(["xdotool", "search", "--onlyvisible", "--class", "konsole"]).split()

def _focus_konsole_window(svc):
    """Activate the Konsole window for `svc` so it holds desktop input focus.
    Konsole's split-view action only moves currentSession to the new pane — and
    the setCurrentSession nudge in place_konsole only sticks — when the window
    is focused. Deck-triggered splits arrive with focus elsewhere, so without
    this currentSession drifts back to the original pane ('focus drifted from N
    to M before inject') and the split bails to a fresh window. Activating
    first makes the split's own focus change hold. Idempotent and safe under
    the lock-free xdotool path; a failed activate just leaves focus as-is and
    the existing nudge/window-fallback still apply."""
    try:
        pid = int(svc.rsplit("-", 1)[-1])
    except ValueError:
        return
    wins = _windows_of_pid(pid)
    if not wins:                                 # None (query fail) or [] (no window)
        return
    wid = next((w for w in wins if _window_visible(w)), wins[0])
    _xrun(["xdotool", "windowactivate", "--sync", wid], timeout=3)

def focus_active_terminal():
    """Raise + focus the active session's konsole window. XL+ knob 1.
    Same xdotool/wmctrl path toggle_or_place uses on second tap."""
    sid = _active_id
    if not sid:
        log("focus-terminal: no active session"); return
    with _lock:
        pid = _win_map.get(sid)
    wins = _windows_of_pid(pid)
    if not wins:
        log("focus-terminal: no window for sid=%s", sid); return
    wid = next((w for w in wins if _window_visible(w)), wins[0])
    _xrun(["wmctrl", "-i", "-a", wid])
    _xrun(["xdotool", "windowactivate", "--sync", wid], timeout=3)
    log("focus-terminal: raised wid=%s for sid=%s", wid, sid)

def _new_window(cmd, sid=None):
    env = _dbus_env()
    # stderr inherited (not DEVNULL): Konsole/Qt D-Bus warnings need to land in
    # `journalctl -u streamdeck-agentdeck` instead of vanishing, so the next
    # on-screen warning is diagnosable without a human transcribing a popup.
    if not shutil.which("konsole"):
        subprocess.Popen(["xterm", "-e", "bash", "-lc", "%s; exec bash" % cmd], env=env,
                         stdout=subprocess.DEVNULL)
        log("opened new xterm window"); return
    before = _konsole_windows()
    subprocess.Popen(["konsole", "-e", "bash", "-lc", "%s; exec bash" % cmd], env=env,
                     stdout=subprocess.DEVNULL)
    # track the konsole PROCESS pid (not the reusable X window id) so a later tap
    # can re-resolve the window — survives X id reuse, detects a real close.
    if sid and shutil.which("xdotool"):
        # ponytail: 0.2s×20 poll to catch the new window id — upgrade: python-xlib
        # substructure-notify events if polling ever misses slow-starting windows.
        for _ in range(20):
            time.sleep(0.2)
            new = _konsole_windows() - before
            if new:
                wid = sorted(new)[-1]
                pid = _xrun(["xdotool", "getwindowpid", wid]).strip()
                with _lock:
                    _win_map[sid] = pid
                    _save_win_map()
                _record_pane(pid, sid)
                log("opened+tracked konsole pid %s (win %s) for session %s",
                    pid, wid, sid[:8]); return
    log("opened new konsole window")

def place_konsole(cmd, mode, sid=None, tmux=None):
    """cmd runs in the chosen placement. window -> fresh konsole (tracked by sid);
    tab/split -> inside the focused konsole window via D-Bus (falls back to a window).
    `tmux` is this session's tmux_session name — when known, split placement verifies
    a client binds THAT exact session (not a global count), killing mirror false positives."""
    if not _dbus_env().get("DISPLAY"):
        log("no DISPLAY; cannot open terminal"); return
    if mode == "window":
        _new_window(cmd, sid=sid); return
    # For splits/tabs, prefer the Konsole that's already hosting agent-deck
    # sessions (tracked in _win_map) over whatever happens to be focused.
    # _focused_konsole() grabs the user's own terminal when it has focus,
    # causing splits to land in the wrong window.
    svc = _agentdeck_konsole() or _focused_konsole() or _any_konsole()
    if not svc:
        log("no konsole running for %s; opening new window", mode)
        _new_window(cmd, sid=sid); return
    if mode == "tab":
        ksid = _qdbus(svc, "/Windows/1", "org.kde.konsole.Window.newSession")
        if ksid:
            _run_in_session(svc, ksid, cmd); log("tab in %s session %s", svc, ksid)
            if sid:
                pid = svc.rsplit("-", 1)[-1]
                with _lock:
                    _win_map[sid] = pid; _save_win_map()
                    _dbus_map[sid] = ksid; _save_dbus_map()
                _record_pane(pid, sid)
        else:
            _new_window(cmd, sid=sid)
        return
    # split: fire the split action, then run cmd in the VISIBLE focused split pane.
    #
    # History of bugs in this path:
    #  - 02a7cc9: fixed "never fall back to currentSession" (mirror via fallback)
    #  - 565ed88: added session-list detection + per-session tmux verify
    #  - THIS FIX: 565ed88 was still mirroring because `sessionList` returns a
    #    background D-Bus session (session 2) that Konsole creates as a ghost entry,
    #    NOT the visible split pane. The visible pane keeps showing session 1 (claude).
    #    Root cause: `split-view-top-bottom` puts the new pane in focus (currentSession
    #    changes) but the session that receives `runCommand` was the ghost session,
    #    not the focused one. Fix: use currentSession() AFTER split to target the
    #    focused visible pane. Ignore sessionList ghosts.
    _focus_konsole_window(svc)   # split's currentSession change only sticks when the window has desktop focus
    action = "split-view-left-right" if mode == "split-right" else "split-view-top-bottom"
    before = set(_session_list(svc))
    before_current = _qdbus(svc, "/Windows/1",
                            "org.kde.konsole.Window.currentSession").strip()
    c0 = _clients_on(tmux)                       # per-session clients before split
    g0 = _tmux_client_count()                    # global fallback when tmux name unknown
    _qdbus(svc, "/konsole/MainWindow_1", "org.kde.KMainWindow.activateAction", action)

    # Wait for currentSession() to change — that signals the split pane got focus.
    # Only accept a session that is genuinely NEW (not already in `before`).
    # sessionList ghosts (background D-Bus sessions the visible pane never shows)
    # are excluded because the split action only focuses VISIBLE panes.
    ksid = None
    for _ in range(25):                          # up to ~2.5s
        time.sleep(0.1)
        after_current = _qdbus(svc, "/Windows/1",
                               "org.kde.konsole.Window.currentSession").strip()
        if after_current and after_current != before_current and after_current not in before:
            ksid = after_current
            log("split: focused pane is session %s (currentSession changed)", ksid)
            break

    if not ksid:
        # currentSession didn't move to a new session.  Two sub-cases:
        # (a) split created no new session at all (duplicate view) — open window
        # (b) Konsole version keeps focus on original pane after split — try sessionList
        fresh = set(_session_list(svc)) - before
        if fresh:
            ksid = sorted(fresh)[0]
            log("split: currentSession unchanged; using sessionList session %s", ksid)
        else:
            log("split spawned no new session; opening window")
            _new_window(cmd, sid=sid); return

    time.sleep(0.3)                              # let the new pane's shell settle
    # Re-verify focus is STILL on ksid right before injecting. currentSession()
    # was read up to ~0.3s ago; if real input focus regressed back to the
    # original pane in that window (observed: command text landing in the
    # OLD session's prompt instead of the new one), runCommand would inject
    # into whatever Konsole actually considers focused now, not the stale
    # ksid we captured earlier. Abort to a safe fresh window rather than risk
    # typing the attach command into someone else's live session.
    refocus = _qdbus(svc, "/Windows/1", "org.kde.konsole.Window.currentSession").strip()
    if refocus != ksid:
        # Konsole doesn't move currentSession on split when the window itself
        # lacks input focus (deck spawns arrive with desktop focus anywhere),
        # which made the sessionList-fallback branch above unreachable — it
        # always tripped this guard. Nudge focus onto the new pane explicitly;
        # a ghost sessionList entry has no view, so setCurrentSession no-ops
        # and we still bail to a fresh window below.
        _qdbus(svc, "/Windows/1", "org.kde.konsole.Window.setCurrentSession", ksid)
        for _ in range(5):
            time.sleep(0.2)
            refocus = _qdbus(svc, "/Windows/1",
                             "org.kde.konsole.Window.currentSession").strip()
            if refocus == ksid:
                log("split: focus nudged onto session %s via setCurrentSession", ksid)
                break
    if refocus != ksid:
        log("split: focus drifted from %s to %s before inject; closing pane, opening window",
            ksid, refocus or "?")
        _close_session(svc, ksid); _new_window(cmd, sid=sid); return
    _run_in_session(svc, ksid, cmd)

    def _bound():
        if tmux:
            return bool(_clients_on(tmux) - c0)  # a NEW client on THIS session
        return _tmux_client_count() > g0         # fallback: any new client (less precise)

    for _ in range(50):                          # up to ~5s — cold agent start / ssh hop can exceed 2.5s
        time.sleep(0.1)
        if _bound():
            break
    else:
        log("split didn't bind %s; opening window", tmux or (sid[:8] if sid else "session"))
        _close_session(svc, ksid); _new_window(cmd, sid=sid); return
    time.sleep(0.8)                              # hold gate: must still be bound 0.8s later
    if _bound() and ksid in _session_list(svc):
        log("split %s in %s session %s (held on %s)", mode, svc, ksid, tmux or "tmux")
        if sid:
            pid = svc.rsplit("-", 1)[-1]
            with _lock:
                _win_map[sid] = pid; _save_win_map()
                _dbus_map[sid] = ksid; _save_dbus_map()
            _record_pane(pid, sid)
        return
    log("split didn't hold for %s; closing pane %s, opening window",
        tmux or (sid[:8] if sid else "session"), ksid)
    _close_session(svc, ksid); _new_window(cmd, sid=sid)

# ---- actions --------------------------------------------------------------
def _bg(fn, *a, **kw):
    threading.Thread(target=fn, args=a, kwargs=kw, daemon=True).start()

def active_session():
    with _lock:
        return next((s for s in _sessions if s.get("id") == _active_id), None)

def _active_menu_opts():
    """Menu option labels for the active session (4-element list of str|None),
    or None when the active session is not at a numbered menu. Reply-key
    renderers call this to label row-1 keys with the actual menu text."""
    sid = _active_id
    return _menu_opts.get(sid) if sid else None

def _host_status_loop():
    """Background ping loop: refresh _host_status every 2s."""
    while True:
        for h in list(_host_status.keys()):
            try:
                subprocess.run(["timeout", "1", "ping", "-c1", "-W1", h],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=2)
                with _host_status_lock: _host_status[h] = True
            except Exception:
                with _host_status_lock: _host_status[h] = False
        time.sleep(2)

# ---- weather + grilling (NWS api.weather.gov, free, no key) ----------------
def _nws_get(url):
    """NWS GET with User-Agent + gzip. Returns parsed JSON dict, or raises."""
    req = urllib.request.Request(url, headers={
        "User-Agent": NWS_USER_AGENT,
        "Accept-Encoding": "gzip",                 # ponytail: opt-in — NWS hourly ~30-80KB; 5-10x wire savings
        "Accept": "application/geo+json",
    })
    with urllib.request.urlopen(req, timeout=WEATHER_TIMEOUT_SEC) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw)

_ALERT_NOGO = {"Moderate", "Severe", "Extreme"}    # ponytail: belt-and-suspenders with per-condition thresholds
# ponytail: light rain OK — match only on these (NOT "rain"/"shower"/"drizzle" alone)
_NOGO_WORDS = ("thunderstorm", "tstm", "lightning", "heavy rain", "downpour", "heavy shower", "squall")

def _decide_grill(periods, alerts):
    """Pure decision: given NWS hourly periods + active alerts, return (ok, reason).
    Reasons (precedence ALERT > TSTM > RAIN > WIND > HEAT — most-dangerous first
    since the key shows one code). Light rain / drizzle / showers are explicitly OK."""
    reasons = []
    for p in periods[:GRILL_HORIZON_HOURS]:
        text = ((p.get("shortForecast") or "") + " " + (p.get("detailedForecast") or "")).lower()
        if any(w in text for w in _NOGO_WORDS):
            if any(t in text for t in ("thunderstorm", "tstm", "lightning")):
                reasons.append("TSTM")
            else:
                reasons.append("RAIN")
        temp = p.get("temperature")
        if isinstance(temp, (int, float)) and temp >= GRILL_HEAT_F:
            reasons.append("HEAT")
        ws = p.get("windSpeed") or ""
        nums = re.findall(r"\d+", ws)
        if nums and int(nums[-1]) >= GRILL_WIND_MPH:    # upper bound of "10 to 15 mph"
            reasons.append("WIND")
        wgust = p.get("windGust") or ""
        gnums = re.findall(r"\d+", wgust)
        if gnums and int(gnums[-1]) >= GRILL_GUST_MPH:
            reasons.append("WIND")
    for a in alerts:
        if a.get("severity") in _ALERT_NOGO:
            reasons.append("ALERT")
            break
    if not reasons:
        return (True, "")
    for code in ("ALERT", "TSTM", "RAIN", "WIND", "HEAT"):
        if code in reasons:
            return (False, code)
    return (False, "?")

def _weather_poll():
    """One weather refresh cycle. Resolves NWS grid on first call (cached forever),
    then fetches hourly forecast + active alerts sequentially, decides grilling
    suitability, and writes globals under their locks. On any failure: increment
    fail_streak, log, preserve last good state."""
    global _nws_grid
    try:
        lat, lon = FORT_MYERS_LATLON
        if _nws_grid is None:
            pts = _nws_get(f"https://api.weather.gov/points/{lat},{lon}")
            props = pts["properties"]
            _nws_grid = (props["gridId"], props["gridX"], props["gridY"])
            logging.info("weather: NWS grid resolved %s", _nws_grid)
        gid, gx, gy = _nws_grid
        hourly = _nws_get(f"https://api.weather.gov/gridpoints/{gid}/{gx},{gy}/forecast/hourly")
        alerts = _nws_get(f"https://api.weather.gov/alerts/active?point={lat},{lon}")
        periods = hourly["properties"]["periods"]
        now_p = periods[0]
        temp_f = now_p.get("temperature")
        short = (now_p.get("shortForecast") or "").strip()
        # Condition word for icon: derive a short uppercase token from the forecast text.
        text_lc = short.lower()
        if "thunder" in text_lc or "tstm" in text_lc:
            icon_word = "TSTM"
        elif "heavy rain" in text_lc or "downpour" in text_lc:
            icon_word = "RAIN"
        elif "rain" in text_lc or "shower" in text_lc or "drizzle" in text_lc:
            icon_word = "DRIZ"
        elif "snow" in text_lc:
            icon_word = "SNOW"
        elif "fog" in text_lc or "haze" in text_lc:
            icon_word = "FOG"
        elif "cloud" in text_lc or "overcast" in text_lc:
            icon_word = "CLD"
        elif "clear" in text_lc or "sunny" in text_lc or "fair" in text_lc:
            icon_word = "CLR"
        else:
            icon_word = short[:4].upper() or "—"
        active_alerts = alerts.get("features", [])
        alert_titles = [a["properties"].get("event", "") for a in active_alerts]
        ok, reason = _decide_grill(periods, active_alerts)
        ts = time.time()
        with _weather_lock:
            _weather.update({"temp_f": temp_f, "short": short, "icon_word": icon_word,
                             "ts": ts, "fail_streak": 0})
        with _grill_lock:
            _grill.update({"ok": ok, "reason": reason, "ts": ts})
        logging.info("weather: %s°F %s — grill=%s %s alerts=%s",
                     temp_f, icon_word, "OK" if ok else "NO",
                     reason or "", alert_titles or "none")
    except Exception as e:
        with _weather_lock:
            _weather["fail_streak"] += 1
        logging.warning("weather poll failed (#%d): %s", _weather["fail_streak"], e)

def _weather_loop():
    """Background weather poller: refresh every WEATHER_REFRESH_SEC."""
    while True:
        _weather_poll()
        time.sleep(WEATHER_REFRESH_SEC)

def _cpu_pct():
    """Aggregate CPU% since the last sample. Cached at most every 1s — the
    render loop calls this at 20fps, but /proc/stat only updates at the kernel
    tick rate, so faster sampling is noise. ponytail: 4-line global + timestamp
    — no thread, no psutil dep — upgrade: psutil if the-host already has it."""
    global _cpu_prev, _cpu_ts, _cpu_pct_cached
    now = time.monotonic()
    if now - _cpu_ts < 1.0:
        return _cpu_pct_cached
    _cpu_ts = now
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]   # user,nice,system,idle,iowait,...
        vals = [int(x) for x in parts]
        idle = vals[3]
        total = sum(vals)
        if _cpu_prev is not None:
            di = idle - _cpu_prev[0]; dt = total - _cpu_prev[1]
            _cpu_pct_cached = max(0.0, min(100.0, 100.0 * (1.0 - di / dt))) if dt > 0 else 0.0
        _cpu_prev = (idle, total)
    except Exception:
        pass
    return _cpu_pct_cached

def _mem_pct():
    """MemAvailable / MemTotal from /proc/meminfo. Read inline — one syscall
    per frame at 20fps is ~zero cost. ponytail: no cache — upgrade: cache if
    profiling ever shows the read in the hot path."""
    try:
        with open("/proc/meminfo") as f:
            mi = {ln.split(":")[0]: ln for ln in f}
        total = int(mi["MemTotal"].split()[1])
        avail = int(mi["MemAvailable"].split()[1])
        return 100.0 * (1.0 - avail / total) if total > 0 else 0.0
    except Exception:
        return 0.0

def _bars(d, x, y, items):
    """items = [(label, value_0_1, color), ...] — mini bars + label."""
    for label, v, color in items:
        d.text((x, y - 2), label, font=ImageFont.truetype(FONT_R, 11), fill=(220, 220, 220))
        d.rectangle([x + 28, y, x + 28 + int(40 * max(0, min(1, v))), y + 6], fill=color)
        x += 78

def active_is_suggest():
    """True if the active session is at a free-text / auto-suggest prompt
    (label "suggest…"), OR was within the last few seconds — agent-deck flickers
    status running↔waiting, which momentarily flips the label off "suggest…".
    The sticky window keeps the "Next" label and a safe slot-2 press alive
    across that flicker. A definitive numbered-menu label is NOT overridden."""
    s = active_session()
    if not s:
        return False
    sid = s["id"]
    lbl = _activity.get(sid, (None, False, None))[0]
    if lbl == "suggest…":
        return True
    if lbl != "choose…":                       # don't override a real menu
        ts = _suggest_sticky.get(sid)
        if ts and (time.monotonic() - ts) < 5.0:
            return True
    return False

def dismiss_session(sess):
    """Mark a session's current prompt as dismissed: stop blinking it, drop it
    from the focus queue, WITHOUT sending keys to the agent. Rearms when the
    agent next goes busy (spinner) or stops needing input, or after DISMISS_TIMEOUT."""
    sid = sess.get("id")
    _dismissed[sid] = time.monotonic()
    _needed_since.pop(sid, None)
    log("dismissed input gate for %s", sess.get("title"))

def act_reply(slot, reply_set=None, allow_dismiss=True):
    """reply_set overrides which REPLY_SETS entry to use. allow_dismiss controls
    whether slot 2 of the select set means "Next" (dismiss the prompt without
    sending) — TRUE on the Plus, whose bottom row had no dedicated Next key, so
    slot 2 doubled as it; FALSE on the XL+, where the Next/</>/Set/+ strip was
    removed (keys 13-17 are dead), so slot 2 there sends a real "3" (option-3
    in a numbered menu)."""
    if reply_set is None:
        reply_set = _reply_set
    s = active_session()
    if not s:
        log("reply: no active session"); return
    # Plus only: slot 2 of the select set dismisses the active session's input
    # gate (stop blinking, yield focus) WITHOUT sending keys — for numbered menus
    # AND auto-suggest prompts, "read it, move on". The XL+ passes allow_dismiss
    # =False so slot 2 sends option 3 and dismiss lives on its own key.
    if allow_dismiss and slot == 2 and reply_set == 0:
        dismiss_session(s); _advance_focus(s["id"]); return
    label, keys = REPLY_SETS[reply_set][1][slot]
    if not keys:
        return                                  # blank slot
    if keys[0] == "!voice":                     # voice dictation toggle
        _bg(_voice_toggle, s); return
    if keys[0].startswith("!"):                 # other shell command
        cmd = keys[0][1:]
        _run(["bash", "-lc", cmd])
        log("zone '%s' -> %s", label, cmd); return
    log("reply '%s' -> %s", label, s.get("title")); tmux_send(s, keys)
    _advance_focus(s["id"])          # zero-lag: snap selector to next needy session

def _attach_cmd(sid):
    # start-if-needed then attach: `start` revives a stopped/killed session (so a
    # session you stopped with dial-4 reopens cleanly); it errors harmlessly when
    # the session is already running, so we silence that and attach regardless.
    # flock one-shot guard: Konsole re-runs a window's original `-e` command for
    # every NEW session created in that window (splits/tabs inherit it), which
    # mirrored the original agent into fresh panes (observed 2026-07-04). The
    # lock is held for the attach's lifetime, so an inherited re-run fails `-n`
    # and degrades to a plain shell instead of a mirror.
    if re.search(r"[^\w.-]", sid or ""):
        # sid feeds a shell fragment + lock filename; agent-deck ids are hex-dash
        # today — if that ever changes, skip the guard rather than break quoting.
        log("attach: sid %r has unexpected chars; skipping flock guard", sid)
        return "%s session start %s >/dev/null 2>&1; %s session attach %s" % (AD, sid, AD, sid)
    lock = "$HOME/.cache/agentdeck/attach-%s.lock" % sid
    return ("mkdir -p $HOME/.cache/agentdeck; flock -n %s -c "
            "'%s session start %s >/dev/null 2>&1; %s session attach %s' "
            "|| { echo \"[agentdeck] session %s already attached elsewhere "
            "(lock held) - plain shell\"; exec bash; }"
            % (lock, AD, sid, AD, sid, sid))

def open_existing(s, mode):
    """(Re)open an existing session in the chosen placement (window/tab/split)."""
    place_konsole(_attach_cmd(s["id"]), mode, sid=s["id"], tmux=s.get("tmux_session"))

def toggle_or_place(deck, s):
    """If this session's konsole window is alive: minimize it when visible,
    restore+raise when minimized. If it has NO window yet, open the placement
    menu for it (window / tab / split), just like spawning a new one — so an
    existing session can be dropped into a split of your konsole too.
    Re-resolves the window from the tracked pid, so an X-close can't desync it."""
    global _pending_session, _pending_tool
    if not s:
        return
    with _lock:
        pid = _win_map.get(s["id"])
    wins = _windows_of_pid(pid)
    if wins:
        wid = wins[0]
        if _window_visible(wid):
            _xrun(["xdotool", "windowminimize", wid]); log("minimize window for %s", s.get("title"))
        else:
            _xrun(["wmctrl", "-i", "-a", wid]); log("restore window for %s", s.get("title"))
    else:
        log("no window for %s; placement menu", s.get("title"))
        _pending_tool = None
        _pending_session = s
        open_menu("place")
        repaint(deck)

def _unique_title(label, sessions):
    """Append -2, -3, … so multiple sessions of the same tool can coexist. agent-deck's
    own dedup didn't fire reliably under -title-lock, so we disambiguate up front."""
    titles = {s.get("title", "") for s in sessions}
    if label not in titles:
        return label
    n = 2
    while "%s-%d" % (label, n) in titles:
        n += 1
    return "%s-%d" % (label, n)

def spawn(tool, mode):
    label, cmd = tool
    with _lock:
        title = _unique_title(label, _sessions)
    log("spawn '%s' as '%s' (%s) in %s", label, title, cmd, NEW_SESSION_DIR)
    # -t names the session; -title-lock keeps Claude's session-name sync from
    # overriding it back to the folder name (e.g. "pooknast").
    r = _run([AD, "launch", NEW_SESSION_DIR, "-cmd", cmd,
              "-t", title, "-title-lock", "--json"], timeout=40)
    if not (r and r.returncode == 0):
        log("spawn failed: %s", (r.stderr.strip()[:140] if r else "no result")); return
    try:
        info = json.loads(r.stdout)
        sid = info.get("id")
        tmux_name = info.get("tmux_session")
    except Exception as e:
        log("spawn parse error: %s", e); return
    if sid:
        place_konsole(_attach_cmd(sid), mode, sid=sid, tmux=tmux_name)

def select_delta(n):
    global _active_id
    with _lock:
        if not _sessions:
            return
        ids = [s["id"] for s in _sessions]
        i = ids.index(_active_id) if _active_id in ids else 0
        _active_id = ids[(i + n) % len(ids)]

def _advance_focus(exclude_sid=None):
    """Snap the selector to the top-priority needy session NOW, excluding the one
    just replied/dismissed so focus advances to the NEXT in the queue without
    waiting for the 2s poll. Event-driven — only called on an explicit
    reply/dismiss — so it can't reintroduce the equal-priority jitter the
    periodic loop avoids.
    ponytail: reads cached _activity/_urgency under _lock — upgrade: trigger a
    fresh scrape too if a brand-new need must be caught faster than REFRESH_SECS."""
    global _active_id
    with _lock:
        needy = [sid for sid, (_lbl, need, _rec) in _activity.items()
                 if need and sid != exclude_sid]
        if not needy:
            return
        needy.sort(key=lambda sid: URG_RANK.get(_urgency.get(sid), 99))
        choice = needy[0]
        cur = _active_id
        cur_rank = (URG_RANK.get(_urgency.get(cur), 99)
                    if _activity.get(cur, (None, False))[1] else 99)
        # Advance when the current session was just handled (exclude_sid) OR a
        # strictly higher-priority need exists. Equal rank never yanks (no jitter).
        if cur == exclude_sid or URG_RANK.get(_urgency.get(choice), 99) < cur_rank:
            if choice != cur:
                _active_id = choice
                log("advance focus -> %s", choice[:8])

# ---- menu state -----------------------------------------------------------
def open_menu(mode):
    global _ui_mode, _menu_deadline
    _ui_mode = mode
    _menu_deadline = time.monotonic() + MENU_TIMEOUT

def close_menu():
    global _ui_mode, _pending_tool, _pending_session
    _ui_mode = "board"; _pending_tool = None; _pending_session = None

# ---- rendering ------------------------------------------------------------
def _multiline(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return (lines or [text[:8]])[:3]

def _key_img(deck, bg):
    img = PILHelper.create_key_image(deck)
    ImageDraw.Draw(img).rectangle([0, 0, img.width, img.height], fill=bg)
    return img

def _render_text(d, img, text, sub=None, text_fill=TXT_DIM, size=20):
    """Lay out title + optional sub onto an existing image, vertically centered
    as a group. Auto-shrinks both fonts until they fit. Factored out of
    _centered so the animated-key path can overlay text on a dynamic background
    without re-implementing the layout."""
    PAD = 10                                   # clearance each side (clears border)
    max_w = img.width - PAD * 2
    # Auto-shrink title font until the longest line fits the key.
    while size >= 11:
        f = ImageFont.truetype(FONT_B, size)
        lines = _multiline(d, text, f, max_w)
        if max((d.textlength(ln, font=f) for ln in lines), default=0) <= max_w:
            break
        size -= 1
    # Auto-shrink sub font so the activity label fits too.
    sub_size = 15; sf = None
    if sub:
        while sub_size >= 10:
            sf = ImageFont.truetype(FONT_R, sub_size)
            if d.textlength(sub, font=sf) <= max_w:
                break
            sub_size -= 1
    lh = size + 2
    gap = 3
    sub_h = (sub_size + gap) if sub else 0
    block_h = len(lines) * lh + sub_h
    y = (img.height - block_h) / 2
    for ln in lines:
        d.text((img.width / 2, y), ln, font=f, anchor="ma", fill=text_fill); y += lh
    if sub:
        d.text((img.width / 2, y + gap), sub, font=sf, anchor="ma", fill=text_fill)

def _centered(deck, bg, text, size=20, sub=None, border=None, border_w=4,
              text_fill=TXT_DIM):
    img = _key_img(deck, bg)
    d = ImageDraw.Draw(img)
    _render_text(d, img, text, sub=sub, text_fill=text_fill, size=size)
    if border:
        d.rectangle([1, 1, img.width - 2, img.height - 2], outline=border, width=border_w)
    return PILHelper.to_native_key_format(deck, img)

# ---- weather + grill label/color helpers (used by LCD renderer) ------------
def _weather_bg(icon_word, temp_f):
    """Pick a background color from the condition word + temp. Ghibli-muted."""
    if icon_word == "TSTM":    return (50, 40, 70)    # purple-ish (matches XL_GOAL palette)
    if icon_word == "RAIN":    return (40, 50, 70)    # dark blue-gray
    if icon_word in ("DRIZ", "FOG", "SNOW"): return (45, 50, 58)  # neutral gray
    if icon_word == "CLD":     return (50, 55, 65)    # neutral cloudy
    if isinstance(temp_f, (int, float)) and temp_f >= 95:
        return (70, 50, 30)                           # amber (matches XL_TOOL_SWAP)
    return (40, 60, 80)                               # cool blue (fair/clear)

def _weather_label():
    """Short label string used by both render paths. Cinema overlays call this."""
    with _weather_lock:
        t = _weather["temp_f"]
        w = _weather["icon_word"]
        fail = _weather["fail_streak"]
    if fail > 4 or t is None:
        return "?"
    return "%d %s" % (int(t), w or "—")

def _grill_label():
    """Short label string used by both render paths."""
    with _grill_lock:
        ok = _grill["ok"]
        reason = _grill["reason"]
    if ok is None:
        return "?"
    if ok:
        return "GRILL"
    return ("GRILL %s" % reason) if reason else "GRILL"

# ---- animation renderers (Ghibli accents) ---------------------------------
# Each takes the PIL draw context, a 0..1 phase, an accent color, and the dark
# base color. They mutate the image in place. Renderers are pure: same phase +
# colors -> same pixels, so _frame_cache can dedupe unchanged frames.
def _ease_sine(t):
    """0..1 -> 0..1 smooth sine easing (slow at the extremes, fast in the middle)."""
    return (math.sin(2.0 * math.pi * (t % 1.0)) + 1.0) / 2.0

# Linear RGB blend, t in 0..1 — canonical implementation lives in ghibli_scenes.
_lerp_color = ghibli._lerp

def _anim_pulse(draw, img, phase, color, base, amp=0.5):
    """Breathing background: bg eases between base and accent. amp is the peak
    blend strength (0.5 = strong, 0.03 = barely-there idle glow)."""
    draw.rectangle([0, 0, img.width, img.height],
                   fill=_lerp_color(base, color, _ease_sine(phase) * amp))

def _anim_spinner(draw, img, phase, color, base):
    """Rotating arc around the key border. phase 0..1 = one full revolution.
    A 90° arc with a tapering tail reads as a spinner even at small sizes."""
    w, h = img.size
    draw.rectangle([0, 0, w, h], fill=base)
    # Draw a soft halo ring (3 concentric arcs of decreasing intensity) for a
    # comet-tail effect rather than a flat stroke.
    for ring, inset in ((1.0, 6), (0.55, 12), (0.25, 18)):
        c = _lerp_color(base, color, ring)
        start = int(360 * phase)
        draw.arc([inset, inset, w - inset, h - inset],
                 start=start, end=start + 100, fill=c, width=max(2, int(10 - ring * 6)))

def _anim_shimmer(draw, img, phase, color, base, amp=0.03):
    """Diagonal light band sweeping across the key. amp = peak intensity.
    Idle uses amp=0.03 (subtle Castle Cloud wave); queued uses ~0.18."""
    w, h = img.size
    draw.rectangle([0, 0, w, h], fill=base)
    # Band center sweeps left -> right; width is 40% of the key.
    cx = (phase % 1.0) * (w + 80) - 40
    bw = w * 0.4
    # Approximate the band as 5 vertical slabs of decreasing intensity off the
    # center. Cheaper than a true gradient and reads the same at key resolution.
    for i, frac in enumerate((0.05, 0.15, amp, 0.15, 0.05)):
        x = int(cx - bw / 2 + (i - 2) * bw / 5)
        if -bw < x < w + bw:
            slab = [x, 0, x + int(bw / 5) + 1, h]
            draw.rectangle(slab, fill=_lerp_color(base, color, frac))

def _anim_sweep_rects(draw, phase, color, base, rect):
    """Concentric rectangles expanding outward from rect's center. Used for the
    touchscreen recommended-zone highlight (replaces hard on/off blink)."""
    x0, y0, x1, y1 = rect
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    # Three expanding rings; phase drives both radius and intensity so the
    # outermost ring fades as a new one is born — a continuous pulse.
    for k in range(3):
        p = (phase + k / 3.0) % 1.0
        scale = 0.4 + p * 0.6
        alpha = (1.0 - p) * 0.7
        hw = (x1 - x0) * scale / 2
        hh = (y1 - y0) * scale / 2
        draw.rectangle([cx - hw, cy - hh, cx + hw, cy + hh],
                       fill=_lerp_color(base, color, alpha))

# ---- weather animation primitives (LCD weather zone) ----------------------
# Each takes a PIL draw context + a bounding region + phase 0..1 + color.
# Pure: same phase + color → same pixels. Used by _render_weather_lcd_zone to
# animate the condition icon at the left of the weather strip. ponytail: no
# asset files — all procedural with PIL primitives. upgrade: PNG sprites if a
# richer look is ever worth the disk footprint.
def _draw_sun(d, cx, cy, r, phase, color):
    """Sun disk + 8 rotating spokes. phase 0..1 = one full rotation."""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    for k in range(8):
        a = phase * 2 * math.pi + k * (math.pi / 4)
        x1 = cx + math.cos(a) * (r + 4)
        y1 = cy + math.sin(a) * (r + 4)
        x2 = cx + math.cos(a) * (r + 10)
        y2 = cy + math.sin(a) * (r + 10)
        d.line([x1, y1, x2, y2], fill=color, width=2)

def _draw_cloud(d, cx, cy, w, phase, color):
    """3 overlapping ellipses drifting right; wraps modulo w."""
    drift = (phase * w) % w
    for k, off in enumerate((-w/3, 0, w/3)):
        x = (cx + off + drift - w/2) % w + (cx - w/2)
        ew = w / 3
        d.ellipse([x - ew/2, cy - 6, x + ew/2, cy + 6], fill=color)

def _draw_rain(d, x0, y0, w, h, phase, density, color):
    """Vertical streaks falling. density = number of drops. phase 0..1 = one wrap."""
    span = h + 8
    for k in range(density):
        # Stable per-drop x position; only y animates.
        x = x0 + int((k * 37.5) % w)
        y_off = (phase * span + k * (span / density)) % span
        y_top = y0 + y_off - 6
        d.line([x, y_top, x, y_top + 6], fill=color, width=1)

def _draw_lightning(d, cx, cy, h, phase, color):
    """Jagged bolt + brief bright flash every cycle."""
    # Flash background on first 10% of cycle.
    if phase < 0.1:
        flash = (255, 255, 200) if color == (255, 220, 80) else color
        # caller's rect should already be drawn; we just add the bolt brighter.
    bolt_h = h - 8
    pts = [(cx - 3, cy - bolt_h/2), (cx + 4, cy - bolt_h/4),
           (cx - 2, cy), (cx + 3, cy + bolt_h/4), (cx, cy + bolt_h/2)]
    d.line(pts, fill=color, width=2, joint="curve")

def _draw_fog(d, x0, y0, w, h, phase, color):
    """Three horizontal translucent bands drifting at different speeds."""
    for k in range(3):
        speed = (1.0, 0.6, 0.4)[k]
        offset = (phase * w * speed + k * w/3) % w
        y = y0 + 8 + k * 12
        # Bands wrap; draw two copies to cover the seam.
        for off in (offset - w, offset):
            d.rectangle([x0 + off, y, x0 + off + w * 0.6, y + 4], fill=color)

def _draw_snow(d, x0, y0, w, h, phase, density, color):
    """Small dots falling with sine-wave horizontal drift."""
    span = h + 6
    for k in range(density):
        sx = (k * 41.0) % w
        drift = math.sin((phase + k * 0.3) * 2 * math.pi) * 4
        x = x0 + int(sx + drift)
        y_off = (phase * span + k * (span / density)) % span
        y = y0 + y_off
        d.ellipse([x - 1, y - 1, x + 1, y + 1], fill=color)

def _draw_heat_shimmer(d, x0, y0, w, h, phase, color):
    """Three wavy horizontal lines below the sun (heat rising)."""
    for k in range(3):
        y = y0 + 4 + k * 6
        pts = []
        for x in range(0, int(w), 3):
            wave = math.sin((x / w) * 2 * math.pi + phase * 2 * math.pi + k) * 2
            pts.append((x0 + x, y + wave))
        if len(pts) > 1:
            d.line(pts, fill=color, width=1)

def _render_weather_lcd_zone(d, x0, y0, w, h, phase):
    """Draw the 400px weather zone: animated condition icon (left 100px),
    temp+condition+city (mid 200px), grill verdict badge (right 100px).
    Reads _weather + _grill under their locks."""
    with _weather_lock:
        snap = dict(_weather)
    with _grill_lock:
        grill = dict(_grill)
    fail = snap["fail_streak"] > 4 or snap["temp_f"] is None
    icon_word = snap["icon_word"] if not fail else "?"
    # ---- animated icon (left 100px) ----
    icon_cx = x0 + 50
    icon_cy = y0 + h/2
    icon_color = TXT_BRIGHT if not fail else (120, 120, 120)
    if fail or icon_word == "?":
        # Dim "?" pulsing.
        pulse = 0.5 + 0.5 * math.sin(phase * 2 * math.pi)
        c = tuple(int(v * (0.4 + 0.4 * pulse)) for v in icon_color)
        f = ImageFont.truetype(FONT_B, 22)
        d.text((icon_cx, icon_cy), "?", font=f, anchor="mm", fill=c)
    elif icon_word == "CLR":
        _draw_sun(d, icon_cx, icon_cy, 8, phase / 8.0, icon_color)
        if isinstance(snap["temp_f"], (int, float)) and snap["temp_f"] >= 95:
            _draw_heat_shimmer(d, x0 + 10, icon_cy + 10, 80, 14, phase / 2.0, icon_color)
    elif icon_word == "CLD":
        _draw_cloud(d, icon_cx, icon_cy, 60, phase / 12.0, icon_color)
    elif icon_word == "DRIZ":
        _draw_cloud(d, icon_cx, icon_cy - 6, 60, phase / 12.0, icon_color)
        _draw_rain(d, x0 + 10, icon_cy + 4, 80, 16, phase / 1.5, 6, icon_color)
    elif icon_word == "RAIN":
        _draw_cloud(d, icon_cx, icon_cy - 6, 60, phase / 12.0, icon_color)
        _draw_rain(d, x0 + 10, icon_cy + 4, 80, 18, phase / 0.8, 10, icon_color)
    elif icon_word == "TSTM":
        # Lightning every 1.5s; dark cloud behind.
        _draw_cloud(d, icon_cx, icon_cy - 8, 60, phase / 12.0, (100, 90, 110))
        _draw_lightning(d, icon_cx, icon_cy + 4, 18, (phase / 1.5) % 1.0, (255, 220, 80))
    elif icon_word == "FOG":
        _draw_fog(d, x0 + 8, icon_cy - 10, 84, 22, phase / 6.0, icon_color)
    elif icon_word == "SNOW":
        _draw_snow(d, x0 + 10, icon_cy - 8, 80, 22, phase / 2.0, 8, icon_color)
    else:
        # Fallback: short text label.
        f = ImageFont.truetype(FONT_B, 14)
        d.text((icon_cx, icon_cy), icon_word[:4], font=f, anchor="mm", fill=icon_color)
    # ---- temp + condition + city (middle 200px, x=x0+100..x0+300) ----
    mid_x = x0 + 200
    if not fail and isinstance(snap["temp_f"], (int, float)):
        d.text((mid_x, y0 + 8), "%d°F" % int(snap["temp_f"]),
               font=ImageFont.truetype(FONT_B, 22), anchor="ma", fill=TXT_BRIGHT)
        d.text((mid_x, y0 + 32), icon_word or snap["short"][:8] or "—",
               font=ImageFont.truetype(FONT_R, 13), anchor="ma", fill=TXT_DIM)
        d.text((mid_x, y0 + 46), "the city",
               font=ImageFont.truetype(FONT_R, 10), anchor="ma", fill=(140, 140, 140))
    else:
        d.text((mid_x, y0 + h/2), "no signal",
               font=ImageFont.truetype(FONT_R, 13), anchor="mm", fill=(140, 140, 140))
    # divider before grill badge
    d.line([(x0 + 300, y0 + 8), (x0 + 300, y0 + h - 4)], fill=(30, 32, 38), width=1)
    # ---- grill verdict (right 100px, x=x0+300..x0+400) ----
    badge_x = x0 + 350
    if grill["ok"] is None:
        bg_c, label_c, sub = (40, 40, 40), (140, 140, 140), "?"
    elif grill["ok"]:
        bg_c, label_c, sub = (30, 80, 50), (140, 230, 170), "ok"
    else:
        bg_c, label_c, sub = (80, 35, 35), (240, 170, 170), (grill["reason"].lower() or "no")
    d.rectangle([x0 + 304, y0 + 6, x0 + 396, y0 + h - 4], fill=bg_c)
    d.text((badge_x, y0 + 14), "GRILL", font=ImageFont.truetype(FONT_B, 13),
           anchor="ma", fill=label_c)
    d.text((badge_x, y0 + 36), sub, font=ImageFont.truetype(FONT_B, 13),
           anchor="ma", fill=label_c)

def _render_reply_key(deck, zone, rec_zone):
    """Non-cinema bottom-row key mirroring reply zone `zone` (0-3) — flat dark
    tile + label, Golden Meadow pulse when it's the recommended zone. Pinned
    to REPLY_SETS[0] ("select"/"1 2 Next Go") regardless of what's cycling on
    the touchscreen — the physical keys always answer menus. Cinema mode has
    its own variant, _render_reply_tile, that overlays the same content on
    the Ghibli scene instead of a flat background."""
    _, zones = REPLY_SETS[0]
    label = zones[zone][0]
    if zone == 2:
        label = "Next"
    img = _key_img(deck, MENU_COLOR)
    d = ImageDraw.Draw(img)
    if zone == rec_zone:
        _anim_pulse(d, img, _anim_phase / 1.6, GHIBLI["meadow"], MENU_COLOR, amp=0.55)
        text_fill = (25, 20, 10)
    else:
        text_fill = TXT_DIM
    _render_text(d, img, label, text_fill=text_fill, size=28)
    return PILHelper.to_native_key_format(deck, img)

def _render_session(deck, s, is_active):
    st = s.get("status", "idle")
    label, needs, _rec = _activity.get(s["id"], (st, False, None))
    # 13 chars used to clip "Wrangling 1m 20s" down to "Wrangling 1m" —
    # dropping the seconds made the live elapsed time look frozen for up to
    # a minute at a time. 18 comfortably fits spinner-word + "Xm Ys".
    title, sub = s.get("title", "?"), str(label)[:18]
    base = STATE_COLOR.get(st, (50, 50, 58))
    urg = _urgency.get(s["id"], "menu") if needs else None
    # Period scaling: each renderer takes phase in "cycles", so dividing
    # _anim_phase by the period (sec) gives one full cycle per period.
    # Periods were chosen for seizure safety (all >= 1.0s) and to layer
    # without beating (no two periods share a common multiple under 6s).
    P = _anim_phase  # seconds, monotonic
    if needs and is_active and urg in ("menu", "urgent"):
        # AUTO-FOCUSED needy session: Golden Meadow breathing, 1.6s cycle.
        # Only one session breathes at a time — the one shown on the LCD — so
        # the auto-focus target is unambiguous even when multiple sessions
        # need input. Other needy sessions fall through to the static-border
        # branch below.
        img = _key_img(deck, base)
        d = ImageDraw.Draw(img)
        _anim_pulse(d, img, P / 1.6, GHIBLI["meadow"], base, amp=0.55)
        text_fill = _lerp_color(TXT_BRIGHT, (25, 20, 10), _ease_sine(P / 1.6) * 0.5)
    elif needs and is_active and urg == "patient":
        # AUTO-FOCUSED patient text: Spirited Rose breathing, slower (3.0s).
        img = _key_img(deck, base)
        d = ImageDraw.Draw(img)
        _anim_pulse(d, img, P / 3.0, GHIBLI["rose"], base, amp=0.40)
        text_fill = TXT_BRIGHT
    elif needs and not is_active:
        # Other needy sessions: steady accent border, NO breathing. Keeps the
        # auto-focus target unambiguous while still signalling "I also need
        # input". 65% lerp toward the urgency accent matches the cinema path.
        accent = GHIBLI["meadow"] if urg in ("menu", "urgent") else GHIBLI["rose"]
        border_c = _lerp_color((20, 20, 28), accent, 0.65)
        return _centered(deck, base, title, sub=sub, border=border_c, border_w=3,
                         text_fill=TXT_BRIGHT)
    elif st == "error":
        # Calm alarm: Muted Coral pulse, 1.0s. Slower than a strobe by 3x.
        img = _key_img(deck, base)
        d = ImageDraw.Draw(img)
        _anim_pulse(d, img, P / 1.0, GHIBLI["coral"], base, amp=0.45)
        text_fill = TXT_BRIGHT
    elif st in ("running", "starting") or label == "thinking":
        # Working: Enchanted Forest spinner arc rotating around the border.
        img = _key_img(deck, base)
        d = ImageDraw.Draw(img)
        _anim_spinner(d, img, P / 1.2, GHIBLI["forest"], base)
        text_fill = TXT_BRIGHT
    elif st == "queued":
        # Queued: Whispering Wind shimmer, slow diagonal sweep (5.0s).
        img = _key_img(deck, base)
        d = ImageDraw.Draw(img)
        _anim_shimmer(d, img, P / 5.0, GHIBLI["wind"], base, amp=0.18)
        text_fill = TXT_BRIGHT
    elif st == "idle":
        # Idle: barely-there Castle Cloud shimmer (3% wave, 8s). Never fully
        # still per user preference — feels alive without distracting.
        img = _key_img(deck, base)
        d = ImageDraw.Draw(img)
        _anim_shimmer(d, img, P / 8.0, GHIBLI["cloud"], base, amp=0.03)
        text_fill = TXT_DIM
    else:
        # stopped / unknown: static, no animation, cache-friendly.
        border = (180, 185, 195) if is_active else None
        return _centered(deck, base, title, sub=sub, border=border)
    # Auto-focused needy session gets a slightly larger title (22pt vs 20pt)
    # to match the cinema path's _overlay_title_centered treatment and make
    # the live target pop. _render_text auto-shrinks if it doesn't fit.
    title_size = 22 if (needs and is_active) else 20
    _render_text(d, img, title, sub=sub, text_fill=text_fill, size=title_size)
    if is_active:
        _draw_selector(d, img)
    return PILHelper.to_native_key_format(deck, img)

def paint_board(deck):
    # Clear the frame cache so every key is force-pushed once (used after a
    # mode switch, menu close, or other context change where stale dedup would
    # suppress a needed redraw).
    global _touch_frame_cache
    _frame_cache.clear()
    _touch_frame_cache = None
    animate_active_keys(deck)

def _overlay_title(draw, img, title):
    """Session title pinned to the top-left corner with a dark drop-shadow on
    all four sides so it reads against any scene background without a backing
    rectangle (classic pixel-art text technique — no alpha needed for JPEG)."""
    f = ImageFont.truetype(FONT_B, 13)
    txt = title[:11]
    y = 4
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        draw.text((4 + dx, y + dy), txt, font=f, fill=(0, 0, 0))
    draw.text((4, y), txt, font=f, fill=TXT_BANNER)

def _overlay_title_centered(draw, img, title, sub=None):
    """Title centered at 22pt for the AUTO-FOCUSED needy session — larger than
    the top-left overlay so the active session is instantly findable on the
    board. Optional sub-label (e.g. 'choose…', 'thinking 3m') renders below in
    13pt regular. Uses _render_text's auto-shrink + wrap so long titles still
    fit. Drop-shadow comes from the cinema wash, not per-pixel shadows."""
    _render_text(draw, img, title, sub=sub, text_fill=TXT_BANNER, size=22)

def _overlay_activity(draw, img, phase, label=None):
    """Real-time CLI activity row just below the top-left title: a mini
    Enchanted-Forest arc spinner (1.2s rotation, matching the per-key spinner
    cadence) followed by the live activity label (e.g. 'Wrangling 1m 20s') so
    the actual elapsed time/action is visible, not just a static spinner icon.
    Drop-shadow pixels on four sides keep both legible over any scene
    background."""
    cx, cy = 12, 22
    r = 6
    color = GHIBLI["forest"]
    p = (phase / 1.2) % 1.0
    start = int(p * 360)
    end = start + 270
    bbox = [cx - r, cy - r, cx + r, cy + r]
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        draw.arc([bbox[0] + dx, bbox[1] + dy, bbox[2] + dx, bbox[3] + dy],
                 start, end, fill=(0, 0, 0), width=2)
    draw.arc(bbox, start, end, fill=color, width=2)
    if label and label != "thinking":
        # Slightly larger legibility per user request — was 11pt / 16 chars.
        # 13pt + 18 chars still fits the XL tile alongside the 12px spinner arc.
        f = ImageFont.truetype(FONT_R, 13)
        txt = label[:18]
        tx, ty = cx + r + 4, cy - 7
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            draw.text((tx + dx, ty + dy), txt, font=f, fill=(0, 0, 0))
        draw.text((tx, ty), txt, font=f, fill=(230, 235, 240))

def _draw_selector(draw, img):
    """Active session selector — bright white corner brackets (viewfinder style).
    Steady and non-breathing by design: the needy accent borders breathe gold/pink,
    so a SOLID white corner-bracket is visually orthogonal and unmistakable.
    Drawn LAST in the per-key pipeline so nothing paints over it. Corner arms
    are 14px long / 4px thick — reads instantly as 'focused' even at 120x120."""
    w, h = img.width, img.height
    c = (255, 255, 255)
    sh = (0, 0, 0)
    arm = 14
    th = 4
    brackets = [
        # (hx0, hy0, hx1, hy3, vx0, vy0, vx3, vy3) per corner
        (0,      0,     arm,        th - 1,   0,     0,     th - 1, arm),       # TL
        (w - arm, 0,     w - 1,      th - 1,   w - th, 0,     w - 1,  arm),       # TR
        (0,      h - th, arm,        h - 1,    0,     h - arm, th - 1, h - 1),   # BL
        (w - arm, h - th, w - 1,     h - 1,    w - th, h - arm, w - 1,  h - 1),   # BR
    ]
    for hx0, hy0, hx1, hy1, vx0, vy0, vx1, vy1 in brackets:
        # 1px dark backing for contrast over any scene pixel
        draw.rectangle([hx0 - 1, hy0 - 1, hx1 + 1, hy1 + 1], fill=sh)
        draw.rectangle([vx0 - 1, vy0 - 1, vx1 + 1, vy1 + 1], fill=sh)
        # bright white bracket arm
        draw.rectangle([hx0, hy0, hx1, hy1], fill=c)
        draw.rectangle([vx0, vy0, vx1, vy1], fill=c)

def _overlay_status_dot(draw, img, status):
    """Tiny 6x6 status indicator in the top-right corner. Color-coded:
    green=running, amber=waiting, red=error, slate=stopped, dim=idle."""
    dot_colors = {"running": (40, 120, 50), "starting": (40, 80, 160),
                  "waiting": (180, 130, 30), "error": (160, 40, 40),
                  "stopped": (50, 50, 60), "idle": (60, 65, 75),
                  "queued": (40, 80, 160)}
    c = dot_colors.get(status, (60, 65, 75))
    draw.rectangle([img.width - 8, 2, img.width - 2, 8], fill=c)

def _sync_page_to_active():
    """Jump the page to wherever _active_id currently lives, once per change.
    Runs every render tick but is a no-op unless _active_id actually moved
    since the last check, so a user who deliberately pages away (e.g. to open
    a new session on another page) is never fought back to the active one."""
    global _page, _page_synced_for
    if _active_id == _page_synced_for:
        return
    _page_synced_for = _active_id
    with _lock:
        ids = [s["id"] for s in _sessions]
    if _active_id in ids:
        _page = ids.index(_active_id) // PAGE_SIZE

def _render_reply_tile(tile, zone, rec_zone):
    """Bottom-row key for reply zone `zone` (0-3) — same Ghibli scene tile as
    background, label + recommended-zone highlight overlaid. Pinned to
    REPLY_SETS[0] ("select"/"1 2 Next Go") regardless of what's cycling on
    the touchscreen, so the physical keys always answer menus."""
    _, zones = REPLY_SETS[0]
    label = zones[zone][0]
    if zone == 2:
        label = "Next"
    if zone == rec_zone:
        pulse = _ease_sine(_anim_phase / 1.6)
        wash = Image.new("RGB", tile.size, GHIBLI["meadow"])
        tile = Image.blend(tile, wash, 0.25 + pulse * 0.25)
    d = ImageDraw.Draw(tile)
    f = ImageFont.truetype(FONT_B, 26)
    cx, cy = tile.width / 2, tile.height / 2
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        d.text((cx + dx, cy + dy), label, font=f, anchor="mm", fill=(0, 0, 0))
    d.text((cx, cy), label, font=f, anchor="mm", fill=TXT_BANNER)
    return tile

def _animate_cinema(deck):
    """Cinema mode: render the full 8-key grid as one continuous 8-bit Ghibli
    battle scene. Keys needing input break through with an accent wash + pulsing
    border. Session titles are overlaid with drop-shadows. The scene is never
    interrupted — alerts wash OVER it, not INSTEAD of it."""
    # Render the full scene canvas once, slice into 8 tiles.
    scene = ghibli.render_scene(_anim_phase)
    canvas = ghibli.scale_to_canvas(scene)
    tiles = ghibli.slice_tiles(canvas)
    _sync_page_to_active()
    with _lock:
        sess = list(_sessions[_page * PAGE_SIZE:(_page + 1) * PAGE_SIZE])
        active = _active_id
    s_act = active_session()
    # Not gated on _reply_set: the bottom-row keys always show the "select"
    # set regardless of what the touchscreen is cycling through, so the
    # recommended-zone highlight should always be eligible on the keys too.
    rec_zone = _activity.get(active, (None, False, None))[2] if s_act is not None else None
    for i in range(deck.key_count()):
        tile = tiles[i].copy()                       # mutable per-key copy
        if i >= PAGE_SIZE:
            # Bottom row: always the 4 reply zones (1/2/Next/Go), never
            # sessions — mirrors the touchscreen strip on the physical keys.
            tile = _render_reply_tile(tile, i - PAGE_SIZE, rec_zone)
            deck.set_key_image(i, PILHelper.to_native_key_format(deck, tile))
            continue
        d = ImageDraw.Draw(tile)
        if i < len(sess):
            s = sess[i]
            label, needs, _rec = _activity.get(s["id"], (s.get("status", "idle"), False, None))
            urg = _urgency.get(s["id"], "menu") if needs else None
            st = s.get("status", "idle")
            thinking = (label == "thinking" or st in ("running", "starting"))
            # 1) Break-through wash for needy keys — applied BEFORE overlays so
            #    the title + spinner stay crisp on top of the washed tile.
            if needs:
                accent = GHIBLI["meadow"] if urg in ("menu", "urgent") else GHIBLI["rose"]
                period = 1.6 if urg in ("menu", "urgent") else 3.0
                pulse = _ease_sine(_anim_phase / period)
                wash = Image.new("RGB", tile.size, accent)
                tile = Image.blend(tile, wash, pulse * 0.35)
                d = ImageDraw.Draw(tile)
            # 2) Per-key overlays on top of (possibly washed) scene tile.
            _overlay_title(d, tile, s.get("title", "?"))
            _overlay_status_dot(d, tile, st)
            if thinking:
                # Real-time CLI activity row under the session title — mini
                # Enchanted-Forest arc spinner (1.2s rotation) + live label
                # (elapsed time / current action), not just the icon alone.
                _overlay_activity(d, tile, _anim_phase, label=str(label))
            # 3) Needy accent border — breathes between darkened-accent and
            #    full accent ONLY (gold/pink). Never lerps toward white, so the
            #    white active-selector border below stays visually unique.
            if needs:
                border_c = _lerp_color((20, 20, 28), accent, 0.65 + pulse * 0.35)
                d.rectangle([2, 2, tile.width - 3, tile.height - 3],
                            outline=border_c, width=3)
            # 4) Active session selector — corner brackets drawn LAST so they
            #    dominate every other element. Viewfinder-style, steady white,
            #    visually orthogonal to the breathing accent borders.
            if s["id"] == active:
                _draw_selector(d, tile)
        # Empty slots: pure scene tile, no overlay — the battle plays through.
        frame = PILHelper.to_native_key_format(deck, tile)
        deck.set_key_image(i, frame)

def _render_reply_key_xl(deck, zone, rec_zone):
    """XL+ answer-strip reply key for `zone` (0-3). The XL+ has no touchscreen, so
    the keys themselves show the live _reply_set (select = 1/2/3/4). The Set key
    that used to cycle reply sets is gone (dead key), so the reply set stays put
    unless cycled elsewhere. Golden Meadow pulse on the recommended zone.

    When the active session has a live numbered menu (select set only), the key
    label becomes the option text ("1 Yes / 2 No / 3 Don't ask") instead of a
    bare digit. Zones past the menu's option count render as a dim placeholder
    dash so it's clear they're inert."""
    _, zones = REPLY_SETS[_reply_set]
    opts = _active_menu_opts() if _reply_set == 0 else None
    img = _key_img(deck, MENU_COLOR)
    d = ImageDraw.Draw(img)
    if zone == rec_zone:
        _anim_pulse(d, img, _anim_phase / 1.6, GHIBLI["meadow"], MENU_COLOR, amp=0.55)
        text_fill = (25, 20, 10)
    elif opts and not opts[zone]:
        text_fill = (60, 65, 75)   # very dim for inert placeholder
    else:
        text_fill = TXT_DIM
    if opts and opts[zone]:
        # Option label may need to wrap ("3 Don't ask again") — _render_text
        # auto-shrinks and wraps up to 3 lines.
        label = "%d  %s" % (zone + 1, opts[zone])
        _render_text(d, img, label, text_fill=text_fill, size=20)
    elif opts:
        _render_text(d, img, "–", text_fill=text_fill, size=22)
    else:
        _render_text(d, img, zones[zone][0], text_fill=text_fill, size=26)
    return PILHelper.to_native_key_format(deck, img)

def _animate_xl(deck):
    """XL+ board renderer (non-cinema fallback): session slots on the board keys
    (XL_BOARD_SLOTS = row 0 + rows 2-3), and row 1 = the answer strip
    (1/2/3/4 reply zones; keys 13-17 blank). The recommended-zone highlight only
    applies on the select set (_reply_set 0)."""
    with _lock:
        sess = list(_sessions[:len(XL_BOARD_SLOTS)])
        active = _active_id
    s_act = active_session()
    rec_zone = (_activity.get(active, (None, False, None))[2]
                if _reply_set == 0 and s_act is not None else None)
    for i in range(deck.key_count()):
        if i in XL_SLOT_OF_KEY:
            j = XL_SLOT_OF_KEY[i]
            if j < len(sess):
                frame = _render_session(deck, sess[j], sess[j]["id"] == active)
            else:
                frame = _centered(deck, EMPTY_COLOR, "+", size=32, sub="new")
        elif XL_REPLY0 <= i <= XL_REPLY0 + 3:
            frame = _render_reply_key_xl(deck, i - XL_REPLY0, rec_zone)
        elif XL_SLASH0 <= i < XL_SLASH0 + len(XL_SLASH):
            # M-SD2: slash-command keys (R1 C4-C8). Cool blue background
            # distinguishes them from the warm MENU_COLOR quick row below.
            label = XL_SLASH[i - XL_SLASH0]["label"]
            frame = _centered(deck, (28, 38, 60), label, size=18)
        elif XL_QUICK0 <= i < XL_QUICK0 + len(XL_QUICK):
            label = XL_QUICK[i - XL_QUICK0]["label"]
            qc = {"Go": (24, 56, 100), "Esc": (60, 28, 28)}.get(label, MENU_COLOR)
            frame = _centered(deck, qc, label, size=22)
        elif i == XL_STATUS:
            frame = _centered(deck, (40, 60, 50), "Status", size=16)
        elif XL_GOAL0 <= i < XL_GOAL0 + len(XL_GOAL):
            frame = _centered(deck, (50, 40, 60), XL_GOAL[i - XL_GOAL0]["label"], size=15)
        elif i == XL_TOOL_SWAP:
            frame = _centered(deck, (60, 50, 30), "Swap", size=18)
        else:
            frame = _key_native_blank(deck)
        if _frame_cache.get(i) != frame:
            _frame_cache[i] = frame
            deck.set_key_image(i, frame)

def _overlay_xl_control(tile, key, rec_zone):
    """Overlay an XL+ row-1 control (keys 9-17) onto its Ghibli scene tile with a
    4-way drop-shadow so the panorama shows through behind the label — the same
    "wash over, never replace" treatment the Plus gives its reply strip. Reply
    zones honor the live _reply_set (select = 1/2/3/4) and pulse Golden Meadow on
    the recommended zone. Returns the mutated tile.

    When the active session has a live numbered menu (select set only), the
    label becomes the option text ("1 Yes / 3 Don't ask") and is rendered
    through _render_text on a darkened wash so the wrapped text stays legible
    over the panorama. Zones past the menu's option count render as a dim dash."""
    sub = None
    use_text_renderer = False   # option-label path uses _render_text (wraps)
    is_rec = False
    is_placeholder = False
    if XL_REPLY0 <= key <= XL_REPLY0 + 3:
        zone = key - XL_REPLY0
        _, zones = REPLY_SETS[_reply_set]
        opts = _active_menu_opts() if _reply_set == 0 else None
        if opts and opts[zone]:
            label = "%d  %s" % (zone + 1, opts[zone])
            use_text_renderer = True
        elif opts:
            label = "–"
            use_text_renderer = True
            is_placeholder = True
        else:
            label = zones[zone][0]
        if zone == rec_zone:
            is_rec = True
            pulse = _ease_sine(_anim_phase / 1.6)
            wash = Image.new("RGB", tile.size, GHIBLI["meadow"])
            tile = Image.blend(tile, wash, 0.25 + pulse * 0.25)
    elif XL_SLASH0 <= key < XL_SLASH0 + len(XL_SLASH):
        # M-SD2: slash-command keys (R1 C4-C8). Cool blue tint matches the
        # non-cinema (28,38,60) background; use_text_renderer auto-shrinks
        # so "/super-worker" fits without overflow.
        label = XL_SLASH[key - XL_SLASH0]["label"]
        sub = None
        use_text_renderer = True
        tint = Image.new("RGB", tile.size, (28, 38, 60))
        tile = Image.blend(tile, tint, 0.55)
    elif XL_QUICK0 <= key < XL_QUICK0 + len(XL_QUICK):
        label, sub = XL_QUICK[key - XL_QUICK0]["label"], None
    elif key == XL_STATUS:
        label, sub = "Status", None
        tint = Image.new("RGB", tile.size, (40, 60, 50))
        tile = Image.blend(tile, tint, 0.55)
    elif XL_GOAL0 <= key < XL_GOAL0 + len(XL_GOAL):
        label, sub = XL_GOAL[key - XL_GOAL0]["label"], None
        use_text_renderer = True
        tint = Image.new("RGB", tile.size, (50, 40, 60))
        tile = Image.blend(tile, tint, 0.55)
    elif key == XL_TOOL_SWAP:
        label, sub = "Swap", None
        tint = Image.new("RGB", tile.size, (60, 50, 30))
        tile = Image.blend(tile, tint, 0.55)
    else:
        label = ""   # any non-action key: blank tile
    if use_text_renderer:
        # Wrap-able option label needs a flatter background than drop-shadows
        # provide over a busy panorama. Skip the extra dark wash on the rec
        # zone (Golden Meadow already supplies contrast there).
        if not is_rec:
            dft = Image.new("RGB", tile.size, (10, 12, 18))
            tile = Image.blend(tile, dft, 0.45)
        d = ImageDraw.Draw(tile)
        if is_rec:
            fill = (25, 20, 10)
        elif is_placeholder:
            fill = (90, 95, 105)
        else:
            fill = TXT_BANNER
        _render_text(d, tile, label, text_fill=fill, size=18)
        return tile
    d = ImageDraw.Draw(tile)
    f = ImageFont.truetype(FONT_B, 26)
    cx, cy = tile.width / 2, tile.height / 2 - (7 if sub else 0)
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        d.text((cx + dx, cy + dy), label, font=f, anchor="mm", fill=(0, 0, 0))
    d.text((cx, cy), label, font=f, anchor="mm", fill=TXT_BANNER)
    if sub:
        sf = ImageFont.truetype(FONT_R, 12)
        sy = cy + 18
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            d.text((cx + dx, sy + dy), sub, font=sf, anchor="mm", fill=(0, 0, 0))
        d.text((cx, sy), sub, font=sf, anchor="mm", fill=(210, 215, 225))
    return tile

def _animate_cinema_xl(deck):
    """XL+ cinema: the Laputa-siege scene spans all 36 keys as one continuous
    panorama (9x4 canvas, no touchscreen to carry the banner). Board slots
    (XL_BOARD_SLOTS) overlay their session tile (title/status/activity/needy-wash/
    selector) ON the scene; row 1 (keys 9-17) overlays the answer/control labels —
    the scene is never interrupted, alerts wash over it. Per-key _frame_cache dedup
    bounds the 36-key HID load."""
    scene = ghibli.render_scene(_anim_phase)
    canvas = ghibli.scale_to_canvas_xl(scene)
    tiles = ghibli.slice_tiles_xl(canvas)
    with _lock:
        sess = list(_sessions[:len(XL_BOARD_SLOTS)])
        active = _active_id
    s_act = active_session()
    rec_zone = (_activity.get(active, (None, False, None))[2]
                if _reply_set == 0 and s_act is not None else None)
    for i in range(deck.key_count()):
        tile = tiles[i].copy()
        j = XL_SLOT_OF_KEY.get(i)
        if j is None:
            tile = _overlay_xl_control(tile, i, rec_zone)
        elif j < len(sess):
            s = sess[j]
            label, needs, _rec = _activity.get(s["id"], (s.get("status", "idle"), False, None))
            urg = _urgency.get(s["id"], "menu") if needs else None
            st = s.get("status", "idle")
            thinking = (label == "thinking" or st in ("running", "starting"))
            # AUTO-FOCUS gating: only the currently focused session (the one on
            # the LCD) breathes. Other needy sessions get a STATIC accent wash
            # + steady border so they still signal "needs input" but don't
            # compete for attention with the auto-focus target.
            is_focus = (s["id"] == active)
            accent = GHIBLI["meadow"] if urg in ("menu", "urgent") else GHIBLI["rose"]
            period = 1.6 if urg in ("menu", "urgent") else 3.0
            pulse = _ease_sine(_anim_phase / period) if needs else 0.0
            if needs and is_focus:
                # Breathing wash — auto-focus target only.
                wash = Image.new("RGB", tile.size, accent)
                tile = Image.blend(tile, wash, pulse * 0.35)
            elif needs:
                # Static faint wash for other needy sessions (no pulse).
                wash = Image.new("RGB", tile.size, accent)
                tile = Image.blend(tile, wash, 0.18)
            d = ImageDraw.Draw(tile)
            # Title: auto-focus gets a centered 22pt title (with sub-label),
            # everyone else keeps the top-left 13pt overlay. The centered
            # treatment makes the active needy session instantly findable.
            if needs and is_focus:
                _overlay_title_centered(d, tile, s.get("title", "?"),
                                        sub=str(label)[:18])
            else:
                _overlay_title(d, tile, s.get("title", "?"))
            _overlay_status_dot(d, tile, st)
            # Skip the top-left activity overlay when the centered title is
            # already showing the activity as a sub-label — otherwise the
            # auto-focused needy session renders "choose…" twice (once in the
            # top-left spinner row, once beneath the centered title).
            if thinking and not (needs and is_focus):
                _overlay_activity(d, tile, _anim_phase, label=str(label))
            if needs:
                # Border: breathing lerp for focus, fixed 0.65 lerp otherwise.
                border_lerp = (0.65 + pulse * 0.35) if is_focus else 0.65
                border_c = _lerp_color((20, 20, 28), accent, border_lerp)
                d.rectangle([2, 2, tile.width - 3, tile.height - 3],
                            outline=border_c, width=3)
            if is_focus:
                _draw_selector(d, tile)
        # else: empty board slot -> pure scene tile (the siege plays through).
        frame = PILHelper.to_native_key_format(deck, tile)
        if _frame_cache.get(i) != frame:
            _frame_cache[i] = frame
            deck.set_key_image(i, frame)

def animate_active_keys(deck):
    """Render every key each tick. In cinema mode the full grid is one continuous
    8-bit Ghibli scene; otherwise per-key state animations (pulse/spinner/shimmer)."""
    if IS_XL:
        if _lcd_mode != "normal" and _ui_mode == "board":
            _animate_cinema_xl(deck)
        else:
            _animate_xl(deck)
        return
    if _lcd_mode != "normal" and _ui_mode == "board":
        _animate_cinema(deck)
        return
    _sync_page_to_active()
    with _lock:
        sess = list(_sessions[_page * PAGE_SIZE:(_page + 1) * PAGE_SIZE])
        active = _active_id
    s_act = active_session()
    # Not gated on _reply_set: the bottom-row keys always show the "select"
    # set regardless of what the touchscreen is cycling through, so the
    # recommended-zone highlight should always be eligible on the keys too.
    rec_zone = _activity.get(active, (None, False, None))[2] if s_act is not None else None
    for i in range(deck.key_count()):
        if i >= PAGE_SIZE:
            frame = _render_reply_key(deck, i - PAGE_SIZE, rec_zone)
        elif i < len(sess):
            frame = _render_session(deck, sess[i], sess[i]["id"] == active)
        else:
            frame = _centered(deck, EMPTY_COLOR, "+", size=40, sub="new")
        if _frame_cache.get(i) != frame:
            _frame_cache[i] = frame
            deck.set_key_image(i, frame)

def paint_menu(deck, items):
    for i in range(deck.key_count()):
        if i == _cancel_key:
            deck.set_key_image(i, _centered(deck, CANCEL_COLOR, "Cancel", size=18))
        elif i < len(items):
            deck.set_key_image(i, _centered(deck, MENU_COLOR, items[i][0], size=18))
        else:
            deck.set_key_image(i, _key_native_blank(deck))

def _key_native_blank(deck):
    return PILHelper.to_native_key_format(deck, _key_img(deck, (8, 9, 12)))

def render_touchscreen(deck):
    if IS_XL and not HAS_DIALS:
        return                                  # original XL has no touchscreen; XL+ does
    img = PILHelper.create_touchscreen_image(deck)
    if img.width == 0 or img.height == 0:
        return                                  # XL+ driver doesn't expose touchscreen geometry yet (library gap)
    global _ts_diag_logged
    if not _ts_diag_logged:
        log("render_touchscreen: img.size=%s IS_XL=%s HAS_DIALS=%s deck_type=%s",
            img.size, IS_XL, HAS_DIALS, deck.deck_type())
        _ts_diag_logged = True
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, img.width, img.height], fill=(6, 7, 12))
    if _ui_mode == "tool":
        d.text((16, 30), "pick agent for new session  ·  Cancel = key 8",
               font=ImageFont.truetype(FONT_B, 24), fill=(95, 140, 180))
    elif _ui_mode == "place":
        _what = (_pending_tool[0] if _pending_tool
                 else _pending_session.get("title", "session") if _pending_session else "")
        d.text((16, 30), "placement for '%s'  ·  Cancel = key 8" % _what,
               font=ImageFont.truetype(FONT_B, 24), fill=(95, 140, 180))
    else:
        # Board mode: panorama (laputa or beach) or flat dark (normal).
        _lcd_drop_shadows = _lcd_mode != "normal"
        if _lcd_mode == "laputa":
            banner = ghibli.render_touchscreen_banner(_anim_phase)
            img.paste(banner, (0, 0))
            d = ImageDraw.Draw(img)
            txt_c = TXT_BANNER
        elif _lcd_mode == "beach":
            _hour = time.localtime().tm_hour + time.localtime().tm_min / 60.0
            beach = ghibli_beach.render_fort_myers_beach(_anim_phase, _weather_snapshot(), _hour)
            img.paste(beach, (0, 0))
            d = ImageDraw.Draw(img)
            txt_c = TXT_BANNER
        else:
            txt_c = (95, 140, 180)
        s = active_session()
        if s:
            st = s.get("status", "idle")
            if not _lcd_drop_shadows:
                d.rectangle([0, 0, 8, img.height], fill=STATE_COLOR.get(st, (40, 42, 50)))
            # LCD improvement: show live activity label (e.g. "Wrangling 1m 20s")
            # instead of the raw status ("running") when activity data is available.
            _lbl = _activity.get(s["id"], (None,))[0]
            head = "▶ {}  ·  {}".format(s.get("title", "?"), _lbl or st)
            sess_name = s.get("title", "?")
        else:
            if _lcd_mode == "laputa":
                head = "▶ Laputa Siege  ·  cinema"
            elif _lcd_mode == "beach":
                head = "▶ the coast  ·  live"
            else:
                head = "▶ no session selected"
            sess_name = "—"
        # Drop-shadow text when on the banner (JPEG has no alpha; 4-direction
        # shadow makes any text readable over the scene without a backing rect).
        if _lcd_drop_shadows:
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                d.text((16 + dx, 6 + dy), head, font=ImageFont.truetype(FONT_B, 24), fill=(0, 0, 0))
        d.text((16, 6), head, font=ImageFont.truetype(FONT_B, 24), fill=txt_c)
        # Change 6a — time + date in top-right (replaces the old setinfo draw;
        # reply-set info has moved into knob zone 2 per Change 4).
        _timestr = time.strftime("%a %H:%M:%S")           # "Fri 14:32:07"
        if _lcd_drop_shadows:
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                d.text((img.width - 12 + dx, 8 + dy), _timestr,
                       font=ImageFont.truetype(FONT_R, 16), anchor="ra", fill=(0, 0, 0))
        d.text((img.width - 12, 8), _timestr,
               font=ImageFont.truetype(FONT_R, 16), anchor="ra", fill=txt_c)
        # M-SD10: needy-count badge — shows "⚠ N waiting" when >1 session needs
        # input, so the breathing pulse on a single key isn't the only signal.
        # Hidden when count ≤ 1 (no noise during normal single-session use).
        with _lock:
            _needy = sum(1 for s in _sessions if s["id"] in _urgency)
        if _needy > 1:
            _badge = "\u26a0 %d waiting" % _needy
            _bx = img.width - 135
            _bf = ImageFont.truetype(FONT_R, 14)
            # LCD improvement: pulse the badge (1.0s period) to draw the eye.
            _pulse = 0.5 + 0.5 * math.sin(_anim_phase / 1.0 * 2 * math.pi)
            _bc = tuple(int(c * (0.55 + 0.45 * _pulse)) for c in (220, 180, 60))
            if _lcd_drop_shadows:
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    d.text((_bx + dx, 9 + dy), _badge, font=_bf, anchor="ra", fill=(0, 0, 0))
            d.text((_bx, 9), _badge, font=_bf, anchor="ra", fill=_bc)
        # M-SD8: reply-preview strip — when the active session has a live menu,
        # show the 4 options on the LCD at y=28-42 so the user can read them
        # before glancing at the physical keys. Replaces the heartbeat + host
        # dots row for the duration of the menu (dots return when menu closes).
        _opts = _active_menu_opts()
        if _opts:
            # LCD improvement: highlight the ❯ cursor option (rec_zone), not
            # always option 1. rec_zone comes from RECO_RE parsing in
            # session_activity — tracks which option Claude Code recommends.
            _rec = _activity.get(_active_id, (None, False, None))[2]
            ow = img.width / 4
            for i, opt in enumerate(_opts):
                x0 = int(i * ow)
                num = "%d" % (i + 1)
                label = "%s  %s" % (num, opt[:24]) if opt else "%s  —" % num
                if opt and i == _rec:
                    color = (95, 220, 140)    # cursor-recommended: bright green
                elif opt:
                    color = (140, 180, 200)   # other valid options: muted blue
                else:
                    color = (60, 60, 60)      # empty zone: dim
                f = ImageFont.truetype(FONT_R, 14)
                if _lcd_drop_shadows:
                    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        d.text((x0 + 10 + dx, 28 + dy), label, font=f, fill=(0, 0, 0))
                d.text((x0 + 10, 28), label, font=f, fill=color)
        else:
            # Change 7 — agent heartbeat pulses: up to MAX_SESSIONS dots at y=28,
            # leftmost = active. pulse phase reuses the existing _anim_phase global
            # so all heartbeats breathe in sync with the rest of the cinema animation.
            with _lock:
                sess_snapshot = list(_sessions[:MAX_SESSIONS])
                active = _active_id
            for i, s in enumerate(sess_snapshot):
                sid = s["id"]; st = s.get("status", "idle")
                base = STATE_COLOR.get(st, (40, 42, 50))
                if st in ("running", "starting"):
                    b = 0.5 + 0.5 * (0.5 + 0.5 * math.sin(_anim_phase / 1.6 * 2 * math.pi))
                    fill = tuple(int(c * b + 20 * (1 - b)) for c in base)
                elif st == "error":
                    fill = (200, 60, 60)
                elif st == "done":
                    fill = (40, 180, 80)
                else:
                    fill = tuple(int(c * 0.4) for c in base)
                x = 16 + i * 12
                d.ellipse([x, 28, x + 8, 36], fill=fill)
                if sid == active:
                    d.rectangle([x - 1, 27, x + 9, 37], outline=(220, 220, 220))
            # Change 6b — SSH host health dots at y=30.
            _hosts = ["the-deck-host", "server-host", "nas-host", "git-host"]
            with _host_status_lock:
                states = [_host_status[h] for h in _hosts]
            _host_x0 = 16 + len(sess_snapshot) * 12 + 8
            for i, up in enumerate(states):
                color = (40, 180, 80) if up else (200, 60, 60)
                d.ellipse([_host_x0 + i * 14, 30, _host_x0 + 8 + i * 14, 38], fill=color)
        # Load avg read once per frame — used in knob zone 2 below (normal mode only).
        # In panorama modes (laputa/beach) the whole y=44-100 strip is the scene,
        # so none of the knob zones are drawn.
        if _lcd_mode == "normal":
            try:
                with open("/proc/loadavg") as _f:
                    load1 = float(_f.read().split()[0])
            except Exception:
                load1 = 0.0
            # Change 4 — board mode: 5 visual zones. Knob 3 hardware (page) keeps
            # working but its label was dropped; zone 3 was repurposed for the
            # weather + grill display (400px). System stats (load/cpu/mem) merged
            # into one zone (was zones 4+5, now zone 2).
            # ponytail: zone widths non-uniform — pg zone absorbed into weather;
            # upgrade: re-add pg label if paging becomes a real workflow.
            setname = REPLY_SETS[_reply_set][0]
            _cpu_val = _cpu_pct()
            _mem_val = _mem_pct()
            sys_label = "%.1f  %d%%  %d%%" % (load1, _cpu_val, _mem_val)
            knob_labels = [
                sess_name,
                "%s %d/%d" % (setname, _reply_set + 1, len(REPLY_SETS)),
                sys_label,
                None,                    # zone 3 = weather (drawn separately, no text label)
                "%d%%" % _brightness,
            ]
            half = img.width / 6        # canonical 200px unit (1200 / 6)
            zone_widths = [half, half, half, half * 2, half]   # [200, 200, 200, 400, 200] = 1200
            x0 = 0
            for i, label in enumerate(knob_labels):
                zw = zone_widths[i]
                if i:
                    d.line([(x0, 44), (x0, img.height)], fill=(20, 22, 28), width=2)
                # Zone 2 (system): 3 stacked mini-bars — load (blue), cpu (amber), mem (purple).
                if i == 2:
                    bar_w = zw - 16
                    # load: scale 0..8 to bar width
                    bw_load = int(bar_w * min(load1 / 8.0, 1.0))
                    d.rectangle([x0 + 8, 48, x0 + 8 + bw_load, 53], fill=(95, 140, 180))
                    # cpu
                    bw_cpu = int(bar_w * (_cpu_val / 100.0))
                    d.rectangle([x0 + 8, 55, x0 + 8 + bw_cpu, 60], fill=(180, 140, 60))
                    # mem
                    bw_mem = int(bar_w * (_mem_val / 100.0))
                    d.rectangle([x0 + 8, 62, x0 + 8 + bw_mem, 67], fill=(140, 100, 180))
                # Zone 3 (weather): animated condition icon + temp/word/city + grill badge.
                if i == 3:
                    _render_weather_lcd_zone(d, x0, 44, zw, img.height - 44, _anim_phase)
                # Zone 4 (brightness): single bar.
                if i == 4:
                    bw = int((zw - 16) * (_brightness / 100.0))
                    d.rectangle([x0 + 8, 48, x0 + 8 + bw, 56], fill=(95, 140, 180))
                # Text label (skipped for weather zone — it has its own text).
                if label is not None:
                    d.text((x0 + zw / 2, 84), label,
                           font=ImageFont.truetype(FONT_R, 13), anchor="mm", fill=txt_c)
                x0 += zw
    native = PILHelper.to_native_touchscreen_format(deck, img)
    # Dedup like animate_active_keys does per-key: the touchscreen image is far
    # bigger than a key icon, so pushing it unconditionally every 20fps tick
    # (most of which are unchanged) keeps the USB HID pipe busy with redundant
    # writes. That queues up ahead of the tick that actually carries the new
    # session name after an auto-focus switch, which is why the key border
    # snapped instantly but the name lagged behind it by up to one render tick
    # (or worse under contention). Skipping unchanged frames clears the queue
    # so the real change gets sent with nothing ahead of it.
    global _touch_frame_cache
    if native == _touch_frame_cache:
        return
    _touch_frame_cache = native
    global _ts_send_logged
    try:
        deck.set_touchscreen_image(native, 0, 0, img.width, img.height)
        if not _ts_send_logged:
            log("set_touchscreen_image OK: %d bytes, geom=%dx%d", len(native), img.width, img.height)
            _ts_send_logged = True
    except Exception as e:
        log("set_touchscreen_image ERROR: %s", e)

def repaint(deck):
    if _ui_mode == "tool":
        paint_menu(deck, TOOLS)
    elif _ui_mode == "place":
        paint_menu(deck, PLACEMENTS)
    else:
        paint_board(deck)
    render_touchscreen(deck)

# ---- callbacks ------------------------------------------------------------
def _wake_and_note(deck):
    """Record input time; if the display is asleep, wake it and report True so the
    caller consumes this event (the waking press/turn just wakes, no action)."""
    global _last_input, _asleep
    _last_input = time.monotonic()
    if _asleep:
        _asleep = False
        deck.set_brightness(_brightness)
        log("display wake"); repaint(deck)
        return True
    return False

def _safe_callback(fn):
    """The StreamDeck library's internal read thread has no exception guard
    around key/dial/touch callbacks — an uncaught exception silently kills
    that thread forever. systemd still sees the (now input-dead) main process
    as healthy, so nothing restarts it. One bad callback = total, invisible
    input failure until someone notices and manually restarts. Log and
    swallow instead of letting that thread die."""
    def wrapped(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception:
            log("! %s callback crashed:\n%s", fn.__name__, traceback.format_exc())
    return wrapped

@_safe_callback
def on_key(deck, key, pressed):
    if not pressed:
        return
    if _wake_and_note(deck):
        return
    global _active_id, _pending_tool
    if _ui_mode == "tool":
        if key == CANCEL_KEY:
            close_menu()
        elif key < len(TOOLS):
            _pending_tool = TOOLS[key]; open_menu("place")
        repaint(deck); return
    if _ui_mode == "place":
        if key == CANCEL_KEY:
            close_menu()
        elif key < len(PLACEMENTS):
            mode = PLACEMENTS[key][1]
            if _pending_tool:                       # spawn a NEW session
                tool = _pending_tool; close_menu(); _bg(spawn, tool, mode)
            elif _pending_session:                  # (re)open an EXISTING session
                s = _pending_session; close_menu(); _bg(open_existing, s, mode)
            else:
                close_menu()
        repaint(deck); return
    # board mode. Top row (0-3) = paginated session picks; bottom row (4-7) =
    # always the 4 reply zones, mirroring the touchscreen strip.
    if key >= PAGE_SIZE:
        _bg(act_reply, key - PAGE_SIZE, reply_set=0); return
    with _lock:
        sess = list(_sessions[_page * PAGE_SIZE:(_page + 1) * PAGE_SIZE])
        active = _active_id
    if key < len(sess):
        s = sess[key]
        if s["id"] == active:
            _bg(toggle_or_place, deck, s)
        else:
            # State-only: main 20fps loop renders the new selector next tick.
            with _lock:
                _active_id = s["id"]
    else:
        open_menu("tool"); repaint(deck)

# M-SD3: long-press detection — state + helper.
_press_ts = {}       # key -> monotonic timestamp of key-down edge
_long_fired = set()  # keys whose long-press action already fired (cleared on key-up)
_long_timers = {}    # key -> threading.Timer handle for the pending long-press

# Keys with a long-press action. key index -> (action callable, threshold seconds).
# M-SD3 seeds key 7 (last slot of row 0) with cinema-mode toggle — the feature
# the module comment at line 349 described but never wired. M-SD4 adds:
# key 18 (Esc) → force-kill, key 21 (Go) → git-push, key 17 (/super-worker) →
# cycle-reply, and all other board slots → tail agentdeck log in a new pane.
def _cycle_lcd_mode():
    """Long-press key 7: cycle normal → laputa → beach → normal."""
    global _lcd_mode, _touch_frame_cache
    order = ["normal", "laputa", "beach"]
    i = order.index(_lcd_mode) if _lcd_mode in order else 0
    _lcd_mode = order[(i + 1) % len(order)]
    _touch_frame_cache = None    # force immediate repaint (skip dedup)
    log("lcd mode -> %s (long-press key 7)", _lcd_mode)

def _force_kill():
    """SIGTERM the foreground process of the active session's tmux pane."""
    s = active_session()
    t = (s or {}).get("tmux_session")
    if not t:
        log("force-kill: no active tmux session"); return
    r = _run(["tmux", "display-message", "-p", "-t", t, "#{pane_pid}"], timeout=5)
    pid = (r.stdout if r and r.returncode == 0 else "").strip()
    if not pid.isdigit():
        log("force-kill: no pane_pid for %s", t); return
    _run(["kill", "-TERM", pid], timeout=5)
    log("force-kill -> %s pid=%s", t, pid)

def _git_push():
    """Send `git push origin <current-branch>` to the active session's pane.
    Pre-checks the pane's CWD via tmux display-message — no-ops with a log
    line if CWD is not inside a git work tree (M-SD4 contract: 'logs + no-op')."""
    s = active_session()
    if not s:
        log("git-push: no active session"); return
    t = s.get("tmux_session")
    if t:
        r = _run(["tmux", "display-message", "-p", "-t", t, "#{pane_current_path}"], timeout=5)
        cwd = (r.stdout if r and r.returncode == 0 else "").strip()
        if cwd:
            gr = _run(["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"], timeout=5)
            if not (gr and gr.returncode == 0 and gr.stdout.strip() == "true"):
                log("git-push: skip — CWD %s not a git work tree", cwd); return
    tmux_send_text(s, "git push origin $(git branch --show-current)")
    tmux_send(s, ["Enter"])
    log("git-push -> %s", s.get("title"))

def _cycle_reply():
    global _reply_set
    _reply_set = (_reply_set + 1) % len(REPLY_SETS)
    log("reply set -> %d %s (long-press)", _reply_set, REPLY_SETS[_reply_set][0])

def _tail_agent_log():
    """Open a 30%-height tmux pane tailing the agentdeck journal in the active
    session's window. Useful for debugging render/input issues on the fly."""
    s = active_session()
    t = (s or {}).get("tmux_session")
    if not t:
        log("tail-log: no active tmux session"); return
    _run(["tmux", "split-window", "-p", "30", "-t", t,
          "journalctl --user -u streamdeck-agentdeck -f"], timeout=5)
    log("tail-log -> new pane in %s", t)

def _status_blast():
    """M-SD5: open a tmux split running ~/bin/status-blast in the active
    session's window. Shows homelab uptime/mem/disk/services at a glance.
    `read -p` holds the pane open until the user presses Enter."""
    s = active_session()
    t = (s or {}).get("tmux_session")
    if not t:
        log("status-blast: no active tmux session"); return
    cmd = "bash ~/bin/status-blast; echo; read -p 'Press Enter to close...'"
    _run(["tmux", "split-window", "-p", "40", "-t", t, cmd], timeout=5)
    log("status-blast -> new pane in %s", t)

# M-SD6: tool swap — cycle the focused session's CLI tool.
_TOOL_CYCLE = ["claude", "glm", "gpt", "local"]
_TOOL_CMDS = {t: c for t, c in [(l, _remote(cmd)) for l, cmd in
    [("claude", "claude"), ("glm", "claude-glm"), ("gpt", "claude-gpt"), ("local", "oc-start")]]}
_tool_swap_at = {}  # session id -> monotonic ts of last swap (10s cooldown)

def _cycle_tool():
    """M-SD6: cycle the focused session's tool claude→glm→gpt→local→claude.
    Sends Ctrl-C to kill the current CLI, then launches the next tool in the
    same tmux pane. 10s cooldown prevents rapid-cycle thrash.
    ponytail: Ctrl-C + relaunch over kill+respawn — preserves pane/window
    without D-Bus gymnastics. Upgrade: proper _close_session + spawn if
    pane recycling proves unreliable."""
    s = active_session()
    if not s:
        log("tool-swap: no active session"); return
    sid = s["id"]
    now = time.monotonic()
    if now - _tool_swap_at.get(sid, 0) < 10.0:
        log("tool-swap: cooldown for %s", s.get("title")); return
    _tool_swap_at[sid] = now
    cur = s.get("tool", "claude")
    try:
        idx = _TOOL_CYCLE.index(cur)
    except ValueError:
        idx = 0
    nxt = _TOOL_CYCLE[(idx + 1) % len(_TOOL_CYCLE)]
    cmd = _TOOL_CMDS.get(nxt, _TOOL_CMDS["claude"])
    tmux_send(s, ["C-c"])
    time.sleep(0.5)
    tmux_send_text(s, cmd)
    tmux_send(s, ["Enter"])
    log("tool-swap %s -> %s in %s", cur, nxt, s.get("title"))

def _manual_prune():
    """M-SD7: manually invoke _prune_dead to catch zombie panes that survived
    the automatic sweep. No-ops (with a log line) when nothing is pruned.
    Reports both pruned count and survivors per contract done-bar."""
    pre = fetch_sessions()
    survivors = _prune_dead(pre)
    n_pruned = len(pre) - len(survivors)
    if n_pruned:
        log("manual-prune: pruned %d session(s), %d remain" % (n_pruned, len(survivors)))
    else:
        log("manual-prune: nothing to prune (%d sessions all healthy)" % len(survivors))

_LONG_PRESS = {
    7:  (_cycle_lcd_mode, 0.6),
    17: (_cycle_reply,    0.6),   # R1 C8 /super-worker long-press
    18: (_force_kill,     0.6),   # R2 C0 Esc long-press
    21: (_git_push,       0.6),   # R2 C3 Go long-press
    35: (_manual_prune,   0.6),   # R3 C8 Status long-press (M-SD7)
}
# M-SD4: all board slots except key 7 (cinema toggle) get tail-log on long-press.
for _k in XL_BOARD_SLOTS:
    if _k not in _LONG_PRESS:
        _LONG_PRESS[_k] = (_tail_agent_log, 0.6)

def _track_press(key, pressed):
    """M-SD3: long-press tracker. Call on BOTH edges for every key event.
    Returns True to SUPPRESS the short-tap action, False to allow it.

    Key-down: if the key is long-press-aware, starts a daemon timer and
    returns True (suppress — discriminate on key-up). Otherwise returns False
    (fire short immediately, as before M-SD3).

    Key-up: cancels the pending timer. Returns False if the hold was brief
    (short should fire now) or True if the long action already fired
    (suppress the redundant short).

    ponytail: threading.Timer over a poll-loop check — the 20fps render loop
    shouldn't care about press timing. Daemon threads never block shutdown.
    Upgrade: single timer wheel if concurrent holds ever matter (they won't)."""
    now = time.monotonic()
    if pressed:
        _press_ts[key] = now
        spec = _LONG_PRESS.get(key)
        if spec is None:
            return False
        action, threshold = spec
        _long_fired.discard(key)
        def _fire():
            _long_fired.add(key)
            try:
                action()
            except Exception:
                log.exception("long-press action failed for key %d", key)
        t = threading.Timer(threshold, _fire)
        t.daemon = True
        _long_timers[key] = t
        t.start()
        return True
    _press_ts.pop(key, None)
    t = _long_timers.pop(key, None)
    if t is not None:
        t.cancel()
    if key in _long_fired:
        _long_fired.discard(key)
        return True
    return False

@_safe_callback
def on_key_xl(deck, key, pressed):
    """XL+ key handler — everything the Plus put on dials/touchscreen lives on
    keys here. Board slots (XL_BOARD_SLOTS = row 0 + rows 2-3) select on first
    tap and toggle the session's window on a second tap of the active one, exactly
    like the Plus. Row 1 (keys 9-17): 9-12 = reply zones 1/2/3/4 (live
    _reply_set); keys 13-17 = slash-command keys (M-SD2). While a menu is open,
    key 9 (XL_REPLY0) acts as Cancel.

    M-SD3: long-press-aware keys defer their short action to key-up so tap vs
    hold can be discriminated. Non-long-press keys fire on key-down (instant)."""
    if pressed:
        if _wake_and_note(deck):
            return                         # wake tap consumed — no timer, no dispatch
        if _track_press(key, True):
            return                         # long-press-aware: defer short action to key-up
    else:
        if _track_press(key, False):
            return                         # long action already fired → suppress short
        if key not in _LONG_PRESS:
            return                         # non-long-press: handled on key-down
        # brief tap on long-press-aware key → fall through to dispatch
    global _active_id, _pending_tool, _reply_set
    if _ui_mode == "tool":
        if key == _cancel_key:
            close_menu()
        elif key < len(TOOLS):
            _pending_tool = TOOLS[key]; open_menu("place")
        repaint(deck); return
    if _ui_mode == "place":
        if key == _cancel_key:
            close_menu()
        elif key < len(PLACEMENTS):
            mode = PLACEMENTS[key][1]
            if _pending_tool:                       # spawn a NEW session
                tool = _pending_tool; close_menu(); _bg(spawn, tool, mode)
            elif _pending_session:                  # (re)open an EXISTING session
                s = _pending_session; close_menu(); _bg(open_existing, s, mode)
            else:
                close_menu()
        repaint(deck); return
    # board mode — reply strip, slash row, quick controls, then board slots.
    if XL_REPLY0 <= key <= XL_REPLY0 + 3:
        # allow_dismiss=False: slot 2 sends a real "3".
        _bg(act_reply, key - XL_REPLY0, _reply_set, False); return
    if XL_SLASH0 <= key < XL_SLASH0 + len(XL_SLASH):
        # M-SD2: slash-command keys (R1 C4-C8). Fire "/<cmd>" + Enter to the
        # active session via the shared _fire_action dispatcher.
        spec = XL_SLASH[key - XL_SLASH0]
        s = active_session()
        if not s:
            log("slash '%s': no active session", spec["label"]); return
        log("slash '%s' -> %s", spec["label"], s.get("title"))
        if _fire_action(s, spec):
            _advance_focus(s["id"])
        return
    if XL_QUICK0 <= key < XL_QUICK0 + len(XL_QUICK):
        spec = XL_QUICK[key - XL_QUICK0]
        s = active_session()
        if not s:
            log("quick '%s': no active session", spec["label"]); return
        log("quick '%s' -> %s", spec["label"], s.get("title"))
        if _fire_action(s, spec):
            _advance_focus(s["id"])
        return
    if key == XL_STATUS:
        _status_blast(); return
    if XL_GOAL0 <= key < XL_GOAL0 + len(XL_GOAL):
        spec = XL_GOAL[key - XL_GOAL0]
        s = active_session()
        if not s:
            log("goal '%s': no active session", spec["label"]); return
        log("goal '%s' -> %s", spec["label"], s.get("title"))
        _fire_action(s, spec)
        return
    if key == XL_TOOL_SWAP:
        _bg(_cycle_tool); return
    j = XL_SLOT_OF_KEY.get(key)
    if j is None:
        return                                      # out-of-board key
    with _lock:
        sess = list(_sessions[:len(XL_BOARD_SLOTS)])
        active = _active_id
    if j < len(sess):
        s = sess[j]
        if s["id"] == active:
            _bg(toggle_or_place, deck, s)
        else:
            with _lock:
                _active_id = s["id"]
    else:
        open_menu("tool"); repaint(deck)            # empty board slot = new session

@_safe_callback
def on_dial(deck, dial, event, value):
    if _wake_and_note(deck):
        return
    log("on_dial: dial=%d event=%s value=%s active_id=%s", dial, event, value, _active_id[:8] if _active_id else None)
    global _brightness, _reply_set, _page
    if event == DialEventType.TURN:
        if _ui_mode != "board":
            return
        if dial == 0:                                   # knob 1: move selection cursor across sessions
            # State-only update: the 20fps main loop renders the new selector
            # within ~50ms. Calling repaint() here would race the main render
            # thread (both pushing frames concurrently → glitch/flicker).
            global _manual_until
            _manual_until = time.monotonic() + 5.0     # suppress auto-focus for 5s
            select_delta(1 if value > 0 else -1)
        elif dial == 1:                                 # knob 2: cycle reply set
            _reply_set = (_reply_set + (1 if value > 0 else -1)) % len(REPLY_SETS)
            log("reply set -> %d (manual)", _reply_set)
        elif dial == 2:                                 # knob 3: page the top row
            _page = (_page + (1 if value > 0 else -1)) % MAX_PAGES
            log("page -> %d/%d (manual)", _page + 1, MAX_PAGES)
        elif dial == 3:                                 # knob 4: unassigned
            pass
        elif dial == 4:                                 # knob 5: unassigned
            pass
        elif dial == 5:                                 # knob 6: brightness (dimmer)
            _brightness = max(10, min(100, _brightness + (5 if value > 0 else -5)))
            deck.set_brightness(_brightness)
            log("knob 6 (brightness) -> %d", _brightness)
    elif event == DialEventType.PUSH and value and _ui_mode == "board":
        # Knob N push = reply slot N (knobs 1-4 only; knobs 5-6 have no reply slot).
        if dial < 4:
            _bg(act_reply, dial)

@_safe_callback
def on_touch(deck, evt, value):
    if _wake_and_note(deck):
        return
    if _ui_mode != "board" or evt not in (TouchscreenEventType.SHORT, TouchscreenEventType.LONG):
        return
    x = (value or {}).get("x", 0)
    zone = max(0, min(3, int(x // (deck.TOUCHSCREEN_PIXEL_WIDTH / 4))))
    _bg(act_reply, zone)

# ---- main -----------------------------------------------------------------
def main():
    global _sessions, _active_id, _activity, _anim_phase, _last_input, _asleep
    global _reply_set, _needed_since, _urgency
    # Retry HID enumeration+open with backoff (device may not be ready at boot).
    # Replaces crash-loop: 33 systemd restarts were observed when udev hadn't
    # settled the HID node yet; this keeps the process alive instead.
    global IS_XL, HAS_DIALS, _cancel_key, _lcd_mode
    _plus = None
    for _attempt in range(30):
        try:
            decks = DeviceManager().enumerate()
            # Accept the Plus ("+" in the name, has dials + touchscreen) OR the
            # XL (32 keys, no dials/touch). XL replaces the Plus on this rig, so
            # the first Stream Deck of either kind wins.
            _plus = next((d for d in decks if "Stream Deck" in d.deck_type()), None)
            if _plus:
                _plus.open()
                break
        except Exception as _e:
            log("HID open attempt %d failed: %s", _attempt + 1, _e)
        if _attempt >= 29:
            log("HID open failed after 30 attempts; giving up"); sys.exit(1)
        _wait = min(2 ** _attempt, 30)
        log("retrying HID in %ds", _wait)
        time.sleep(_wait)
    plus = _plus
    if not plus:
        log("no Stream Deck found"); sys.exit(1)
    IS_XL = "XL" in plus.deck_type()
    HAS_DIALS = "+" in plus.deck_type()          # XL+ and Plus have dials+touch
    plus.reset(); plus.set_brightness(_brightness)
    if IS_XL:
        # XL: all input on keys; menu-cancel lives on the first reply key
        # (XL_REPLY0 = key 9). Reply set's 4th label is "4" (was "Go"; the
        # Go action moved to the always-visible quick-control strip at key 18).
        # LCD panorama default — "beach" boots into the the city live scene.
        # Long-press key 7 cycles normal → laputa → beach → normal. Keys 13-17
        # are intentionally dead (slash-key strip is handled on the physical keys).
        _lcd_mode = "beach"
        _cancel_key = XL_REPLY0
        plus.set_key_callback(on_key_xl)
        if HAS_DIALS:
            plus.set_dial_callback(on_dial)
            plus.set_touchscreen_callback(on_touch)
            log("Stream Deck XL+ detected (%s, %d keys + dials) — hybrid layout",
                plus.deck_type(), plus.key_count())
        else:
            log("Stream Deck XL detected (%s, %d keys) — key-only layout",
                plus.deck_type(), plus.key_count())
    else:
        plus.set_key_callback(on_key)
        plus.set_dial_callback(on_dial)
        plus.set_touchscreen_callback(on_touch)
        log("Stream Deck Plus detected (%s) — dials + touchscreen layout",
            plus.deck_type())

    _load_win_map()
    _load_dbus_map()
    _load_pane_order()
    _sessions = fetch_sessions()
    _active_id = _sessions[0]["id"] if _sessions else None
    _activity = {s["id"]: session_activity(s) for s in _sessions[:MAX_SESSIONS]}
    _last_input = time.monotonic()
    _bg(_host_status_loop)
    _bg(_weather_loop)
    repaint(plus)
    log("session board ready: %d sessions, tools=%s", len(_sessions),
        [t[0] for t in TOOLS])

    stop = threading.Event()
    def shutdown(*_):
        log("shutting down"); stop.set()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Cadence split: ANIM is the render tick (20 fps for smooth motion);
    # per_refresh keeps the expensive fetch + pane scrape on the slow 2s cadence.
    # The 20fps loop only reads cached state (_sessions/_activity/_urgency) and
    # redraws — animate_active_keys dedupes via _frame_cache so static keys
    # don't flood the HID bus with identical frames.
    # ponytail: single render thread, no GPU/OpenGL — upgrade: glfw framebuffer
    # if we ever exceed 8 keys at 30fps.
    ANIM = 0.05
    per_refresh = max(1, round(REFRESH_SECS / ANIM))   # 40 ticks per state poll
    tick = 0
    global _anim_phase
    while not stop.wait(ANIM):
        # idle sleep: blank the OLEDs after SLEEP_SECS with no input; a callback
        # (key/dial/touch) wakes it back up via _wake_and_note().
        if not _asleep and (time.monotonic() - _last_input) > SLEEP_SECS:
            _asleep = True; plus.set_brightness(0)
            log("display asleep (idle %d min)", SLEEP_SECS // 60)
        if _asleep:
            continue
        tick += 1
        _anim_phase += ANIM                              # seconds, monotonic
        do_refresh = (tick % per_refresh == 0)
        if do_refresh:
            new = _prune_dead(fetch_sessions())
            act = {s["id"]: session_activity(s) for s in new[:MAX_SESSIONS]}
            maybe_remediate(new)                    # auto-restart errored sessions
            now = time.monotonic()
            # Apply "Next" dismissals: a session dismissed via the "Next" zone
            # stops blinking (need forced False) until the agent next goes busy
            # (spinner clears the dismissal inside session_activity), stops
            # needing input, or DISMISS_TIMEOUT elapses. Time-based, not content
            # based — the pane footer drifts every refresh and would clear a
            # fingerprint match instantly.
            for sid in list(act.keys()):
                lbl, need, rec = act[sid]
                ts = _dismissed.get(sid)
                if ts and (not need or now - ts > DISMISS_TIMEOUT):
                    _dismissed.pop(sid, None)
                elif ts and need:
                    act[sid] = (lbl, False, None)
            # Refresh sticky-suggest timestamps so active_is_suggest() bridges
            # the agent-deck running↔waiting status flicker (5s window).
            for sid, (lbl, _n, _r) in act.items():
                if lbl == "suggest…":
                    _suggest_sticky[sid] = now
            # Track when sessions first started needing input (for 10s slow-blink).
            for sid, (label, need, _r) in act.items():
                if need:
                    _needed_since.setdefault(sid, now)
                else:
                    _needed_since.pop(sid, None)
            # Classify urgency:
            #   "menu"    = numbered choice → fast blink, top focus priority
            #   "suggest" = auto-suggest/text prompt → slow blink immediately
            #               (differentiates from fast-blink menus; the "Go"
            #               zone blinks as the recommended accept action)
            #   "urgent"  = text input < 10s → fast blink, secondary focus
            #   "patient" = text input > 10s → slow blink, lowest focus priority
            urg = {}
            for sid, (label, need, _r) in act.items():
                if not need:
                    continue
                if label in ("choose…", "input…"):
                    urg[sid] = "menu"
                elif label == "suggest…":
                    urg[sid] = "patient"
                elif (now - _needed_since.get(sid, now)) < INPUT_TIMEOUT:
                    urg[sid] = "urgent"
                else:
                    urg[sid] = "patient"
            _urgency.clear(); _urgency.update(urg)
            # Auto-focus priority queue: menus → urgent text → patient text.
            # Strict priority: if the currently focused session is needy but at
            # a LOWER priority than the top of the queue, we upgrade instantly.
            # Equal-priority competitors do NOT yank focus (avoids jitter when
            # two menus appear in the same poll). Lower rank number = higher
            # priority. The selector snaps on the next 20fps frame (~50ms).
            focus_order = sorted(
                [sid for sid in act if act[sid][1]],
                key=lambda sid: URG_RANK.get(urg.get(sid), 99),
            )
            choice_id = focus_order[0] if focus_order else None
            # NOT auto-switching the touchscreen to "select" here anymore: the
            # bottom-row keys are permanently pinned to the select set (see
            # act_reply's reply_set override), so menus are always answerable
            # regardless of what the touchscreen shows. Auto-switching used to
            # yank the touchscreen back to "select" on almost every poll (this
            # board fields menu prompts constantly), which looked like it was
            # mirroring the bottom row instead of staying on its own default.
            with _lock:
                _sessions = new; _activity = act
                if _active_id not in [s["id"] for s in new]:
                    _active_id = new[0]["id"] if new else None
                # NOT gated on `manual`: manual is a single global 2s timer
                # re-armed on every interaction anywhere on the board, so
                # during active multi-session use (replies firing every few
                # seconds) it never expires — that silently blocked this
                # entire escalation, starving genuinely higher-priority needs
                # (e.g. a live menu) indefinitely. The strict `choice_rank <
                # cur_rank` inequality below is already the anti-jitter guard
                # the comment describes; `manual` added nothing but the bug.
                if choice_id and _ui_mode == "board" and time.monotonic() > _manual_until:
                    cur_needs = act.get(_active_id, (None, False))[1]
                    cur_rank = URG_RANK.get(urg.get(_active_id), 99) if cur_needs else 99
                    choice_rank = URG_RANK.get(urg.get(choice_id), 99)
                    if choice_rank < cur_rank:
                        _active_id = choice_id
                        log("auto-select session %s (priority %d < %d)",
                            choice_id[:8], choice_rank, cur_rank)
            if _ui_mode != "board" and time.monotonic() > _menu_deadline:
                close_menu()
        try:
            if _ui_mode == "board":
                # 20fps render of every key + touchscreen. animate_active_keys
                # dedupes static frames via _frame_cache so the wire cost stays
                # bounded even though we render all 8 keys every tick.
                animate_active_keys(plus)
                render_touchscreen(plus)
            elif do_refresh:
                # Menus are static between interactions — only repaint on the
                # slow cadence (or when a callback forces it via repaint()).
                repaint(plus)
        except Exception as e:
            log("repaint error: %s", e)
    try:
        plus.reset(); plus.close()
    except Exception:
        pass

if __name__ == "__main__":
    if os.environ.get("_SMOKE"):
        # Smoke test: exercise weather HTTP + parse + decision without the Stream
        # Deck attached. Run on the-host: `_SMOKE=1 python3 deck.py`. Verifies NWS
        # reachability, JSON schema drift, and grilling logic; prints state.
        _weather_poll()
        print("weather:", _weather)
        print("grill:", _grill)
        sys.exit(0)
    main()
