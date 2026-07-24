"""Terminal backend package — abstracts Konsole D-Bus so it's optional."""

def create_backend(mode, **kwargs):
    """Return a TerminalBackend instance for the given mode.

    mode: 'konsole', 'tmux', or 'none'
    kwargs: forwarded to backend constructors (e.g. win_map, save_win_map_fn)
    """
    if mode == "konsole":
        from .konsole_dbus import KonsoleDBus
        return KonsoleDBus(**kwargs)
    if mode == "tmux":
        from .tmux_only import TmuxOnly
        return TmuxOnly()
    from .none import NoneBackend
    return NoneBackend()
