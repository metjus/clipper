"""Speech-to-text with word timestamps (faster-whisper, runs locally)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

from .config import get_settings
from .models import Transcript, Word

_model = None
_model_key = None


def _get_model():
    global _model, _model_key
    s = get_settings()
    key = (s.whisper_model, s.whisper_device, s.whisper_compute_type)
    if _model is not None and _model_key == key:
        return _model
    from faster_whisper import WhisperModel  # lazy: heavy import

    device = s.whisper_device
    compute = s.whisper_compute_type
    if device == "auto":
        device = "cuda" if _cuda_available() else "cpu"
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    try:
        _model = WhisperModel(s.whisper_model, device=device, compute_type=compute)
    except Exception:
        if device != "cpu":
            # CUDA libs missing etc. -> fall back to CPU
            _model = WhisperModel(s.whisper_model, device="cpu", compute_type="int8")
        else:
            raise
    _model_key = key
    return _model


def _cuda_available() -> bool:
    try:
        import ctranslate2  # type: ignore

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


_CLEAN = re.compile(r"^[\s\-—–]+|[\s]+$")


def transcribe(path: str | Path, language: Optional[str] = None,
               log: Optional[Callable[[str], None]] = None) -> Transcript:
    log = log or (lambda m: None)
    model = _get_model()
    segments, info = model.transcribe(
        str(path),
        language=language,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300},
        beam_size=5,
        condition_on_previous_text=False,
    )
    words = []
    for seg in segments:
        for w in seg.words or []:
            text = _CLEAN.sub("", w.word)
            if not text:
                continue
            words.append(Word(text=text, start=round(w.start, 3), end=round(max(w.end, w.start + 0.05), 3),
                              prob=round(float(w.probability or 1.0), 3)))
    log(f"transcribed {len(words)} words, language={info.language} ({info.language_probability:.2f})")
    return Transcript(language=info.language or "en", language_prob=float(info.language_probability or 0), words=words)
