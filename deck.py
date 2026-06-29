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
  DIALS  D0 turn select | push reply-1 · D1 turn reply-set | push reply-2
         D2 push focus terminal (manual typing) · D3 turn brightness | push Esc

Config = the TOOLS / PLACEMENTS / REPLY_ZONES lists below. ponytail: state in
module globals + a lock, no config file — upgrade: external file only if these
must change without a restart.
"""
import os, re, sys, json, time, math, signal, shutil, subprocess, threading, logging

from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper
from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
from PIL import Image, ImageDraw, ImageFont
import ghibli_scenes as ghibli

AD = os.path.expanduser("~/.local/bin/agent-deck")
NEW_SESSION_DIR = os.path.expanduser("~")
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_R = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
if not os.path.exists(FONT_R):
    FONT_R = FONT_B
REFRESH_SECS = 2
MENU_TIMEOUT = 12          # seconds before an open picker reverts to the board
SLEEP_SECS = 3600         # idle seconds before the OLEDs blank (wake on any input)

# All agent commands run on BatKave over `ssh -t` (PTY): the agent-deck session
# lives on k11 (so it's on the board and you reply via tmux send-keys), but the
# agent process runs on BatKave where every tool's stack is installed. A login
# shell (`bash -lc`) puts ~/bin and ~/.local/bin on the remote PATH.
# ponytail: hardcoded tool list — upgrade: read from config.toml when it grows.
SSH_HOST = "batkave"

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
                ("Go", ["Tab", "~0.5", "Enter"])]),
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

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("deck").info

_lock = threading.Lock()
_sessions = []
_active_id = None
_brightness = 60
_ui_mode = "board"         # board | tool | place
_pending_tool = None       # (label, command) chosen in the tool menu -> spawn new
_pending_session = None    # existing session chosen to (re)open in a placement
_menu_deadline = 0.0
_win_map = {}              # session id -> konsole process pid we opened for it
_reply_set = 0             # index into REPLY_SETS (cycled by knob 2 scroll)
_manual_until = 0.0        # monotonic deadline; suppress auto-switch/select after user input
MANUAL_GRACE = 2.0         # seconds the deck respects manual selection before resuming auto
_activity = {}             # session id -> (label, needs_choice, rec_zone) from pane parsing
_needed_since = {}         # session id -> monotonic timestamp when first detected as needing input
_urgency = {}              # session id -> "menu" | "urgent" | "patient" (blink speed + focus)
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
# Cinema mode: the full 4x2 key grid + touchscreen become ONE continuous canvas
# playing an 8-bit Ghibli battle scene (Laputa Siege at Golden Hour). Sessions
# needing input "break through" the canvas with a Ghibli-accent wash + pulsing
# border instead of replacing the tile entirely, so the epic scene is never
# interrupted. Toggle via key 7 long-press when on the board with no menu open.
_cinema_mode = True
# Auto-suggest dismissal: "Next" clears the input gate (stop blinking, drop
# from focus queue) WITHOUT sending keys to the agent. Time-based: holds until
# the agent goes busy again (spinner detected → rearm) or stops needing input.
# Content-fingerprint matching was too fragile — Claude Code's status footer
# ("· 1 shell · ← for agents") drifts every refresh, clearing the dismiss at
# once. Sticky-suggest bridges agent-deck's running↔waiting flicker so the
# "Next" label and a safe slot-2 press survive the noise.
_dismissed = {}            # session id -> monotonic dismiss timestamp
_suggest_sticky = {}       # session id -> monotonic timestamp of last "suggest…" label
_pruned = {}               # session id -> monotonic timestamp (suppress re-prune noise)
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
    "local":  'curl -sf --max-time 3 http://localhost:11434/health >/dev/null 2>&1 || pgrep -x ollama >/dev/null 2>&1',
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
    # Fixed layout matching the user's Konsole tab order. Unlisted sessions
    # go to the end (sorted by creation time among themselves).
    SESSION_ORDER = ["claude-glm", "glm-2", "claude-2", "claude-glm (2)",
                     "claude", "local-2", "glm"]
    order = {t: i for i, t in enumerate(SESSION_ORDER)}
    data.sort(key=lambda s: (order.get(s.get("title", ""), len(SESSION_ORDER)),
                             s.get("created_at", "")))
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
        if is_ssh and (pane_dead == "1" or cmd != "ssh"):
            dead_ids.add(sid)
            log("prune dead '%s' (SSH closed, pane now %s/%s)",
                s.get("title"), cmd, pane_dead)
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

def _voice_toggle(sess):
    """Voice dictation via batkave (mic is there). First press starts recording;
    second press stops, transcribes on batkave, and injects the result into the
    session via tmux send-keys -l (no Enter, so the user reviews before submit)."""
    ssh = ["ssh", "-o", "ConnectTimeout=5", SSH_HOST]
    # Are we currently recording? (PID file exists on batkave)
    r = _run(ssh + ["test -f /tmp/voice-glm-rec.pid"], timeout=8)
    if r and r.returncode != 0:
        # Not recording — start
        _run(ssh + ["~/.local/bin/voice-glm.sh"], timeout=10)
        log("voice: recording started on %s", SSH_HOST); return
    # Recording — stop + transcribe (konsole-send will fail harmlessly on batkave;
    # the transcript is written before that call)
    _run(ssh + ["~/.local/bin/voice-glm.sh"], timeout=90)
    r = _run(ssh + ["cat /tmp/voice-glm-transcript.txt"], timeout=8)
    text = (r.stdout.strip() if r else "")
    if text:
        tmux_send_text(sess, text)
    else:
        log("voice: empty transcript")

def session_activity(sess):
    """Scrape the session's pane -> (label, needs_choice, rec_zone). label is
    the live action (e.g. 'Vibing 8m23s' while thinking, 'choose…' at a prompt)
    else the agent-deck status. needs_choice=True means blink for user input.
    rec_zone is the touchscreen zone index (0-2) Claude Code's ❯ cursor marks as
    the recommended pick in a numbered menu, else None. agent-deck can't track
    shell-tool activity (oc-start, plain shells), so also scrape idle shells."""
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
    if CHOICE_RE.search(footer):
        # Numbered permission menu ("Do you want to proceed? ❯ 1. Yes"). Find
        # which option the ❯ cursor highlights. Zone 2 is now "Next" (dismiss),
        # not option "3", so only options 1-2 map to blinking zones 0-1.
        rec = None
        matches = list(RECO_RE.finditer(footer))
        if matches:
            n = int(matches[-1].group(1))
            if 1 <= n <= 2:
                rec = n - 1
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
    try:
        out = subprocess.check_output(["qdbus"], env=_dbus_env(), text=True, timeout=6)
    except Exception:
        return []
    return re.findall(r"org\.kde\.konsole-\d+", out)

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

def _session_list(svc):
    return [x for x in _qdbus(svc, "/Windows/1",
                              "org.kde.konsole.Window.sessionList").split() if x.strip()]

def _run_in_session(svc, sid, cmd):
    _qdbus(svc, "/Sessions/%s" % sid, "org.kde.konsole.Session.runCommand", cmd)

def _xrun(args, timeout=5):
    try:
        return subprocess.check_output(args, env=_dbus_env(), text=True, timeout=timeout)
    except Exception:
        return ""

def _konsole_windows():
    return set(_xrun(["xdotool", "search", "--class", "konsole"]).split())

def _windows_of_pid(pid):
    """Live konsole window id(s) belonging to konsole process `pid`. Empty if the
    process is gone — so a closed window (or a reused X id) resolves to nothing."""
    if not pid:
        return []
    res = _xrun(["xdotool", "search", "--pid", str(pid)]).split()
    konsole = _konsole_windows()
    return [w for w in res if w in konsole]

def _window_visible(wid):
    return wid in _xrun(["xdotool", "search", "--onlyvisible", "--class", "konsole"]).split()

def _new_window(cmd, sid=None):
    env = _dbus_env()
    if not shutil.which("konsole"):
        subprocess.Popen(["xterm", "-e", "bash", "-lc", "%s; exec bash" % cmd], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("opened new xterm window"); return
    before = _konsole_windows()
    subprocess.Popen(["konsole", "-e", "bash", "-lc", "%s; exec bash" % cmd], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # track the konsole PROCESS pid (not the reusable X window id) so a later tap
    # can re-resolve the window — survives X id reuse, detects a real close.
    if sid and shutil.which("xdotool"):
        for _ in range(20):
            time.sleep(0.2)
            new = _konsole_windows() - before
            if new:
                wid = sorted(new)[-1]
                pid = _xrun(["xdotool", "getwindowpid", wid]).strip()
                with _lock:
                    _win_map[sid] = pid
                log("opened+tracked konsole pid %s (win %s) for session %s",
                    pid, wid, sid[:8]); return
    log("opened new konsole window")

def place_konsole(cmd, mode, sid=None):
    """cmd runs in the chosen placement. window -> fresh konsole (tracked by sid);
    tab/split -> inside the focused konsole window via D-Bus (falls back to a window)."""
    if not _dbus_env().get("DISPLAY"):
        log("no DISPLAY; cannot open terminal"); return
    if mode == "window":
        _new_window(cmd, sid=sid); return
    svc = _focused_konsole()
    if not svc:
        log("no focused konsole for %s; opening new window", mode)
        _new_window(cmd, sid=sid); return
    if mode == "tab":
        sid = _qdbus(svc, "/Windows/1", "org.kde.konsole.Window.newSession")
        if sid:
            _run_in_session(svc, sid, cmd); log("tab in %s session %s", svc, sid)
        else:
            _new_window(cmd)
        return
    # split: create a new pane (new session) and run cmd in it
    action = "split-view-left-right" if mode == "split-right" else "split-view-top-bottom"
    before = set(_session_list(svc))
    _qdbus(svc, "/konsole/MainWindow_1", "org.kde.KMainWindow.activateAction", action)
    time.sleep(0.4)
    new = list(set(_session_list(svc)) - before)
    sid = new[0] if new else _qdbus(svc, "/Windows/1", "org.kde.konsole.Window.currentSession")
    if sid:
        _run_in_session(svc, sid, cmd); log("split %s in %s session %s", mode, svc, sid)
    else:
        _new_window(cmd)

# ---- actions --------------------------------------------------------------
def _bg(fn, *a):
    threading.Thread(target=fn, args=a, daemon=True).start()

def active_session():
    with _lock:
        return next((s for s in _sessions if s.get("id") == _active_id), None)

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

def act_reply(slot):
    s = active_session()
    if not s:
        log("reply: no active session"); return
    # Zone 2 is always "Next" on the select set: dismiss the active session's
    # input gate (stop blinking, yield focus) WITHOUT sending keys to the agent.
    # Applies to numbered menus AND auto-suggest prompts — "read it, move on".
    if slot == 2 and _reply_set == 0:
        dismiss_session(s); return
    label, keys = REPLY_SETS[_reply_set][1][slot]
    if not keys:
        return                                  # blank slot
    if keys[0] == "!voice":                     # voice dictation toggle
        _bg(_voice_toggle, s); return
    if keys[0].startswith("!"):                 # other shell command
        cmd = keys[0][1:]
        _run(["bash", "-lc", cmd])
        log("zone '%s' -> %s", label, cmd); return
    log("reply '%s' -> %s", label, s.get("title")); tmux_send(s, keys)

def _attach_cmd(sid):
    # start-if-needed then attach: `start` revives a stopped/killed session (so a
    # session you stopped with dial-4 reopens cleanly); it errors harmlessly when
    # the session is already running, so we silence that and attach regardless.
    return "%s session start %s >/dev/null 2>&1; %s session attach %s" % (AD, sid, AD, sid)

def open_existing(s, mode):
    """(Re)open an existing session in the chosen placement (window/tab/split)."""
    place_konsole(_attach_cmd(s["id"]), mode, sid=s["id"])

def focus_terminal(s):
    """Raise the konsole window showing the active session for manual typing.
    Never opens a duplicate: if the session's tmux already has a client (it's
    visible somewhere), only raise the window — don't attach a new terminal."""
    if not s:
        return
    sid = s["id"]; title = s.get("title", ""); t = s.get("tmux_session")
    # Does this session already have a live terminal? (prevents mirror duplicates)
    has_client = False
    if t:
        r = _run(["tmux", "list-clients", "-t", t], timeout=4)
        has_client = bool(r and r.stdout.strip())
    # Find an existing window: deck-tracked first, then title search
    with _lock:
        pid = _win_map.get(sid)
    wins = _windows_of_pid(pid)
    if not wins and title:
        candidates = _xrun(["xdotool", "search", "--name", title]).split()
        konsole = _konsole_windows()
        wins = [w for w in candidates if w in konsole]
    if wins:
        _xrun(["wmctrl", "-i", "-a", wins[0]])
        log("focus terminal for %s", title)
    elif not has_client:
        # truly no terminal — open one (attach-only, never restart)
        place_konsole("%s session attach %s" % (AD, sid), "window", sid=sid)
        log("open terminal for %s", title)
    else:
        log("session %s already visible (has client) — not opening a duplicate", title)

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
        sid = json.loads(r.stdout).get("id")
    except Exception as e:
        log("spawn parse error: %s", e); return
    if sid:
        place_konsole(_attach_cmd(sid), mode, sid=sid)

