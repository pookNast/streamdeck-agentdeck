#!/usr/bin/env python3
"""Clean up stale entries in agent-deck cache files.

Determines which agent-deck sessions are still alive by cross-referencing
live tmux session names, then removes stale entries from pane_order.json,
windows.json, and dbus_sessions.json.
"""
import json, os, subprocess

CACHE = os.path.expanduser("~/.cache/agentdeck")

def live_tmux_names():
    r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                       capture_output=True, text=True)
    if r.returncode:
        return set()
    return set(r.stdout.strip().splitlines())

# Get agent-deck session list if available
ad = os.path.expanduser("~/.local/bin/agent-deck")
def agentdeck_sessions():
    if not os.path.exists(ad):
        return None  # agent-deck not available — use tmux heuristics
    r = subprocess.run([ad, "list", "--json"], capture_output=True, text=True, timeout=10)
    if r.returncode:
        return None
    try:
        data = json.loads(r.stdout)
        if isinstance(data, list):
            return {s["id"] for s in data}
        return {s["id"] for s in data.get("items", data.get("sessions", []))}
    except Exception:
        return None

# Get live agent-deck session IDs
live_ids = agentdeck_sessions()

if live_ids is None:
    # Fallback: derive from tmux session names
    # tmux names follow pattern: agentdeck_<tool>_<rand>
    # agent-deck IDs contain the rand suffix
    tmux_names = live_tmux_names()
    rands = set()
    for name in tmux_names:
        if name.startswith("agentdeck_"):
            # extract rand: last segment after last _
            parts = name.rsplit("_", 1)
            if len(parts) == 2:
                rands.add(parts[1])

    # Read current pane_order to find all known session IDs
    pane_order = {}
    try:
        with open(os.path.join(CACHE, "pane_order.json")) as f:
            pane_order = json.load(f)
    except Exception:
        pass

    all_sids = set()
    for sids in pane_order.values():
        all_sids.update(sids)

    # A session is alive if its rand suffix matches a live tmux rand
    # (e.g., 53034a28-1783866021 matches if "53034a28" or "1783866021" appears in rands)
    live_ids = set()
    for sid in all_sids:
        for rand in rands:
            if rand in sid:
                live_ids.add(sid)
                break

    print(f"Fallback: derived {len(live_ids)} live sessions from {len(tmux_names)} tmux sessions")

# Load cache files
win_map = {}
try:
    with open(os.path.join(CACHE, "windows.json")) as f:
        win_map = json.load(f)
except Exception:
    pass

dbus_map = {}
try:
    with open(os.path.join(CACHE, "dbus_sessions.json")) as f:
        dbus_map = json.load(f)
except Exception:
    pass

pane_order = {}
try:
    with open(os.path.join(CACHE, "pane_order.json")) as f:
        pane_order = json.load(f)
except Exception:
    pass

# Count before
n_win_before = len(win_map)
n_pane_before = sum(len(v) for v in pane_order.values())
n_dbus_before = len(dbus_map)

# Clean windows.json
stale_win = {sid: pid for sid, pid in win_map.items() if sid not in live_ids}
for sid in stale_win:
    del win_map[sid]
print(f"windows.json: removed {len(stale_win)} stale entries ({n_win_before} -> {len(win_map)})")

# Clean pane_order.json
stale_pane_count = 0
for pid_key in list(pane_order):
    pane_order[pid_key] = [s for s in pane_order[pid_key] if s in live_ids]
    if not pane_order[pid_key]:
        del pane_order[pid_key]

# Clean dbus_map
stale_dbus = {sid: v for sid, v in dbus_map.items() if sid not in live_ids}
for sid in stale_dbus:
    del dbus_map[sid]
print(f"dbus_sessions.json: removed {len(stale_dbus)} stale entries ({n_dbus_before} -> {len(dbus_map)})")

# Save
try:
    with open(os.path.join(CACHE, "windows.json"), "w") as f:
        json.dump(win_map, f)
    with open(os.path.join(CACHE, "pane_order.json"), "w") as f:
        json.dump(pane_order, f)
    with open(os.path.join(CACHE, "dbus_sessions.json"), "w") as f:
        json.dump(dbus_map, f)
    print("Cache files cleaned up successfully")
except Exception as e:
    print(f"Error saving: {e}")
