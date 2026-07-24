"""Configuration loading for streamdeck-agentdeck.

Reads ~/.config/streamdeck-agentdeck/config.toml or ~/.streamdeck-agentdeck.toml.
Missing keys fall back to built-in defaults — the file is optional.
"""
import os

def _load_toml(path):
    """Load a TOML file, returning dict. Falls back to built-in tomllib (3.11+)."""
    try:
        from tomllib import load  # Python 3.11+
    except ImportError:
        try:
            import toml as tomllib
            load = tomllib.load  # type: ignore
        except ImportError:
            # Last resort: try toml package
            import toml  # type: ignore
            with open(path) as f:
                return toml.load(f)
    with open(path, "rb") as f:
        return load(f)

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
        """List of (label, command) tuples for tool spawn."""
        return self._get("deck.tools", [
            ["claude", "claude"],
            ["glm",    "claude-glm"],
            ["gpt",    "claude-gpt"],
            ["local",  "oc-start"],
        ])

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
        """List of reply set dicts: [{name, zones: [[label, keys]]}]."""
        return self._get("deck.reply_sets", [
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
        ])

    # --- Terminal section ---
    @property
    def terminal_mode(self):
        """Terminal integration mode: 'konsole', 'tmux', or 'none'."""
        return self._get("terminal.mode", "konsole")

    # --- Pruning section ---
    @property
    def win_miss_threshold(self):
        return self._get("pruning.win_miss_threshold", 2)

    @property
    def idle_timeout_sec(self):
        return self._get("pruning.idle_timeout_sec", 0)

    @property
    def prune_cooldown_sec(self):
        return self._get("pruning.prune_cooldown_sec", 60)

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
            return "ssh -t %s bash -lc %s" % (self.ssh_host, tool_cmd)
        return tool_cmd