def act_restart():
    s = active_session()
    if s:
        log("restart %s", s.get("title")); _run([AD, "session", "restart", s["id"]])

def act_stop():
    s = active_session()
    if s:
        log("stop %s", s.get("title")); _run([AD, "session", "stop", s["id"]])

def select_delta(n):
    global _active_id
    with _lock:
        if not _sessions:
            return
        ids = [s["id"] for s in _sessions]
        i = ids.index(_active_id) if _active_id in ids else 0
        _active_id = ids[(i + n) % len(ids)]

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

def _render_text(d, img, text, sub=None, text_fill=(205, 210, 220), size=20):
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
              text_fill=(205, 210, 220)):
    img = _key_img(deck, bg)
    d = ImageDraw.Draw(img)
    _render_text(d, img, text, sub=sub, text_fill=text_fill, size=size)
    if border:
        d.rectangle([1, 1, img.width - 2, img.height - 2], outline=border, width=border_w)
    return PILHelper.to_native_key_format(deck, img)

# ---- animation renderers (Ghibli accents) ---------------------------------
# Each takes the PIL draw context, a 0..1 phase, an accent color, and the dark
# base color. They mutate the image in place. Renderers are pure: same phase +
# colors -> same pixels, so _frame_cache can dedupe unchanged frames.
def _ease_sine(t):
    """0..1 -> 0..1 smooth sine easing (slow at the extremes, fast in the middle)."""
    return (math.sin(2.0 * math.pi * (t % 1.0)) + 1.0) / 2.0

