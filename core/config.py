"""Configuration loading for streamdeck-agentdeck.

Reads ~/.config/streamdeck-agentdeck/config.toml or ~/.streamdeck-agentdeck.toml.
Missing keys fall back to built-in defaults — the file is optional.
"""
import os
import shlex
import logging

def _load_toml(path):
    """Load a TOML file, returning dict. Falls back to built-in tomllib (3.11+).

    A malformed config file logs a warning and returns {} so the service falls
    back to defaults instead of bricking on a TOMLDecodeError propagating through
    Config() at startup."""
    try:
        from tomllib import load  # Python 3.11+
    except ImportError:
        try:
            import toml as tomllib
            load = tomllib.load  # type: ignore
        except ImportError:
            # Last resort: try toml package
            import toml  # type: ignore
            try:
                with open(path) as f:
                    return toml.load(f)
            except Exception as e:
                import logging
                logging.warning("config %s malformed (%s); using defaults", path, e)
                return {}
    try:
        with open(path, "rb") as f:
            return load(f)
    except Exception as e:
        # tomllib.TOMLDecodeError (or toml's parse error) on a syntax error —
        # never let this escape Config() and brick the systemd service.
        import logging
        logging.warning("config %s malformed (%s); using defaults", path, e)
        return {}

def _resolve_path():
    """Find config file: ~/.config/streamdeck-agentdeck/config.toml > ~/.streamdeck-agentdeck.toml"""
    candidates = [
        os.path.expanduser("~/.config/streamdeck-agentdeck/config.toml"),
        os.path.expanduser("~/.streamdeck-agentdeck.toml"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

class Config:
    """Central configuration object. Read config.toml once at startup, then
    serve as a read-only config dict with sensible defaults for any key missing
    from the file."""

    def __init__(self, raw=None):
        if raw is None:
            raw = {}
            path = _resolve_path()
            if path:
                raw = _load_toml(path)

        self._raw = raw
        self._d = raw

    def _get(self, key_chain, default):
        """Traverse a dotted key chain like 'terminal.mode' through nested dicts."""
        parts = key_chain.split(".")
        d = self._d
        for part in parts:
            if not isinstance(d, dict):
                return default
            d = d.get(part, default)
            if d is default:
                return default
        return d

    # --- Deck section ---
    @property
    def ssh_host(self):
        """SSH host for agent sessions. None means local-only."""
        return self._get("deck.ssh_host", None)

    @property
    def tools(self):
        """List of (label, command) tuples for tool spawn.

        Validates structure: a row must be a 2-element list/tuple so the
        module-scope TOOLS comprehension's tuple-unpack can't crash on a
        malformed [deck.tools] entry. Malformed rows are skipped+logged; if
        none survive, the well-formed default is returned (F12: never brick
        import over a structural config error)."""
        default = [
            ["claude", "claude"],
            ["glm",    "claude-glm"],
            ["gpt",    "claude-gpt"],
            ["local",  "oc-start"],
        ]
        raw = self._get("deck.tools", None)
        if raw is None:
            return default
        valid = []
        for entry in raw:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                valid.append([entry[0], entry[1]])
            else:
                logging.warning(
                    "config: skipping malformed deck.tools entry %r "
                    "(need [label, command])", entry)
        return valid if valid else default

    @property
    def placements(self):
        """List of (label, mode) tuples for placement options."""
        return self._get("deck.placements", [
            ["Window",  "window"],
            ["Tab",     "tab"],
            ["Split →", "split-right"],
            ["Split ↓", "split-down"],
        ])

    @property
    def reply_sets(self):
        """List of reply set dicts: [{name, zones: [[label, keys]]}].

        Validates structure: each entry must be a dict with a 'name' and a
        non-empty list 'zones', so the module-scope REPLY_SETS comprehension's
        rs['name']/rs['zones'] access can't crash on schema drift. Malformed
        entries are skipped+logged; if none survive, the default is returned."""
        default = [
            {
                "name": "select",
                "zones": [
                    ["1",      ["Up",       "Enter"]],
                    ["2",      ["Down",     "Enter"]],
                    ["3",      ["Down", "Down", "Enter"]],
                    ["4",      ["Tab", "~0.5", "Enter"]],
                ],
            },
            {
                "name": "keys",
                "zones": [
                    ["Esc",   ["Escape"]],
                    ["Space", ["Space"]],
                    ["S-Tab", ["BTab"]],
                    ["Voice", ["!voice"]],
                ],
            },
            {
                "name": "type",
                "zones": [
                    ["1",      ["1", "Enter"]],
                    ["2",      ["2", "Enter"]],
                    ["3",      ["3", "Enter"]],
                    ["Esc",    ["Escape"]],
                ],
            },
        ]
        raw = self._get("deck.reply_sets", None)
        if raw is None:
            return default
        valid = []
        for rs in raw:
            if (isinstance(rs, dict) and "name" in rs
                    and isinstance(rs.get("zones"), list) and rs["zones"]):
                valid.append(rs)
            else:
                logging.warning(
                    "config: skipping malformed deck.reply_sets entry %r "
                    "(need {name, zones: [...non-empty...]})", rs)
        return valid if valid else default

    # --- Pruning section ---
    @property
    def win_miss_threshold(self):
        """Number of missed checks before declaring a konsole window dead."""
        return self._get("pruning.win_miss_threshold", 2)

    # --- Weather section ---
    @property
    def weather_enabled(self):
        return self._get("weather.enabled", True)

    @property
    def weather_lat(self):
        return self._get("weather.lat", 0.0)

    @property
    def weather_lon(self):
        return self._get("weather.lon", 0.0)

    @property
    def weather_refresh_sec(self):
        return self._get("weather.refresh_sec", 900)

    # --- Animations section ---
    @property
    def lcd_mode(self):
        return self._get("animations.lcd_mode", "beach")

    # --- Paths section ---
    @property
    def agent_deck_bin(self):
        return self._get("paths.agent_deck_bin", os.path.expanduser("~/.local/bin/agent-deck"))

    @property
    def cache_dir(self):
        return self._get("paths.cache_dir", os.path.expanduser("~/.cache/agentdeck"))

    def remote_command(self, tool_cmd):
        """Wrap a tool command with SSH if ssh_host is set."""
        if self.ssh_host:
            # shlex.quote both fields: tool_cmd becomes one bash arg, and the
            # trailing `_` absorbs $0 so `bash -lc CMD _ --flag` runs the whole
            # command (without it, `bash -lc claude --model X` runs only `claude`
            # with $0=--model). No current default tool has args, but quote it
            # correctly so a future flag-bearing tool runs remotely as written.
            return "ssh -t %s bash -lc %s _" % (
                shlex.quote(self.ssh_host), shlex.quote(tool_cmd))
        return tool_cmd
