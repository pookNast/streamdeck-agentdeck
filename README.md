# streamdeck-agentdeck

Turn an **Elgato Stream Deck Plus** into a headless control surface for
[**agent-deck**](https://github.com/asheshgoplani/agent-deck) — manage your live
Claude / AI coding sessions from the deck: each session is a button, the
touchscreen is a quick-reply bar, and the dials drive selection and lifecycle.

No GUI tool (OpenDeck / StreamController) required — this is a single Python
daemon talking straight to the device via
[python-elgato-streamdeck](https://github.com/abcminiuser/python-elgato-streamdeck),
and to agent-deck via its CLI.

```
┌──────┬──────┬──────┬──────┐   8 KEYS  = live agent-deck sessions
│ sess │ sess │ sess │  +   │            (sorted waiting-first; color = state)
├──────┼──────┼──────┼──────┤            amber=waiting  green=running
│  +   │  +   │  +   │  +   │            slate=idle  red=error  "+"=spawn
└──────┴──────┴──────┴──────┘
   [ 1 ]  [ 2 ]  [ 3 ]  [Esc]   TOUCHSCREEN = quick replies to the active session
    (◑)    ( )    (↻)    (■)     DIALS
```

## Features

- **Session board** — the 8 keys mirror `agent-deck list --json`, refreshed every
  couple of seconds and **colored by state**, so a session that is *waiting* for
  your input glows amber.
- **Reply from the deck** — the touchscreen's 4 zones send real keystrokes
  (`1` / `2` / `3` / `Esc`) into the selected session's tmux pane — answer a
  Claude permission prompt without touching the keyboard.
- **Repurposed dials** — D0 turn = move selection · D0 push = attach ·
  D1 push = spawn · D2 push = restart · D3 push = stop · D3 turn = brightness.
- **Two-step spawn picker** — tap an empty `+` key (or push D1):
  1. **Tool menu** — pick which agent to launch (`TOOLS`).
  2. **Placement menu** — `Window` (new konsole) · `Tab` / `Split →` / `Split ↓`
     **inside the konsole you're focused on**, via Konsole's D-Bus API.
- **Optional SSH routing** — a tool entry can be `ssh -t <host> bash -lc <tool>`
  so the agent runs on another machine while the session stays on your board and
  stays repliable.

## Requirements

- Linux with an active local desktop session (X11 recommended for the konsole
  tab/split placement; the session board itself is display-agnostic).
- An Elgato Stream Deck **Plus** (USB `0fd9:0084`).
- [agent-deck](https://github.com/asheshgoplani/agent-deck) on `PATH`
  (`~/.local/bin/agent-deck`), plus `tmux`.
- `python3` with `streamdeck` + `pillow`, and the `libhidapi-libusb0` backend.
- For tab/split placement: KDE **Konsole** + `qdbus` + `xdotool`.

## Install

```bash
git clone <this-repo> ~/streamdeck-agentdeck-src
cd ~/streamdeck-agentdeck-src
./install.sh
```

`install.sh` is idempotent: it installs the udev rule (uaccess for vendor
`0fd9`, so no root is needed to drive the device), the Python deps, copies
`deck.py` into `~/streamdeck-agentdeck/`, and enables the systemd **user**
service (`--user`, with linger so it survives reboot).

Logs: `journalctl --user -u streamdeck-agentdeck -f`

## Configure

Everything is plain constants at the top of `deck.py` — edit and
`systemctl --user restart streamdeck-agentdeck`:

| Constant | Purpose |
|---|---|
| `TOOLS` | `(label, command)` rows for the spawn tool menu. A command may be a local binary (`claude`) or remote (`ssh -t host bash -lc claude-glm`). |
| `SSH_HOST` | Host used by the `_remote()` helper when routing tools over SSH. |
| `PLACEMENTS` | Spawn placement options (Window / Tab / Split → / Split ↓). |
| `REPLY_ZONES` | The 4 touchscreen quick-replies (label + tmux key sequence). |
| `NEW_SESSION_DIR` | Working dir registered for a newly spawned session. |
| `REFRESH_SECS` / `MENU_TIMEOUT` | Board refresh cadence / picker auto-cancel. |

### Agent flavors

`contrib/claude-glm` is an example wrapper that points Claude Code at the Z.AI
GLM backend (reads its key from `~/.config/claude-glm/secrets`, never embedded).
Drop similar wrappers on `PATH` and add them to `TOOLS`.

## How it works

- **State + list** come from `agent-deck list --json` (poll); the daemon never
  parses tmux directly for the board.
- **Replies / special keys** go through `tmux send-keys -t <tmux_session>` for
  instant, reliable input (numbers, Enter, Escape).
- **Spawning** is `agent-deck launch <dir> -cmd <tool> --json`; the returned
  session is then attached in the chosen placement.
- **Tab/split** use Konsole D-Bus on the *focused* window:
  `Window.newSession()` + `Session.runCommand()` for a tab; `activateAction`
  `split-view-left-right` / `top-bottom` + run-in-new-pane for a split. Konsole
  only ever creates the new pane on the right/bottom, hence `Split →` / `Split ↓`.

## License

MIT — see [LICENSE](LICENSE).