def _lerp_color(a, b, t):
    """Linear RGB blend. t in 0..1. No alpha; we composite onto a known base."""
    t = max(0.0, min(1.0, t))
    return (int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))

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

def _render_session(deck, s, is_active):
    st = s.get("status", "idle")
    label, needs, _rec = _activity.get(s["id"], (st, False, None))
    title, sub = s.get("title", "?"), str(label)[:13]
    base = STATE_COLOR.get(st, (50, 50, 58))
    urg = _urgency.get(s["id"], "menu") if needs else None
    # Period scaling: each renderer takes phase in "cycles", so dividing
    # _anim_phase by the period (sec) gives one full cycle per period.
    # Periods were chosen for seizure safety (all >= 1.0s) and to layer
    # without beating (no two periods share a common multiple under 6s).
    P = _anim_phase  # seconds, monotonic
    if needs and urg in ("menu", "urgent"):
        # Numbered menu / urgent text: Golden Meadow breathing, 1.6s cycle.
        img = _key_img(deck, base)
        d = ImageDraw.Draw(img)
        _anim_pulse(d, img, P / 1.6, GHIBLI["meadow"], base, amp=0.55)
        text_fill = _lerp_color((220, 225, 235), (25, 20, 10), _ease_sine(P / 1.6) * 0.5)
    elif needs and urg == "patient":
        # Auto-suggest / patient text: Spirited Rose breathing, slower (3.0s).
        img = _key_img(deck, base)
        d = ImageDraw.Draw(img)
        _anim_pulse(d, img, P / 3.0, GHIBLI["rose"], base, amp=0.40)
        text_fill = (220, 225, 235)
    elif st == "error":
        # Calm alarm: Muted Coral pulse, 1.0s. Slower than a strobe by 3x.
        img = _key_img(deck, base)
        d = ImageDraw.Draw(img)
        _anim_pulse(d, img, P / 1.0, GHIBLI["coral"], base, amp=0.45)
        text_fill = (220, 225, 235)
    elif st in ("running", "starting") or label == "thinking":
        # Working: Enchanted Forest spinner arc rotating around the border.
        img = _key_img(deck, base)
        d = ImageDraw.Draw(img)
        _anim_spinner(d, img, P / 1.2, GHIBLI["forest"], base)
        text_fill = (220, 225, 235)
    elif st == "queued":
        # Queued: Whispering Wind shimmer, slow diagonal sweep (5.0s).
        img = _key_img(deck, base)
        d = ImageDraw.Draw(img)
        _anim_shimmer(d, img, P / 5.0, GHIBLI["wind"], base, amp=0.18)
        text_fill = (220, 225, 235)
    elif st == "idle":
        # Idle: barely-there Castle Cloud shimmer (3% wave, 8s). Never fully
        # still per user preference — feels alive without distracting.
        img = _key_img(deck, base)
        d = ImageDraw.Draw(img)
        _anim_shimmer(d, img, P / 8.0, GHIBLI["cloud"], base, amp=0.03)
        text_fill = (205, 210, 220)
    else:
        # stopped / unknown: static, no animation, cache-friendly.
        border = (180, 185, 195) if is_active else None
        return _centered(deck, base, title, sub=sub, border=border)
    _render_text(d, img, title, sub=sub, text_fill=text_fill)
    if is_active:
        _draw_selector(d, img)
    return PILHelper.to_native_key_format(deck, img)

