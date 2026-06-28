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
import os, re, sys, json, time, signal, shutil, subprocess, threading, logging

from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper
from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
from PIL import ImageDraw, ImageFont

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
                ("Esc", ["Escape"])]),
    ("keys",   [("Enter", ["Enter"]), ("Space", ["Space"]),
                ("S-Tab", ["BTab"]),
                ("Voice", ["!voice"])]),
    ("type",   [("1", ["1", "Enter"]), ("2", ["2", "Enter"]),
                ("3", ["3", "Enter"]), ("Esc", ["Escape"])]),
]

STATE_COLOR = {"waiting": (235, 150, 25), "running": (38, 140, 60),
               "idle": (58, 64, 78), "starting": (40, 90, 165),
               "queued": (40, 90, 165), "error": (175, 40, 40),
               "stopped": (44, 44, 52)}
EMPTY_COLOR = (22, 24, 30)
MENU_COLOR = (32, 52, 70)
CANCEL_COLOR = (120, 35, 35)
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
_reply_set_locked = False  # user manually changed reply set; don't auto-switch
_activity = {}             # session id -> (label, needs_choice) from pane parsing
_blink = False             # animation phase for the choice-needed border
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
    data.sort(key=lambda s: (STATE_RANK.get(s.get("status", "idle"), 9),
                             s.get("created_at", "")))
    return data

def tmux_send(sess, keys):
    t = sess.get("tmux_session")
    if not t:
        log("no tmux_session for %s", sess.get("title")); return
    _run(["tmux", "send-keys", "-t", t, *keys], timeout=8)
    log("send-keys -> %s : %s", t, " ".join(keys))

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
    """Scrape the session's pane -> (label, needs_choice). label is the live
    action (e.g. 'Vibing 8m23s' while thinking, 'choose…' at a prompt) else the
    agent-deck status. agent-deck can't track shell-tool activity (oc-start,
    plain shells), so also scrape idle shells for spinner/prompt evidence."""
    st = sess.get("status", "idle")
    t = sess.get("tmux_session")
    tool = sess.get("tool", "")
    # Scrape when agent-deck reports an active state, OR for shell-tool sessions
    # where agent-deck has no visibility (status stuck at 'idle' while running).
    scrape = st in ("running", "starting", "waiting") or (
        st == "idle" and tool == "shell" and bool(t))
    if not t or not scrape:
        return (st, False)
    r = _run(["tmux", "capture-pane", "-p", "-t", t, "-S", "-20"], timeout=5)
    pane = (r.stdout if r else "") or ""
    # Spinner present = agent is actively working, regardless of what agent-deck
    # reports as the status (it often flickers between running/waiting while a
    # Bash command or sub-task runs). Never blink a spinning pane.
    m = SPIN_RE.search(pane)
    if m:
        el = ELAPSED_RE.search(pane)
        return ("%s %s" % (m.group(1), el.group(1)) if el else m.group(1), False)
    if st == "waiting":
        # No spinner + agent-deck says waiting = genuinely waiting for user
        # input (numbered menu or text prompt). CHOICE_RE refines the label.
        return ("choose…" if CHOICE_RE.search(pane) else "input…", True)
    # Shell-tool idle without a spinner: detect when opencode is ASKING a
    # question (not merely done with its turn). Heuristics, in order:
    #   - ◎ ... for <elapsed>  = just-finished completion marker → idle, no blink
    #   - otherwise, a '?' in the pane = agent asked a question → blink
    # The [oc] footer alone is NOT enough — it's present in every oc state.
    if st == "idle" and tool == "shell" and re.search(r"\[oc\]\s+\d+:", pane):
        if re.search(r"◎\s+\S.*\bfor\b\s+\d+[ms]", pane):
            return ("idle", False)            # turn done, awaiting next instruction
        if "?" in pane:
            return ("input…", True)           # agent asked a question → blink
        return ("idle", False)                # plain oc prompt, no question
    # Shell-tool idle that doesn't match a spinner is genuinely idle (prompt
    # visible). Other active states without a spinner show as 'thinking'.
    return ("thinking" if st in ("running", "starting") else st, False)

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

