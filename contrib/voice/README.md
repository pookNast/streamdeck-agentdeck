# Voice dictation stack (the-deck-host side)

Canonical home for the voice scripts the deck's Voice button drives. They install
OUTSIDE this repo (paths below are gitignored in the home repo), so treat this
directory as source of truth and re-copy after edits.

| File | Installs to | Role |
|---|---|---|
| `voice-glm.sh` | `~/.local/bin/voice-glm.sh` (the-deck-host) | PTT toggle: press 1 records (mic is on the-deck-host), press 2 transcribes and leaves `/tmp/voice-glm-transcript.txt` for the caller |
| `transcribe.py` | `~/.local/share/voice-rosie/transcribe.py` (the-deck-host) | faster-whisper helper (shared with Voice Rosie); `WHISPER_MODEL` env overrides model |

Flow with the deck: `deck.py:_voice_toggle` (the-host) → `ssh the-deck-host voice-glm.sh`
(returns instantly; recorder is `setsid`-detached) → second press transcribes →
deck cats the transcript and injects via `tmux send-keys -l` into the session
pane. No Konsole/DBus on the the-deck-host side — injection is skipped when no local
desktop bus exists.

Hardening shipped 2026-07-05 (see home memory `project_agentdeck_voice_fix`):
- `setsid` + closed fds — recorder no longer inherits ssh pipes (press was
  hanging 10s and the timeout-kill truncated recordings).
- Stale-toggle guard — a recording that hits MAX_DURATION unattended leaves a
  dead pidfile; next press now detects it (wav older than MAX_DURATION+30s) and
  restarts fresh instead of inverting the toggle.
- Whisper `small` + VAD + `condition_on_previous_text=False` + pinned English —
  silence returns empty instead of "Thanks for watching!" hallucinations;
  ~10s latency (model load dominates).
- Per-capture `audio: rms=… peak=…` line in `~/.local/share/voice-rosie/voice-glm.log`
  — first thing to read when voice "doesn't work": rms < 100 ≈ mic/capture
  problem, healthy speech ≈ 3000+. Mic = Umik-1, source at 150% (+10.6 dB).

Expect ~10 s between second press and text appearing — do NOT press again while
waiting (that starts a new recording; harmless now, but confusing).