def paint_board(deck):
    # Clear the frame cache so every key is force-pushed once (used after a
    # mode switch, menu close, or other context change where stale dedup would
    # suppress a needed redraw).
    _frame_cache.clear()
    animate_active_keys(deck)

def _overlay_title(draw, img, title, thinking=False):
    """Small session title in the bottom-left corner with a dark drop-shadow on
    all four sides so it reads against any scene background without a backing
    rectangle (classic pixel-art text technique — no alpha needed for JPEG).
    When `thinking=True` the title is shifted up one row to leave room for the
    real-time activity spinner below it (see _overlay_activity)."""
    f = ImageFont.truetype(FONT_B, 13)
    txt = title[:11]
    y = img.height - 34 if thinking else img.height - 18
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        draw.text((4 + dx, y + dy), txt, font=f, fill=(0, 0, 0))
    draw.text((4, y), txt, font=f, fill=(255, 230, 180))

def _overlay_activity(draw, img, phase):
    """Real-time CLI activity indicator below the session title — a mini
    Enchanted-Forest arc spinner (1.2s rotation, matching the per-key spinner
    cadence). Drop-shadow pixels on four sides keep it legible over any scene
    background. Drawn AFTER _overlay_title(thinking=True) so it sits beneath
    the shifted title row."""
    cx, cy = 12, img.height - 11
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