def act_reply(slot):
    s = active_session()
    if not s:
        log("reply: no active session"); return
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

def _centered(deck, bg, text, size=20, sub=None, border=None, border_w=4,
              text_fill=(255, 255, 255)):
    img = _key_img(deck, bg)
    d = ImageDraw.Draw(img)
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
    # Center the title + sub block vertically as a group (not pinned to bottom).
    lh = size + 2
    gap = 3
    sub_h = (sub_size + gap) if sub else 0
    block_h = len(lines) * lh + sub_h
    y = (img.height - block_h) / 2
    for ln in lines:
        d.text((img.width / 2, y), ln, font=f, anchor="ma", fill=text_fill); y += lh
    if sub:
        d.text((img.width / 2, y + gap), sub, font=sf, anchor="ma", fill=text_fill)
    if border:
        d.rectangle([1, 1, img.width - 2, img.height - 2], outline=border, width=border_w)
    return PILHelper.to_native_key_format(deck, img)

def _render_session(deck, s, is_active):
    st = s.get("status", "idle")
    label, needs = _activity.get(s["id"], (st, False))
    title, sub = s.get("title", "?"), str(label)[:13]
    if needs:                                   # choice needed -> blink BACKGROUND only
        # ponytail: no thick border on blink — it hid the active-session cursor
        # when knob 1 moved the selector. Background flash (amber↔dark) is enough.
        if _blink:
            bg, fill = (255, 200, 60), (15, 15, 15)
        else:
            bg, fill = (70, 45, 0), (255, 255, 255)
        border = (255, 255, 255) if is_active else None   # keep cursor visible
        return _centered(deck, bg, title, sub=sub, text_fill=fill, border=border)
    border = (255, 255, 255) if is_active else None
    return _centered(deck, STATE_COLOR.get(st, (50, 50, 58)), title, sub=sub, border=border)

def paint_board(deck):
    with _lock:
        sess = list(_sessions[:deck.key_count()]); active = _active_id
    for i in range(deck.key_count()):
        if i < len(sess):
            deck.set_key_image(i, _render_session(deck, sess[i], sess[i]["id"] == active))
        else:
            deck.set_key_image(i, _centered(deck, EMPTY_COLOR, "+", size=40, sub="new"))

def blink_animating(deck):
    """Redraw only the keys whose session needs a choice, so their border blinks."""
    with _lock:
        sess = list(_sessions[:deck.key_count()]); active = _active_id
    for i, s in enumerate(sess):
        if _activity.get(s["id"], (None, False))[1]:
            deck.set_key_image(i, _render_session(deck, s, s["id"] == active))

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
    d.rectangle([0, 0, img.width, img.height], fill=(12, 14, 22))
    if _ui_mode == "tool":
        d.text((16, 30), "pick agent for new session  ·  Cancel = key 8",
               font=ImageFont.truetype(FONT_B, 24), fill=(150, 210, 255))
    elif _ui_mode == "place":
        _what = (_pending_tool[0] if _pending_tool
                 else _pending_session.get("title", "session") if _pending_session else "")
        d.text((16, 30), "placement for '%s'  ·  Cancel = key 8" % _what,
               font=ImageFont.truetype(FONT_B, 24), fill=(150, 210, 255))
    else:
        s = active_session()
        if s:
            st = s.get("status", "idle")
            d.rectangle([0, 0, 8, img.height], fill=STATE_COLOR.get(st, (60, 60, 60)))
            head = "▶ {}  ·  {}".format(s.get("title", "?"), st)
        else:
            head = "▶ no session selected"
        d.text((16, 6), head, font=ImageFont.truetype(FONT_B, 24), fill=(150, 210, 255))
        setname, zones = REPLY_SETS[_reply_set]
        d.text((img.width - 12, 8), "%s %d/%d" % (setname, _reply_set + 1, len(REPLY_SETS)),
               font=ImageFont.truetype(FONT_R, 16), anchor="ra", fill=(120, 130, 150))
        zw = img.width / 4
        for i, (label, _) in enumerate(zones):
            x0 = i * zw
            if i:
                d.line([(x0, 44), (x0, img.height)], fill=(40, 44, 56), width=2)
            d.text((x0 + zw / 2, 70), label, font=ImageFont.truetype(FONT_B, 26),
                   anchor="mm", fill="white")
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
    global _last_input, _asleep
    _last_input = time.monotonic()
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
            with _lock:
                _active_id = s["id"]
            repaint(deck)
    else:
        open_menu("tool"); repaint(deck)

