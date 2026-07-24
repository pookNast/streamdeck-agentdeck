"""TerminalBackend protocol — all pruning/render code depends on this.

Three implementations:
  - KonsoleDBus   (signals 1-4, full KDE integration)
  - TmuxOnly      (signals 1-2, portable, no X11/D-Bus)
  - NoneBackend   (signal 1 only, no terminal integration)
"""

class TerminalBackend:
    """Protocol that all terminal backends implement.

    Methods:
      is_window_alive(pid) -> bool
          Whether the konsole/PID window still exists.
          None means "unknown / backend doesn't track windows".

      is_dbus_session_alive(sid) -> bool | None
          Whether the D-Bus session still exists (Konsole-only).
          None means "unknown / backend doesn't track sessions".

      get_live_dbus_kids(svc) -> set[str] | None
          Get the set of live D-Bus session paths for a konsole service.
          None means "backend doesn't support this".

      record_session_pane(pid, sid) -> None
          Record that session sid lives in window pid.

      forget_session_pane(sid) -> None
          Remove session sid from all tracked windows.
    """

    def is_window_alive(self, pid: int) -> bool | None:
        raise NotImplementedError

    def is_dbus_session_alive(self, sid: str) -> bool | None:
        return None

    def get_live_dbus_kids(self, svc: str) -> set | None:
        return None

    def record_session_pane(self, pid: int, sid: str) -> None:
        pass

    def forget_session_pane(self, sid: str) -> None:
        pass
