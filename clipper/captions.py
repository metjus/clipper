"""Build an ASS subtitle file with word-by-word "pop" captions.

Style reference: the big bold centred captions popular on TikTok / Shorts
(a few words at a time, the spoken word highlighted, a quick scale-in).
Rendered by FFmpeg's ``ass`` filter (libass).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence

from .models import CaptionStyle, TextOverlay, Word

W, H = 1080, 1920

FONT_FAMILY = {
    "LuckiestGuy": "Luckiest Guy",
    "Anton": "Anton",
    "Bangers": "Bangers",
}


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> '&HAABBGGRR'."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def _norm(word: str) -> str:
    return re.sub(r"[^\w']", "", word).lower()


def chunk_words(words: Sequence[Word], max_words: int = 3, max_chars: int = 16,
                max_gap: float = 0.7) -> List[List[Word]]:
    """Group words into short lines that are displayed together."""
    chunks: List[List[Word]] = []
    cur: List[Word] = []
    for w in words:
        if cur:
            gap = w.start - cur[-1].end
            chars = sum(len(x.text) for x in cur) + len(cur) + len(w.text)
            ends_sentence = cur[-1].text.rstrip()[-1:] in ".?!"
            if len(cur) >= max_words or chars > max_chars or gap > max_gap or ends_sentence:
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(words: Sequence[Word], style: CaptionStyle, emphasis: Iterable[str] = (),
              overlays: Sequence[TextOverlay] = (), time_offset: float = 0.0,
              speed: float = 1.0, remap: Optional[Callable[[float], float]] = None) -> str:
    """Return the full ASS document.

    ``time_offset`` is subtracted from every word time (clip trim), ``remap``
    (if given) maps trimmed-timeline seconds to the jump-cut timeline, and
    ``speed`` divides the timings (playback speed change). Overlay times are
    already on the trimmed timeline, so only ``remap`` and ``speed`` apply.
    """
    emph = {_norm(e) for e in emphasis}
    font = FONT_FAMILY[style.font]
    size = style.size
    primary = _ass_color(style.primary_color)
    highlight = _ass_color(style.highlight_color)
    accent = _ass_color(style.accent_color)
    outline = style.outline
    margin_v = int(H * (1 - style.y_position))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},{primary},{primary},&H00000000,&H80000000,0,0,0,0,100,100,1,0,1,{outline},3,5,40,40,{margin_v},1
Style: Hook,{font},{int(size * 0.95)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,1,0,1,{outline},4,8,60,60,{int(H * 0.11)},1
Style: Center,{font},{int(size * 1.1)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,1,0,1,{outline},4,5,60,60,{int(H * 0.5)},1
Style: Bottom,{font},{int(size * 0.9)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,1,0,1,{outline},4,2,60,60,{int(H * 0.16)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines: List[str] = []

    def t_(x: float) -> float:
        x = x - time_offset
        if remap is not None:
            x = remap(x)
        return max(0.0, x / speed)

    def o_(x: float) -> float:
        if remap is not None:
            x = remap(x)
        return max(0.0, x / speed)

    preset = style.preset
    cap_pos = r"{\an5\pos(%d,%d)}" % (W // 2, int(H * style.y_position))
    pop = ""
    if style.pop and preset in ("pop", "karaoke"):
        pop = r"{\fscx70\fscy70\t(0,70,\fscx112\fscy112)\t(70,130,\fscx100\fscy100)}"
    cur_scale = {"pop": 108, "karaoke": 106, "bounce": 100, "minimal": 104}[preset]
    bounce = r"\fscx135\fscy135\t(0,110,\fscx100\fscy100)" if preset == "bounce" else ""
    upper = style.uppercase and preset != "minimal"

    chunks = chunk_words(words, style.words_per_line)
    for ci, chunk in enumerate(chunks):
        chunk_start = t_(chunk[0].start)
        next_start = t_(chunks[ci + 1][0].start) if ci + 1 < len(chunks) else None
        for i, w in enumerate(chunk):
            start = t_(w.start)
            end = t_(chunk[i + 1].start) if i + 1 < len(chunk) else t_(chunk[-1].end) + 0.25 / speed
            if next_start is not None:
                end = min(end, next_start)
            if end - start < 0.04:   # word fell into a jump cut
                continue
            parts = []
            for j, x in enumerate(chunk):
                txt = _esc(x.text.upper() if upper else x.text)
                is_emph = _norm(x.text) in emph
                if j == i:
                    col = accent if is_emph else highlight
                    parts.append(r"{\c" + col + r"\fscx%d\fscy%d%s}" % (cur_scale, cur_scale, bounce)
                                 + txt + r"{\c" + primary + r"\fscx100\fscy100}")
                elif is_emph:
                    parts.append(r"{\c" + accent + "}" + txt + r"{\c" + primary + "}")
                elif preset == "karaoke" and j < i:
                    parts.append(r"{\c" + highlight + "}" + txt + r"{\c" + primary + "}")
                else:
                    parts.append(txt)
            text = " ".join(parts)
            prefix = pop if (i == 0 and start == chunk_start) else ""
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{cap_pos}{prefix}{text}")

    for ov in overlays:
        st = {"top": "Hook", "center": "Center", "bottom": "Bottom"}[ov.position]
        col = _ass_color(ov.color)
        oy = {"top": 0.12, "center": 0.44, "bottom": 0.86}[ov.position]
        anim = r"{\an5\pos(%d,%d)\c" % (W // 2, int(H * oy)) + col + r"\fscx60\fscy60\t(0,120,\fscx105\fscy105)\t(120,200,\fscx100\fscy100)}"
        txt = _esc(ov.text.upper() if upper else ov.text)
        lines.append(f"Dialogue: 1,{_ts(o_(ov.start))},{_ts(o_(ov.end))},{st},,0,0,0,,{anim}{txt}")

    return header + "\n".join(lines) + "\n"


def write_ass(path: Path, *args, **kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_ass(*args, **kwargs), encoding="utf-8")
    return path
