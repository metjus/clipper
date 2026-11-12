"""Silence detection and jump-cut time remapping."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, List, Sequence, Tuple

from .config import get_settings

Range = Tuple[float, float]
_START = re.compile(r"silence_start: ([\d.]+)")
_END = re.compile(r"silence_end: ([\d.]+)")


def detect_silences(path: str | Path, start: float = 0.0, duration: float | None = None,
                    noise_db: float = -32.0, min_len: float = 0.6) -> List[Range]:
    """Return [(start, end)] of silent stretches, relative to ``start``."""
    s = get_settings()
    cmd = [s.ffmpeg, "-hide_banner", "-nostats", "-ss", f"{start:.3f}"]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", str(path), "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_len}", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception:
        return []
    starts = [float(m.group(1)) for m in _START.finditer(r.stderr)]
    ends = [float(m.group(1)) for m in _END.finditer(r.stderr)]
    out: List[Range] = []
    for i, a in enumerate(starts):
        b = ends[i] if i < len(ends) else (duration if duration else a)
        if b is not None and b > a:
            out.append((round(a, 3), round(b, 3)))
    return out


def keep_ranges(length: float, silences: Sequence[Range], pad: float = 0.14,
                min_cut: float = 0.45, protect: Sequence[float] = ()) -> List[Range]:
    """Turn silences into the parts of the clip we keep.

    ``pad`` seconds are kept on both sides of every silence so words are not
    clipped; a silence is only cut when the removable part is at least
    ``min_cut`` long. Times in ``protect`` (e.g. loud peaks, cue times) are
    never cut. The first/last 0.3 s are never cut either (the trims handle them).
    """
    cuts: List[Range] = []
    for a, b in silences:
        ca, cb = a + pad, b - pad
        if cb - ca < min_cut:
            continue
        ca, cb = max(ca, 0.3), min(cb, length - 0.3)
        if cb - ca < min_cut:
            continue
        if any(ca - 0.2 <= p <= cb + 0.2 for p in protect):
            continue
        cuts.append((ca, cb))
    keeps: List[Range] = []
    pos = 0.0
    for ca, cb in cuts:
        if ca > pos:
            keeps.append((pos, ca))
        pos = cb
    if length > pos:
        keeps.append((pos, length))
    return keeps or [(0.0, length)]


def remapper(keeps: Sequence[Range]) -> Callable[[float], float]:
    """Map a time on the original (trimmed) timeline to the jump-cut timeline."""
    table = []
    acc = 0.0
    for a, b in keeps:
        table.append((a, b, acc))
        acc += b - a

    def remap(t: float) -> float:
        for a, b, off in table:
            if t < a:
                return off          # inside a removed gap before this keep -> snap to its start
            if t <= b:
                return off + (t - a)
        return acc
    return remap


def kept_length(keeps: Sequence[Range]) -> float:
    return sum(b - a for a, b in keeps)


def select_expr(keeps: Sequence[Range]) -> str:
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in keeps)
