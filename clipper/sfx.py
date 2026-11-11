"""Procedurally generated sound effects.

Everything is synthesised with numpy so the project ships no third-party audio
and has no licensing questions. Files are cached under the work dir.
Drop your own ``<name>.wav`` into ``~/.clipper/sfx/`` to override any of them.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from .config import APP_DIR

SR = 44100
USER_SFX_DIR = APP_DIR / "sfx"
CACHE_DIR = APP_DIR / "sfx_cache"


def _env(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / SR
    a = np.clip(t / max(attack, 1e-4), 0, 1)
    d = np.exp(-t / max(decay, 1e-4))
    return a * d


def _noise(n: int) -> np.ndarray:
    rng = np.random.default_rng(7)
    return rng.standard_normal(n).astype(np.float32)


def _lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    # one-pole IIR, good enough for SFX
    rc = 1.0 / (2 * np.pi * cutoff)
    dt = 1.0 / SR
    alpha = dt / (rc + dt)
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):
        acc += alpha * (v - acc)
        y[i] = acc
    return y


def _bandsweep_noise(dur: float, f0: float, f1: float) -> np.ndarray:
    """Noise whose brightness sweeps from f0 to f1 (whoosh / riser)."""
    n = int(dur * SR)
    x = _noise(n)
    # split in chunks, each lowpassed at an interpolated cutoff
    chunks = 24
    out = np.zeros(n, dtype=np.float32)
    step = n // chunks
    for i in range(chunks):
        c = f0 + (f1 - f0) * (i / (chunks - 1))
        s, e = i * step, (i + 1) * step if i < chunks - 1 else n
        out[s:e] = _lowpass(x[s:e], c)
    return out


def _normalise(x: np.ndarray, peak: float = 0.9) -> np.ndarray:
    m = np.abs(x).max()
    return (x / m * peak) if m > 0 else x


def gen_boom(dur: float = 1.2) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 160 * np.exp(-t * 6) + 38
    phase = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(phase) * _env(n, 0.003, 0.35)
    click = _lowpass(_noise(n), 1800) * _env(n, 0.001, 0.03) * 0.8
    sub = np.sin(2 * np.pi * 45 * t) * _env(n, 0.01, 0.5) * 0.6
    return _normalise(np.tanh((body + click + sub) * 1.8))


def gen_bass_drop(dur: float = 1.6) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 110 * np.exp(-t * 2.2) + 30
    phase = 2 * np.pi * np.cumsum(f) / SR
    x = np.sin(phase) * _env(n, 0.01, 0.9)
    return _normalise(np.tanh(x * 2.5))


def gen_whoosh(dur: float = 0.45) -> np.ndarray:
    n = int(dur * SR)
    x = _bandsweep_noise(dur, 400, 6000)
    env = np.sin(np.linspace(0, np.pi, n)) ** 1.5
    return _normalise(x * env, 0.7)


def gen_riser(dur: float = 1.5) -> np.ndarray:
    n = int(dur * SR)
    x = _bandsweep_noise(dur, 200, 9000)
    env = np.linspace(0.05, 1.0, n) ** 2
    return _normalise(x * env, 0.75)


def gen_ding(dur: float = 0.9) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = (np.sin(2 * np.pi * 1318 * t) + 0.5 * np.sin(2 * np.pi * 2637 * t) + 0.25 * np.sin(2 * np.pi * 3951 * t))
    return _normalise(x * _env(n, 0.002, 0.25), 0.7)


def gen_pop(dur: float = 0.12) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 900 * np.exp(-t * 40) + 150
    phase = 2 * np.pi * np.cumsum(f) / SR
    return _normalise(np.sin(phase) * _env(n, 0.001, 0.03), 0.8)


def gen_record_scratch(dur: float = 0.5) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 300 + 2500 * np.abs(np.sin(2 * np.pi * 3 * t))
    phase = 2 * np.pi * np.cumsum(f) / SR
    x = np.sign(np.sin(phase)) * 0.3 + _lowpass(_noise(n), 3000) * 0.7
    env = np.sin(np.linspace(0, np.pi, n)) ** 0.5
    return _normalise(x * env, 0.7)


GENERATORS = {
    "boom": gen_boom,
    "bass_drop": gen_bass_drop,
    "whoosh": gen_whoosh,
    "riser": gen_riser,
    "ding": gen_ding,
    "pop": gen_pop,
    "record_scratch": gen_record_scratch,
}


def _write_wav(path: Path, x: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def sfx_path(name: str) -> Path:
    """Return a wav file for ``name``, generating it on first use."""
    user = USER_SFX_DIR / f"{name}.wav"
    if user.is_file():
        return user
    cached = CACHE_DIR / f"{name}.wav"
    if not cached.is_file():
        gen = GENERATORS.get(name)
        if gen is None:
            raise KeyError(f"unknown sfx {name!r}")
        _write_wav(cached, gen())
    return cached


def sfx_duration(name: str) -> float:
    with wave.open(str(sfx_path(name)), "rb") as w:
        return w.getnframes() / w.getframerate()
