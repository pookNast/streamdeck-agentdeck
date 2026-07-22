# Stream Deck XL+ Keymap — agentdeck v3

**Hardware:** Elgato Stream Deck XL+ (36 keys: 9 cols × 4 rows + LCD strip)
**Host:** the-host (homelab LAN, user `pooknast`)
**Source:** `git-host:pook/streamdeck-agentdeck` — `deck.py` is the single-file service
**Deploy:** `scp deck.py the-host:~/streamdeck-agentdeck/ && ssh the-host "systemctl --user restart streamdeck-agentdeck"`
**Last updated:** 2026-07-22 (Ghibli-style painterly beach + knob zones always visible with drop-shadows)

---

## Physical layout (9 columns × 4 rows)

```
         C0    C1    C2    C3    C4    C5    C6    C7    C8
       ┌─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┐
  R0   │ 0   │ 1   │ 2   │ 3   │ 4   │ 5   │ 6   │ 7   │ 8   │
       │ ses │ ses │ ses │ ses │ ses │ ses │ ses │ ses │ ses │
       ├─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┤
  R1   │ 9   │ 10  │ 11  │ 12  │ 13  │ 14  │ 15  │ 16  │ 17  │
       │ 1   │ 2   │ 3   │ 4   │/com │/res │/clr │/cst │/swk │
       │reply│reply│reply│reply│mit  │ume  │ear  │     │rkr  │
       ├─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┤
  R2   │ 18  │ 19  │ 20  │ 21  │ 22  │ 23  │ 24  │ 25  │ 26  │
       │Esc  │S-Tab│Voice│ Go  │ ses │ ses │ ses │ ses │ ses │
       ├─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┤
  R3   │ 27  │ 28  │ 29  │ 30  │ 31  │ 32  │ 33  │ 34  │ 35  │
       │ ses │ ses │ ses │ ses │ ses │Swap │/goal│/goal│Stat │
       │     │     │     │     │     │     │     │ cmpl│ us  │
       └─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┘
       ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ LCD strip (1200×100 px) ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
```

**Legend:** `ses` = session slot · `/com` = `/commit` · `/res` = `/resume` · `/clr` = `/clear` · `/cst` = `/cost` · `/swkr` = `/super-worker` · `/goal cmpl` = `/goal complete` · `Stat us` = Status blast

**Session slot count:** 19 (R0: 9 + R2 C4-C8: 5 + R3 C0-C4: 5)

---

## Tap actions (short press)

### Row 0 — Session slots (keys 0–8)
| Action | Behavior |
|---|---|
| First tap | Focus session (LCD shows its title + activity) |
| Second tap (same key) | Toggle its konsole window open/closed |

### Row 1 — Reply strip + slash keys (keys 9–17)
| Key | Label | Action |
|---|---|---|
| 9 | `1` | Send reply option 1 (e.g., "Yes"). Cancel menu when no live menu. |
| 10 | `2` | Send reply option 2 (e.g., "No") |
| 11 | `3` | Send reply option 3 (e.g., "Don't ask") |
| 12 | `4` | Send reply option 4 |
| 13 | `/commit` | Fire `/commit` Enter to active session |
| 14 | `/resume` | Fire `/resume` Enter |
| 15 | `/clear` | Fire `/clear` Enter |
| 16 | `/cost` | Fire `/cost` Enter |
| 17 | `/super-worker` | Fire `/super-worker` Enter |

**Reply key labels** update dynamically — when the active session has a live numbered menu, keys 9–12 show `"1 Yes"`, `"2 No"`, `"3 Don't ask"`, etc. When no menu, they show bare `"1"`, `"2"`, `"3"`, `"4"`.

### Row 2 — Quick controls + session slots (keys 18–26)
| Key | Label | Action |
|---|---|---|
| 18 | `Esc` | Send Escape to active session |
| 19 | `S-Tab` | Send Shift-Tab |
| 20 | `Voice` | Toggle voice input |
| 21 | `Go` | Send `Tab → 0.5s pause → Enter` (submit) |
| 22–26 | (session) | Session slot (focus/toggle) |

