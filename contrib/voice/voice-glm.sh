#!/usr/bin/env bash
# voice-glm.sh — PTT toggle for GLM dictation into active Konsole (claude-glm prompt)
# Press once: start recording. Press again: stop → transcribe → inject into Konsole prompt.
#
# Coexists with voice-capture.sh (Rosie). Bind a DIFFERENT KDE global hotkey to this script.
# Native /voice is OAuth-gated to claude.ai; this is the token-auth GLM equivalent — pure
# STT dictation into whatever prompt is focused in the active Konsole session.
#
# ponytail: duplicates ~30 lines of voice-capture.sh record/transcribe logic
#           — upgrade: factor into voice-lib.sh if a third mode appears

VENV="$HOME/.local/share/voice-rosie/venv"
WHISPER_MODELS="$HOME/.local/share/voice-rosie/models/whisper"
TRANSCRIBE="$HOME/.local/share/voice-rosie/transcribe.py"
WAV=/tmp/voice-glm.wav
PID_FILE=/tmp/voice-glm-rec.pid
TRANSCRIPT=/tmp/voice-glm-transcript.txt
LOG="$HOME/.local/share/voice-rosie/voice-glm.log"
MAX_DURATION=60  # safety cap: auto-stop after 60s

_log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*" >> "$LOG"; }

if [[ -f "$PID_FILE" ]]; then
    ARECORD_PID=$(cat "$PID_FILE")
    # Stale-toggle guard: if the recorder is dead AND the wav is old, a prior
    # recording hit MAX_DURATION unattended (e.g. extra press during the
    # transcription delay). Treat THIS press as a fresh start, not a stop —
    # otherwise the toggle inverts and every press transcribes stale audio.
    if ! kill -0 "$ARECORD_PID" 2>/dev/null; then
        WAV_AGE=$(( $(date +%s) - $(stat -c %Y "$WAV" 2>/dev/null || echo 0) ))
        if (( WAV_AGE > MAX_DURATION + 30 )); then
            _log "stale pidfile (recorder dead, wav ${WAV_AGE}s old) — restarting fresh"
            rm -f "$PID_FILE"
            exec "$0"
        fi
    fi
    # Second press — stop recording and transcribe
    kill "$ARECORD_PID" 2>/dev/null || true
    rm -f "$PID_FILE"
    notify-send -t 3000 "GLM" "Transcribing..." 2>/dev/null || true
    # wait for arecord to actually exit and flush the wav (max 2s)
    for _ in $(seq 1 20); do kill -0 "$ARECORD_PID" 2>/dev/null || break; sleep 0.1; done

    # capture-level evidence: rms/peak of what the mic actually heard
    STATS=$("$VENV/bin/python3" - "$WAV" <<'PY' 2>/dev/null
import sys, wave, struct
w = wave.open(sys.argv[1]); f = w.readframes(w.getnframes())
s = struct.unpack('<%dh' % (len(f)//2), f) or (0,)
print("rms=%d peak=%d frames=%d" % ((sum(x*x for x in s)/len(s))**0.5, max(abs(x) for x in s), w.getnframes()))
PY
)
    _log "audio: ${STATS:-unreadable} bytes=$(stat -c%s "$WAV" 2>/dev/null)"

    # small model: ~4x faster than medium on CPU — dictation latency beats
    # the marginal accuracy gain (long delay makes users double-press).
    WHISPER_MODEL="${WHISPER_MODEL:-small}" \
        "$VENV/bin/python3" "$TRANSCRIBE" "$WAV" "$WHISPER_MODELS" > "$TRANSCRIPT" 2>>"$LOG"
    UTTERANCE=$(cat "$TRANSCRIPT" 2>/dev/null)

    if [[ -z "$UTTERANCE" ]]; then
        _log "STT: empty transcript"
        notify-send -t 3000 "GLM" "Didn't catch that" 2>/dev/null || true
        exit 0
    fi

    # Sanitize: collapse newlines to spaces (raw \n via DBus would submit the prompt)
    # and trim leading/trailing whitespace.
    UTTERANCE=$(tr '\n' ' ' <<< "$UTTERANCE" | sed 's/^ *//; s/ *$//')

    _log "STT: $UTTERANCE"

    # Inject into active Konsole session (where claude-glm is running) — only
    # when a local desktop bus exists. Remote callers (agentdeck via ssh) read
    # $TRANSCRIPT themselves and inject on their side.
    if [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}${DISPLAY:-}" ]]; then
        if ! bash "$HOME/.local/bin/konsole-send.sh" --no-enter "$UTTERANCE" 2>/dev/null; then
            _log "ERROR: konsole-send.sh failed (no Konsole running?)"
            notify-send -t 3000 "GLM" "No Konsole to receive text" 2>/dev/null || true
            exit 1
        fi
        notify-send -t 2000 "GLM" "Injected into prompt" 2>/dev/null || true
    else
        _log "no local display/bus; transcript left in $TRANSCRIPT for caller"
    fi
    _log "DONE"
else
    # First press — start recording
    rm -f "$WAV"
    notify-send -t 60000 "GLM" "Listening... (press again to stop)" 2>/dev/null || true
    _log "REC: start"
    trap 'kill "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; rm -f "$PID_FILE"' EXIT INT TERM
    # setsid + closed fds: recorder must not inherit the caller's pipes, or an
    # ssh invocation (agentdeck) hangs until MAX_DURATION and its timeout-kill
    # tears down the recording mid-capture.
    setsid arecord -f cd -q -d "$MAX_DURATION" "$WAV" </dev/null >/dev/null 2>>"$LOG" &
    echo $! > "$PID_FILE"
    trap - EXIT INT TERM  # child is detached; PID file owns cleanup
fi
