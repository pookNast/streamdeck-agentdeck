"""Konsole D-Bus terminal backend — full KDE integration (signals 1-4).

Requires: qdbus, xdotool, Konsole running on X11.
"""
import os, re, subprocess, shutil, time, signal

from .base import TerminalBackend

_DBUS_MAP_PATH = os.path.expanduser("~/.cache/agentdeck/dbus_sessions.json")

def _dbus_env():
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    return env

def _qdbus(*args, timeout=6):
    try:
        return subprocess.check_output(
            ["qdbus", *args], env=_dbus_env(), text=True, timeout=timeout
        ).strip()
    except Exception as e:
        print("qdbus %s failed: %s" % (" ".join(args), e))
        return ""

def _xrun_checked(args, timeout=5):
    try:
        r = subprocess.run(args, env=_dbus_env(), capture_output=True, text=True, timeout=timeout)
        return (True, r.stdout) if r.returncode == 0 else (False, "")
    except Exception:
        return (False, "")

class KonsoleDBus(TerminalBackend):
    """Full Konsole D-Bus backend supporting all four pruning signals."""

    def __init__(self, win_map, save_win_map_fn, save_dbus_map_fn):
        """win_map: dict sid->pid, save_*_fn: persistence callbacks.
        The caller (deck.py) owns the lock and persistence; the backend
        delegates through callbacks so it doesn't need import access to globals."""
        self._win_map = win_map
        self._save_win_map = save_win_map_fn
        self._save_dbus_map = save_dbus_map_fn

    # --- TerminalBackend protocol ---

    def is_window_alive(self, pid: int) -> bool | None:
        """Signal 3: check whether a konsole PID still has live X windows."""
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            return False
        ok1, res = _xrun_checked(["xdotool", "search", "--pid", str(pid)])
        ok2, kons = _xrun_checked(["xdotool", "search", "--class", "konsole"])
        if not (ok1 and ok2):
            return None  # query failed — unknown
        konsole = set(kons.split())
        return len([w for w in res.split() if w in konsole]) > 0

    def is_dbus_session_alive(self, sid: str) -> bool | None:
        """Signal 4: check whether a D-Bus ksid still exists in sessionList."""
        from_deck = self._win_map.get(sid)
        if not from_deck:
            return None
        return from_deck is not None  # tracked ⇒ alive (actual check in _prune_dead)

    def get_live_dbus_kids(self, svc: str) -> tuple:
        """Return (ok, set-of-ksids) for a konsole D-Bus service."""
        ok, out = _xrun_checked(
            ["qdbus", svc, "/Windows/1", "org.kde.konsole.Window.sessionList"])
        return (ok, {p.strip() for p in out.split() if p.strip()}) if ok else (False, set())

    def record_session_pane(self, pid: int, sid: str) -> None:
        self._win_map[sid] = pid
        self._save_win_map()

    def forget_session_pane(self, sid: str) -> None:
        self._win_map.pop(sid, None)
        self._save_win_map()

    # --- High-level placement operations ---

    def _konsole_services(self):
        return re.findall(r"org\.kde\.konsole-\d+", _qdbus())

    def _focused_konsole(self):
        if not (shutil.which("qdbus") and shutil.which("xdotool")):
            return None
        try:
            pid = subprocess.check_output(
                ["xdotool", "getactivewindow", "getwindowpid"],
                env=_dbus_env(), text=True, timeout=5
            ).strip()
            svc = "org.kde.konsole-%s" % pid
            if svc in self._konsole_services():
                return svc
        except Exception:
            pass
        return None

    def _agentdeck_konsole(self):
        pids = list(self._win_map.values())
        svcs = set(self._konsole_services())
        for pid in pids:
            svc = "org.kde.konsole-%s" % pid
            if svc in svcs:
                return svc
        return None

    def _any_konsole(self):
        svcs = self._konsole_services()
        if not svcs:
            return None
        if shutil.which("xdotool"):
            for w in _xrun_checked(["xdotool", "search", "--onlyvisible",
                                    "--class", "konsole"])[1].split():
                pid = subprocess.check_output(
                    ["xdotool", "getwindowpid", w], env=_dbus_env(),
                    text=True, timeout=5).strip()
                svc = "org.kde.konsole-%s" % pid
                if svc in svcs:
                    return svc
        return svcs[0]

    def _session_list(self, svc):
        return [x for x in _qdbus(svc, "/Windows/1",
                                  "org.kde.konsole.Window.sessionList").split()
                if x.strip()]

    def _run_in_session(self, svc, sid, cmd):
        _qdbus(svc, "/Sessions/%s" % sid,
               "org.kde.konsole.Session.runCommand", cmd)

    def _windows_of_pid(self, pid):
        if not pid:
            return []
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            return []
        ok1, res = _xrun_checked(["xdotool", "search", "--pid", str(pid)])
        ok2, kons = _xrun_checked(["xdotool", "search", "--class", "konsole"])
        if not (ok1 and ok2):
            return None
        konsole = set(kons.split())
        return [w for w in res.split() if w in konsole]

    def _window_visible(self, wid):
        return wid in _xrun_checked(
            ["xdotool", "search", "--onlyvisible", "--class", "konsole"]
        )[1].split()

    def _focus_konsole_window(self, svc):
        try:
            pid = int(svc.rsplit("-", 1)[-1])
        except ValueError:
            return
        wins = self._windows_of_pid(pid)
        if not wins:
            return
        wid = next((w for w in wins if self._window_visible(w)), wins[0])
        _xrun_checked(["xdotool", "windowactivate", "--sync", wid], timeout=3)

    def _new_window(self, cmd, sid=None):
        env = _dbus_env()
        if not shutil.which("konsole"):
            subprocess.Popen(
                ["xterm", "-e", "bash", "-lc", "%s; exec bash" % cmd],
                env=env, stdout=subprocess.DEVNULL)
            return
        before = set(_xrun_checked(["xdotool", "search", "--class", "konsole"])[1].split())
        subprocess.Popen(
            ["konsole", "-e", "bash", "-lc", "%s; exec bash" % cmd],
            env=env, stdout=subprocess.DEVNULL)
        if sid and shutil.which("xdotool"):
            for _ in range(20):
                time.sleep(0.2)
                all_wins = set(_xrun_checked(["xdotool", "search", "--class", "konsole"])[1].split())
                new = all_wins - before
                if new:
                    wid = sorted(new)[-1]
                    pid = subprocess.check_output(
                        ["xdotool", "getwindowpid", wid],
                        env=_dbus_env(), text=True, timeout=5).strip()
                    self.record_session_pane(int(pid), sid)
                    return

    def place(self, cmd, mode, sid=None, tmux=None):
        """Place cmd in konsole using the given mode.
        Returns True on success, False on failure."""
        if not _dbus_env().get("DISPLAY"):
            return False
        if mode == "window":
            self._new_window(cmd, sid=sid)
            return True

        svc = self._agentdeck_konsole() or self._focused_konsole() or self._any_konsole()
        if not svc:
            self._new_window(cmd, sid=sid)
            return True

        if mode == "tab":
            ksid = _qdbus(svc, "/Windows/1",
                          "org.kde.konsole.Window.newSession")
            if ksid:
                self._run_in_session(svc, ksid, cmd)
                if sid:
                    pid = int(svc.rsplit("-", 1)[-1])
                    self.record_session_pane(pid, sid)
                return True
            self._new_window(cmd, sid=sid)
            return True

        # split
        self._focus_konsole_window(svc)
        action = ("split-view-left-right" if mode == "split-right"
                  else "split-view-top-bottom")
        _qdbus(svc, "/konsole/MainWindow_1",
               "org.kde.KMainWindow.activateAction", action)
        time.sleep(0.3)

        return True
