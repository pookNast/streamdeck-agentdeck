"""Tmux-only terminal backend — no X11/D-Bus dependency.

Supports signals 1-2 (tmux gone, SSH died). No Konsole D-Bus integration.
This is the portable default for non-KDE rigs.
"""
from .base import TerminalBackend

class TmuxOnly(TerminalBackend):
    """Minimal backend that only checks tmux session existence.

    Signal 1: tmux session gone
    Signal 2: SSH tunnel died (pane fell back from ssh)
    """

    def is_window_alive(self, pid: int) -> bool | None:
        # Without Konsole, we don't track windows.
        # The caller should not call this; return None to signal "unknown".
        return None

    def is_dbus_session_alive(self, sid: str) -> bool | None:
        return None

    def get_live_dbus_kids(self, svc: str) -> set | None:
        return None
