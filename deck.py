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
  DIALS  D0 turn select | push attach · D1 push +new · D2 push restart
         D3 turn brightness | push stop

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

# All agent commands run on BatKave over `ssh -t` (PTY): the agent-deck session
# lives on k11 (so it's on the board and you reply via tmux send-keys), but the
# agent process runs on BatKave where every tool's stack is installed. A login
# shell (`bash -lc`) puts ~/bin and ~/.local/bin on the remote PATH.
# ponytail: hardcoded tool list — upgrade: read from config.toml when it grows.
SSH_HOST = "batkave"

def _remote(tool):
    return "ssh -t %s bash -lc %s" % (SSH_HOST, tool)

TOOLS = [("claude",     _remote("claude")),
         ("claude-glm", _remote("claude-glm")),
         ("claude-gpt", _remote("claude-gpt")),
         ("oc-start",   _remote("oc-start"))]

# (label, mode). Tab/split act INSIDE the focused konsole window via D-Bus;
# "window" opens a fresh konsole. Konsole places the new split pane on the right
# (split-view-left-right) or bottom (split-view-top-bottom) — top/left placement
# isn't exposed by Konsole's API, so we offer right (→) and down (↓).
PLACEMENTS = [("Window", "window"), ("Tab", "tab"),
              ("Split →", "split-right"), ("Split ↓", "split-down")]
CANCEL_KEY = 7             # last key cancels any open menu

REPLY_ZONES = [("1", ["1", "Enter"]), ("2", ["2", "Enter"]),
               ("3", ["3", "Enter"]), ("Esc", ["Escape"])]

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

def act_reply(zone):
    s = active_session()
    if not s:
        log("reply: no active session"); return
    label, keys = REPLY_ZONES[zone]
    log("reply '%s' -> %s", label, s.get("title")); tmux_send(s, keys)

def _attach_cmd(sid):
    # start-if-needed then attach: `start` revives a stopped/killed session (so a
    # session you stopped with dial-4 reopens cleanly); it errors harmlessly when
    # the session is already running, so we silence that and attach regardless.
    return "%s session start %s >/dev/null 2>&1; %s session attach %s" % (AD, sid, AD, sid)

def open_existing(s, mode):
    """(Re)open an existing session in the chosen placement (window/tab/split)."""
    place_konsole(_attach_cmd(s["id"]), mode, sid=s["id"])

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

def spawn(tool, mode):
    label, cmd = tool
    log("spawn '%s' (%s) as %s in %s", label, cmd, mode, NEW_SESSION_DIR)
    # -t names the session after the tool; -title-lock keeps Claude's session-name
    # sync from overriding it back to the folder name (e.g. "pooknast").
    r = _run([AD, "launch", NEW_SESSION_DIR, "-cmd", cmd,
              "-t", label, "-title-lock", "--json"], timeout=40)
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

def _centered(deck, bg, text, size=20, sub=None, border=None):
    img = _key_img(deck, bg)
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT_B, size)
    lines = _multiline(d, text, f, img.width - 8)
    y = (img.height - len(lines) * (size + 2)) / 2 - (8 if sub else 0)
    for ln in lines:
        d.text((img.width / 2, y), ln, font=f, anchor="ma", fill="white"); y += size + 2
    if sub:
        d.text((img.width / 2, img.height - 18), sub, font=ImageFont.truetype(FONT_R, 15),
               anchor="ma", fill=(235, 235, 235))
    if border:
        d.rectangle([1, 1, img.width - 2, img.height - 2], outline=border, width=4)
    return PILHelper.to_native_key_format(deck, img)

def paint_board(deck):
    with _lock:
        sess = list(_sessions[:deck.key_count()]); active = _active_id
    for i in range(deck.key_count()):
        if i < len(sess):
            s = sess[i]; st = s.get("status", "idle")
            deck.set_key_image(i, _centered(
                deck, STATE_COLOR.get(st, (50, 50, 58)), s.get("title", "?"),
                sub=st, border=(255, 255, 255) if s["id"] == active else None))
        else:
            deck.set_key_image(i, _centered(deck, EMPTY_COLOR, "+", size=40, sub="new"))

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
        zw = img.width / len(REPLY_ZONES)
        for i, (label, _) in enumerate(REPLY_ZONES):
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
def on_key(deck, key, pressed):
    if not pressed:
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
    global _brightness
    if event == DialEventType.TURN:
        if dial == 0 and _ui_mode == "board":
            select_delta(1 if value > 0 else -1); repaint(deck)
        elif dial == 3:
            _brightness = max(10, min(100, _brightness + (5 if value > 0 else -5)))
            deck.set_brightness(_brightness)
    elif event == DialEventType.PUSH and value and _ui_mode == "board":
        {0: lambda: _bg(toggle_or_place, deck, active_session()),
         1: lambda: (open_menu("tool"), repaint(deck)),
         2: act_restart, 3: act_stop}.get(dial, lambda: None)()
        if dial in (2, 3):
            _bg(repaint, deck)

def on_touch(deck, evt, value):
    if _ui_mode != "board" or evt not in (TouchscreenEventType.SHORT, TouchscreenEventType.LONG):
        return
    x = (value or {}).get("x", 0)
    zone = max(0, min(len(REPLY_ZONES) - 1, int(x // (800 / len(REPLY_ZONES)))))
    _bg(act_reply, zone)

# ---- main -----------------------------------------------------------------
def main():
    global _sessions, _active_id
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
    repaint(plus)
    log("session board ready: %d sessions, tools=%s", len(_sessions),
        [t[0] for t in TOOLS])

    stop = threading.Event()
    def shutdown(*_):
        log("shutting down"); stop.set()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while not stop.wait(REFRESH_SECS):
        new = fetch_sessions()
        with _lock:
            _sessions = new
            if _active_id not in [s["id"] for s in new]:
                _active_id = new[0]["id"] if new else None
        if _ui_mode != "board" and time.monotonic() > _menu_deadline:
            close_menu()
        try:
            repaint(plus)
        except Exception as e:
            log("repaint error: %s", e)
    try:
        plus.reset(); plus.close()
    except Exception:
        pass

if __name__ == "__main__":
    main()
