"""NoneBackend — signal 1 only (tmux gone). No Konsole, no X11, no D-Bus.

Useful for headless servers or rigs without a desktop session manager.
"""
from .base import TerminalBackend

class NoneBackend(TerminalBackend):
    """Minimal backend — only checks tmux session existence."""

    def is_window_alive(self, pid: int) -> bool | None:
        return None

    def is_dbus_session_alive(self, sid: str) -> bool | None:
        return None

    def get_live_dbus_kids(self, svc: str) -> set | None:
        return None