### Row 3 — Session slots + workflow ops corner (keys 27–35)
| Key | Label | Action |
|---|---|---|
| 27–31 | (session) | Session slot (focus/toggle) |
| 32 | `Swap` | Cycle active session's tool: claude→glm→gpt→local→claude (10s cooldown) |
| 33 | `/goal` | Fire `/goal` Enter — initiate goal-tracked loop |
| 34 | `/goal complete` | Fire `/goal complete` Enter — close active goal loop |
| 35 | `Status` | Open tmux split with homelab status (the-host/the-deck-host/server-host uptime+mem+disk+services) |

---

## Long-press actions (hold ~0.6s)

| Key | Short tap (preserved) | Long press (new) |
|---|---|---|
| **7** (R0 C7) | Focus/toggle session | **Cycle LCD panorama mode** (normal → Laputa → Beach → normal) |
| **17** (R1 C8) | `/super-worker` | **Cycle reply set** (select→keys→type→select) |
| **18** (R2 C0) | Esc | **Force-kill** active session's foreground process (SIGTERM via tmux `pane_pid`) |
| **21** (R2 C3) | Go (submit) | **Git push** — sends `git push origin <current-branch>` to the pane |
| **35** (R3 C8) | Status blast | **Manual prune** — invokes `_prune_dead` to sweep zombie panes |
| **0–6, 8, 22–31** (session keys) | Focus/toggle session | **Tail agentdeck log** — opens 30% tmux pane with `journalctl -u streamdeck-agentdeck -f` |

**Anti-bounce:** tap vs hold is discriminated on key-RELEASE. A quick tap (~0.1s) fires the short action on release; holding past 0.6s fires the long action and suppresses the short. Wake-from-sleep presses never start the long-press timer.

---

## LCD strip layout (1200 × 100 px) — 3 panorama modes

Long-press **key 7** cycles: `normal → laputa → beach → normal`. Default boot mode: **beach**.

### Top-half overlays (all modes)

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ▶ session · activity_label                              ⚠ N waiting  Fri │  y=6–22
│                                                          (pulse)  14:32 │
│                                                                          │
│  ❶ 1 Yes     ❷ 2 No     ❸ 3 Don't ask    ❹ 4 —                        │  y=28–42
│  (OR: ● ● ● ● ●  ● ● ● ●  heartbeat + host dots)                        │
└──────────────────────────────────────────────────────────────────────────┘
```

Top-half text (banner, time, needy badge, reply menu / heartbeat+host dots) renders on top of whatever panorama is showing. Drop-shadows (4-direction black outline) are enabled in `laputa` and `beach` modes so text stays readable over photo-real backgrounds.

### Mode: `normal`

```
┌────────┬────────┬────────┬─────────────────────────────┬────────┐
│  glm   │select  │ld·cpu·m│ ☀  88°F   [GRILL]           │  60%   │   y=44–100
│        │ 1/3    │1.2 23 45│    CLR   ok                │ ▓▓░░░  │
└────────┴────────┴────────┴─────────────────────────────┴────────┘
 ← 200 → ← 200 → ← 200 → ←─────── 400 (weather) ─────→ ← 200 →