def _animate_cinema(deck):
    """Cinema mode: render the full 8-key grid as one continuous 8-bit Ghibli
    battle scene. Keys needing input break through with an accent wash + pulsing
    border. Session titles are overlaid with drop-shadows. The scene is never
    interrupted — alerts wash OVER it, not INSTEAD of it."""
    # Render the full scene canvas once, slice into 8 tiles.
    scene = ghibli.render_scene(_anim_phase)
    canvas = ghibli.scale_to_canvas(scene)
    tiles = ghibli.slice_tiles(canvas)
    with _lock:
        sess = list(_sessions[:deck.key_count()]); active = _active_id
    for i in range(deck.key_count()):
        tile = tiles[i].copy()                       # mutable per-key copy
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
            _overlay_title(d, tile, s.get("title", "?"), thinking=thinking)
            _overlay_status_dot(d, tile, st)
            if thinking:
                # Real-time CLI activity indicator under the session title —
                # mini Enchanted-Forest arc spinner (1.2s rotation).
                _overlay_activity(d, tile, _anim_phase)
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

def animate_active_keys(deck):
    """Render every key each tick. In cinema mode the full grid is one continuous
    8-bit Ghibli scene; otherwise per-key state animations (pulse/spinner/shimmer)."""
    if _cinema_mode and _ui_mode == "board":
        _animate_cinema(deck)
        return
    with _lock:
        sess = list(_sessions[:deck.key_count()]); active = _active_id
    for i in range(deck.key_count()):
        if i < len(sess):
            frame = _render_session(deck, sess[i], sess[i]["id"] == active)
        else:
            frame = _centered(deck, EMPTY_COLOR, "+", size=40, sub="new")
        if _frame_cache.get(i) != frame:
            _frame_cache[i] = frame
            deck.set_key_image(i, frame)