def on_dial(deck, dial, event, value):
    if _wake_and_note(deck):
        return
    global _brightness, _reply_set, _reply_set_locked
    if event == DialEventType.TURN:
        if _ui_mode != "board":
            return
        if dial == 0:                                   # knob 1: select session
            select_delta(1 if value > 0 else -1); repaint(deck)
        elif dial == 1:                                 # knob 2: cycle reply set
            _reply_set = (_reply_set + (1 if value > 0 else -1)) % len(REPLY_SETS)
            _reply_set_locked = True   # respect manual choice until it resolves
            log("reply set -> %d (locked)", _reply_set); repaint(deck)
        elif dial == 3:                                 # knob 4: brightness
            _brightness = max(10, min(100, _brightness + (5 if value > 0 else -5)))
            deck.set_brightness(_brightness)
        # dial 2 (knob 3) scroll: reserved for now
    elif event == DialEventType.PUSH and value and _ui_mode == "board":
        if dial == 2:                                   # knob 3 push: focus terminal
            s = active_session()
            if s:
                _bg(focus_terminal, s)
        else:
            _bg(act_reply, dial)    # push knob N -> send reply slot N to active session

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
    global _sessions, _active_id, _activity, _blink, _last_input, _asleep, _reply_set, _reply_set_locked
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

    # tick fast for the choice-needed border animation; do the expensive
    # fetch + pane scrape only every Nth tick.
    ANIM = 0.45
    per_refresh = max(1, round(REFRESH_SECS / ANIM))
    tick = 0
    while not stop.wait(ANIM):
        # idle sleep: blank the OLEDs after SLEEP_SECS with no input; a callback
        # (key/dial/touch) wakes it back up via _wake_and_note().
        if not _asleep and (time.monotonic() - _last_input) > SLEEP_SECS:
            _asleep = True; plus.set_brightness(0)
            log("display asleep (idle %d min)", SLEEP_SECS // 60)
        if _asleep:
            continue
        tick += 1
        _blink = not _blink
        do_refresh = (tick % per_refresh == 0)
        if do_refresh:
            new = fetch_sessions()
            act = {s["id"]: session_activity(s) for s in new[:plus.key_count()]}
            maybe_remediate(new)                    # auto-restart errored sessions
            # Auto-select the first flashing (choice-needed) session so the
            # touchscreen replies land on it without manually hunting with knob 1.
            # Skip if the current session also needs a choice, or a menu is open.
            choice_id = next((sid for sid, (_, need) in act.items() if need), None)
            if not choice_id:
                _reply_set_locked = False            # choices cleared → release lock
            elif _reply_set != 0 and not _reply_set_locked:
                _reply_set = 0                        # auto-switch to arrow-nav set
                log("reply set -> select (choice needed)")
            with _lock:
                _sessions = new; _activity = act
                if _active_id not in [s["id"] for s in new]:
                    _active_id = new[0]["id"] if new else None
                if (choice_id and _ui_mode == "board"
                        and not act.get(_active_id, (None, False))[1]
                        and _active_id != choice_id):
                    _active_id = choice_id
                    log("auto-select flashing session %s", choice_id[:8])
            if _ui_mode != "board" and time.monotonic() > _menu_deadline:
                close_menu()
        try:
            if _ui_mode != "board":
                if do_refresh:
                    repaint(plus)
            elif do_refresh:
                repaint(plus)               # full board (current blink phase)
            else:
                blink_animating(plus)       # only choice-needed keys -> blinking border
        except Exception as e:
            log("repaint error: %s", e)
    try:
        plus.reset(); plus.close()
    except Exception:
        pass

if __name__ == "__main__":
    main()