```

Five visual knob zones. System stats merged (load/cpu/mem). Weather + grill zone (400px) with animated condition icon + temp + grill verdict badge.

### Mode: `laputa` — Ghibli Siege panorama (existing)

Full 1200×100 procedural canvas from `ghibli_scenes.render_touchscreen_banner(phase)`. Fantasy airship battle at golden hour. Knob zones render on top with drop-shadows.

### Mode: `beach` — the city live-weather panorama (NEW)

Full 1200×100 procedural canvas from `ghibli_beach.render_fort_myers_beach(phase, weather, local_hour)`. Reflects real the city GPS coords + live NWS weather conditions:

**Layered render chain:**

| Layer | Source | Notes |
|---|---|---|
| Sky gradient | 5 stop-bands, palette keyed to local hour | Interpolated across night → sunrise → day → sunset → twilight |
| Celestial body | Sun (day) or moon+stars (night) | Position derived from `local_hour` — arc across the frame |
| Far clouds | Cumulus (CLR/CLD), stratus (RAIN), heavy dark (TSTM) | Count + tint driven by `icon_word` |
| Horizon island | Distant landmass silhouette at sky-ocean boundary | Fixed shape, palette-tinted |
| Ocean gradient | Blue-teal, darker when overcast | |
| Wave caps + surf line | Animated with `_anim_phase` (20fps) | Small white pixel lines |
| Sand | Tan/gold trapezoid with per-pixel noise texture | |
| Palm trees | 3 silhouettes at fixed x positions | Fronds sway with phase |

**Weather overlays (conditional on `icon_word`):**
- `CLR` (temp<95): bright sun rays, calm waves
- `CLR` (temp≥95): sun + heat shimmer (wavy lines above sand)
- `CLD`: overcast gray wash, no sun rays, flat clouds
- `DRIZ`: sparse slow rain streaks
- `RAIN`: dense fast rain streaks, choppy wave caps, dark clouds
- `TSTM`: very dark sky, lightning bolt flash every ~1.4s, heavy rain
- `FOG`: horizontal translucent bands drifting across frame
- `SNOW`: drifting flakes with sine-wave x motion

**Palette by time-of-day (5 phases, linear interpolation at boundaries):**

| Hour | Phase | Sky zenith | Sand |
|---|---|---|---|
| 0–5 | Night | `(8,12,35)` | `(35,35,45)` |
| 5–7 | Sunrise | `(50,30,70)` | `(120,90,70)` |
| 7–17 | Day | `(70,130,200)` | `(220,200,150)` |
| 17–19 | Sunset | `(60,40,90)` | `(160,110,80)` |
| 19–24 | Twilight | `(15,20,45)` | `(60,50,70)` |

All knob zones (session/reply/system/weather/brightness) **always render** — in panorama modes they overlay the scene with drop-shadows on text labels for readability.

### Elements (top-half, all modes)

| Element | Position | When | Description |
|---|---|---|---|
| **Banner** | y=6, left | Always | `▶ <session> · <activity>` — activity uses live `_activity` label (e.g., "Wrangling 1m 20s") |
| **Time** | y=8, right-anchored | Always | `Fri 14:32:07` (FONT_R 16) |
| **Needy badge** | y=9, right of time | When >1 session needs input | `⚠ N waiting` — pulses at 1.0s period (amber sine). Hidden when count ≤ 1 |
| **Reply preview** | y=28–42, 4 segments | When active session has live menu | Shows 4 menu options. The ❯ cursor option gets bright green; others muted blue; empty zones dim |
| **Heartbeat dots** | y=28–36, left | When NO live menu | One dot per session (up to MAX_SESSIONS), leftmost=active. Breathing pulse for running, steady red=error, green=done, dim=idle |
| **Host dots** | y=30–38, right of heartbeats | When NO live menu | Green/red dots for the-deck-host, server-host, nas-host, git-host reachability |

### Knob behavior (dials)

| Dial | Turn | Behavior |
|---|---|---|
| 1 | Left/Right | Move session selection cursor (5s auto-focus suppression) |
| 2 | Left/Right | Cycle reply set (select/keys/type) |
| 3 | Left/Right | Page the top row (no on-screen label — knob hardware kept for muscle memory) |
| 4 | — | Unassigned |

---

## Key geometry constants (deck.py)

```
XL_BOARD_SLOTS = range(0,9) + range(22,32)   # 19 session slots
XL_REPLY0      = 9                            # keys 9–12: reply zones
XL_SLASH0      = 13                           # keys 13–17: /commit /resume /clear /cost /super-worker
XL_QUICK0      = 18                           # keys 18–21: Esc S-Tab Voice Go
XL_TOOL_SWAP   = 32                           # R3 C5: tool cycle
XL_GOAL0       = 33                           # keys 33–34: /goal /goal complete
XL_STATUS      = 35                           # R3 C8: status blast
```

## Color coding (Ghibli palette)

| Element | Color (RGB) | Meaning |
|---|---|---|
| Cool blue `(28, 38, 60)` | Slash keys background | R1 C4–C8 |
| Green `(40, 60, 50)` | Status key | R3 C8 |
| Purple `(50, 40, 60)` | Goal keys | R3 C6–C7 |
| Amber `(60, 50, 30)` | Tool swap key | R3 C5 |
| Red wash | Reply key recording | Active menu recording |
| Breathing meadow | Needy+active session | Auto-focus target |
| Static 0.18 wash | Needy+not-active | Other sessions waiting |
