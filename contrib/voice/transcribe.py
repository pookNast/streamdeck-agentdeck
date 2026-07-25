#!/usr/bin/env python3
# transcribe.py — whisper transcription helper for Voice Rosie / voice-glm
# WHISPER_MODEL env overrides the model (voice-glm uses "small" for dictation
# latency; Rosie's default stays "medium").
import os
import sys
from faster_whisper import WhisperModel

wav = sys.argv[1]
models_dir = sys.argv[2]

model = WhisperModel(os.environ.get("WHISPER_MODEL", "medium"),
                     device="cpu", compute_type="int8", download_root=models_dir)
# vad_filter drops non-speech so silence yields "" instead of hallucinated
# stock phrases ("Thanks for watching!"); condition_on_previous_text=False
# stops repetition runaway; pinned language skips detection wobble on short clips.
segments, _ = model.transcribe(
    wav, vad_filter=True, condition_on_previous_text=False, language="en"
)
print(" ".join(seg.text for seg in segments).strip())