def paint_menu(deck, items):
    for i in range(deck.key_count()):
        if i == CANCEL_KEY:
            deck.set_key_image(i, _centered(deck, CANCEL_COLOR, "Cancel", size=18))
        elif i < len(items):
            deck.set_key_image(i, _centered(deck, MENU_COLOR, items[i][0], size=18))
        else:
            deck.set_key_image(i, _key_native_blank(deck))

def _key_native_blank(deck):
    return PILHelper.to_native_key_format(deck, _key_img(deck, (8, 9, 12)))

def render_touchscreen(deck):
    img = PILHelper.create_touchscreen_image(deck)
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
        # Board mode: Ghibli panorama banner (cinema) or flat dark (non-cinema).
        if _cinema_mode:
            banner = ghibli.render_touchscreen_banner(_anim_phase)
            img.paste(banner, (0, 0))
            d = ImageDraw.Draw(img)
            txt_c = (255, 230, 180)
        else:
            txt_c = (95, 140, 180)
        s = active_session()
        if s:
            st = s.get("status", "idle")
            if not _cinema_mode:
                d.rectangle([0, 0, 8, img.height], fill=STATE_COLOR.get(st, (40, 42, 50)))
            head = "▶ {}  ·  {}".format(s.get("title", "?"), st)
        else:
            head = "▶ Laputa Siege  ·  cinema" if _cinema_mode else "▶ no session selected"
        # Drop-shadow text when on the banner (JPEG has no alpha; 4-direction
        # shadow makes any text readable over the scene without a backing rect).
        if _cinema_mode:
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                d.text((16 + dx, 6 + dy), head, font=ImageFont.truetype(FONT_B, 24), fill=(0, 0, 0))
        d.text((16, 6), head, font=ImageFont.truetype(FONT_B, 24), fill=txt_c)
        setname, zones = REPLY_SETS[_reply_set]
        setinfo = "%s %d/%d" % (setname, _reply_set + 1, len(REPLY_SETS))
        if _cinema_mode:
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                d.text((img.width - 12 + dx, 8 + dy), setinfo,
                       font=ImageFont.truetype(FONT_R, 16), anchor="ra", fill=(0, 0, 0))
        d.text((img.width - 12, 8), setinfo,
               font=ImageFont.truetype(FONT_R, 16), anchor="ra", fill=txt_c)
        # Recommended choice: the zone Claude Code's ❯ cursor marks
        # (1/2/3 for numbered menus, "Go" for auto-suggest). decoupled from the
        # "Next" label override below so menus still light their number.
        rec_zone = None
        if _reply_set == 0 and s is not None:
            rec_zone = _activity.get(s["id"], (None, False, None))[2]
        zw = img.width / 4
        for i, (label, _) in enumerate(zones):
            # Zone 2 is always "Next" on the select set: dismiss the active
            # session's input gate (stop blinking, move on) without sending
            # keys. Works for both numbered menus and auto-suggest prompts.
            if _reply_set == 0 and i == 2:
                label = "Next"
            x0 = i * zw
            if i:
                d.line([(x0, 44), (x0, img.height)], fill=(20, 22, 28), width=2)
            # Recommended zone: concentric Spirited Rose sweep instead of a hard
            # blink. Pulses continuously in sync with the matching key's meadow
            # pulse (1.6s period), so eye + key read as one cue.
            if i == rec_zone:
                _anim_sweep_rects(d, _anim_phase / 1.6, GHIBLI["rose"],
                                  (12, 13, 18),
                                  (x0 + 2, 46, x0 + zw - 2, img.height - 1))
                lbl_fill = (25, 20, 25)             # dark text on lit sweep
            else:
                lbl_fill = txt_c
            if _cinema_mode:
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    d.text((x0 + zw / 2 + dx, 70 + dy), label,
                           font=ImageFont.truetype(FONT_B, 26), anchor="mm", fill=(0, 0, 0))
            d.text((x0 + zw / 2, 70), label, font=ImageFont.truetype(FONT_B, 26),
                   anchor="mm", fill=lbl_fill)
    native = PILHelper.to_native_touchscreen_format(deck, img)
    deck.set_touchscreen_image(native, 0, 0, img.width, img.height)

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
    global _last_input, _asleep, _manual_until
    _last_input = time.monotonic()
    _manual_until = time.monotonic() + MANUAL_GRACE  # any interaction → hold session/reply-set
    if _asleep:
        _asleep = False
        deck.set_brightness(_brightness)
        log("display wake"); repaint(deck)
        return True
    return False

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
    # board mode
    with _lock:
        sess = list(_sessions[:deck.key_count()]); active = _active_id
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

