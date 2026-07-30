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
        """SSH host for agent sessions. None means local-only.

        Coerced to str-or-None so a non-string truthy value (e.g. a stray
        integer in config) can't crash shlex.quote in remote_command() or the
        `["ssh", ..., cfg.ssh_host]` arg list at runtime. SECURITY/error-
        boundary exempt from minimization."""
        val = self._get("deck.ssh_host", None)
        if val is None:
            return None
        if not isinstance(val, str):
            logging.warning("config: deck.ssh_host %r is not a string; coercing", val)
            return str(val)
        return val

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
        if not isinstance(raw, list):
            logging.warning("config: deck.tools %r is not a list; using default", raw)
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
        """List of (label, mode) tuples for placement options.

        Mirrors tools: each entry must be a 2-element list/tuple so the
        module-scope PLACEMENTS comprehension's tuple-unpack can't crash on
        a malformed [deck.placements] entry. Malformed entries are
        skipped+logged; if none survive, the default is returned."""
        default = [
            ["Window",  "window"],
            ["Tab",     "tab"],
            ["Split →", "split-right"],
            ["Split ↓", "split-down"],
        ]
        raw = self._get("deck.placements", None)
        if raw is None:
            return default
        if not isinstance(raw, list):
            logging.warning("config: deck.placements %r is not a list; using default", raw)
            return default
        valid = []
        for entry in raw:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                valid.append([entry[0], entry[1]])
            else:
                logging.warning(
                    "config: skipping malformed deck.placements entry %r "
                    "(need [label, mode])", entry)
        return valid if valid else default

    @property
    def reply_sets(self):
        """List of reply set dicts: [{name, zones: [[label, keys]]}].

        Two-level validation: (1) outer entry must be a dict with a 'name' and
        a non-empty list 'zones'; (2) EACH inner zone must be a 2-element
        [str, list] pair so the module-scope REPLY_SETS comprehension's
        `for label, keys in zone` unpack can't crash on a malformed zone.
        Malformed zones are filtered (logged); if a reply_set's zones become
        empty after filtering, the whole reply_set is skipped; if none
        survive, the default is returned."""
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
        if not isinstance(raw, list):
            logging.warning("config: deck.reply_sets %r is not a list; using default", raw)
            return default
        valid = []
        for rs in raw:
            if not (isinstance(rs, dict) and "name" in rs
                    and isinstance(rs.get("zones"), list) and rs["zones"]):
                logging.warning(
                    "config: skipping malformed deck.reply_sets entry %r "
                    "(need {name, zones: [...non-empty...]})", rs)
                continue
            # INNER validation: each zone must be a 2-element [str, list] pair
            # so `for label, keys in zone` can't crash downstream.
            good_zones = []
            for zone in rs["zones"]:
                if (isinstance(zone, (list, tuple)) and len(zone) == 2
                        and isinstance(zone[0], str)
                        and isinstance(zone[1], list)):
                    good_zones.append([zone[0], zone[1]])
                else:
                    logging.warning(
                        "config: skipping malformed zone %r in reply_set %r "
                        "(need [label, [keys...]])", zone, rs.get("name"))
            if not good_zones:
                logging.warning(
                    "config: reply_set %r had no valid zones; skipping",
                    rs.get("name"))
                continue
            valid.append({"name": rs["name"], "zones": good_zones})
        return valid if valid else default

    # --- Pruning section ---
    @property
    def win_miss_threshold(self):
        """Number of missed checks before declaring a konsole window dead.

        Coerced to int so a non-numeric config value (e.g. "3" or true) can't
        crash the `misses < _WIN_MISS_THRESHOLD` comparison at module-scope
        import (deck.py reads this into a global). Safe fallback to default 2."""
        raw = self._get("pruning.win_miss_threshold", 2)
        try:
            return int(raw)
        except (TypeError, ValueError):
            logging.warning(
                "config: pruning.win_miss_threshold %r is not an int; using 2", raw)
            return 2

    # --- Weather section ---
    @property
    def weather_enabled(self):
        # Truthiness-only consumer (_weather_loop) — any value is safe.
        return self._get("weather.enabled", True)

    @property
    def weather_lat(self):
        # Used only in f-strings inside _weather_poll's try/except — a bad
        # value just fails the NWS fetch (logged), never crashes the loop.
        return self._get("weather.lat", 0.0)

    @property
    def weather_lon(self):
        # See weather_lat — same inherent safety (f-string inside try).
        return self._get("weather.lon", 0.0)

    @property
    def weather_refresh_sec(self):
        # Consumer (_weather_loop) wraps float(self) in try/except — safe.
        return self._get("weather.refresh_sec", 900)

    # --- Animations section ---
    @property
    def lcd_mode(self):
        # Equality-only consumer (== "beach"/"laputa"/"normal"); a non-string
        # simply yields False on every comparison and falls through to the
        # default branch. Never crashes.
        return self._get("animations.lcd_mode", "beach")

    # --- Paths section ---
    @property
    def agent_deck_bin(self):
        """Path to agent-deck binary. Coerced to str so it can't crash
        subprocess.Popen arg-list assembly (deck.py module-scope `AD = ...`)."""
        val = self._get("paths.agent_deck_bin",
                        os.path.expanduser("~/.local/bin/agent-deck"))
        if not isinstance(val, str):
            logging.warning("config: paths.agent_deck_bin %r is not a string; coercing", val)
            return str(val)
        return val

    @property
    def cache_dir(self):
        """Cache directory. Coerced to str because deck.py passes this straight
        to os.path.join() at module scope (lines 41-43) — a non-string value
        would raise TypeError and BRICK IMPORT before the service starts."""
        val = self._get("paths.cache_dir",
                        os.path.expanduser("~/.cache/agentdeck"))
        if not isinstance(val, str):
            logging.warning("config: paths.cache_dir %r is not a string; coercing", val)
            return str(val)
        return val

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