def on_dial(deck, dial, event, value):
    if _wake_and_note(deck):
        return
    global _brightness, _reply_set, _manual_until
    if event == DialEventType.TURN:
        if _ui_mode != "board":
            return
        if dial == 0:                                   # knob 1: select session
            # State-only update: the 20fps main loop renders the new selector
            # within ~50ms. Calling repaint() here would race the main render
            # thread (both pushing frames concurrently → glitch/flicker).
            select_delta(1 if value > 0 else -1)
        elif dial == 1:                                 # knob 2: cycle reply set
            _reply_set = (_reply_set + (1 if value > 0 else -1)) % len(REPLY_SETS)
            log("reply set -> %d (manual)", _reply_set)
        elif dial == 3:                                 # knob 4: brightness
            _brightness = max(10, min(100, _brightness + (5 if value > 0 else -5)))
            deck.set_brightness(_brightness)
        # dial 2 (knob 3) scroll: reserved for now
    elif event == DialEventType.PUSH and value and _ui_mode == "board":
        # Knob N push = reply slot N. Knob 3 (dial 2) is "Next" on the select
        # set (dismiss the gate); on other reply sets it sends that slot's keys.
        # ponytail: focus-terminal that lived on dial 2 was dropped so knob 3
        # always matches its "Next" label — upgrade: long-press dial 2 if needed.
        _bg(act_reply, dial)

def on_touch(deck, evt, value):
    if _wake_and_note(deck):
        return
    if _ui_mode != "board" or evt not in (TouchscreenEventType.SHORT, TouchscreenEventType.LONG):
        return
    x = (value or {}).get("x", 0)
    zone = max(0, min(3, int(x // (800 / 4))))
    _bg(act_reply, zone)

# ---- main -----------------------------------------------------------------
def main():
    global _sessions, _active_id, _activity, _anim_phase, _last_input, _asleep
    global _reply_set, _manual_until, _needed_since, _urgency
    decks = DeviceManager().enumerate()
    plus = next((d for d in decks if "+" in d.deck_type()), None)
    if not plus:
        log("no Stream Deck Plus (found %s)", [d.deck_type() for d in decks]); sys.exit(1)
    plus.open(); plus.reset(); plus.set_brightness(_brightness)
    plus.set_key_callback(on_key)
    plus.set_dial_callback(on_dial)
    plus.set_touchscreen_callback(on_touch)

    _sessions = fetch_sessions()
    _active_id = _sessions[0]["id"] if _sessions else None
    _activity = {s["id"]: session_activity(s) for s in _sessions[:plus.key_count()]}
    _last_input = time.monotonic()
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
            act = {s["id"]: session_activity(s) for s in new[:plus.key_count()]}
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
            URG_RANK = {"menu": 0, "urgent": 1, "patient": 2}
            focus_order = sorted(
                [sid for sid in act if act[sid][1]],
                key=lambda sid: URG_RANK.get(urg.get(sid), 99),
            )
            choice_id = focus_order[0] if focus_order else None
            manual = now < _manual_until
            if choice_id and _reply_set != 0 and not manual:
                _reply_set = 0                        # auto-switch to arrow-nav set
                log("reply set -> select (choice needed)")
            with _lock:
                _sessions = new; _activity = act
                if _active_id not in [s["id"] for s in new]:
                    _active_id = new[0]["id"] if new else None
                if choice_id and _ui_mode == "board" and not manual:
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
    main()
