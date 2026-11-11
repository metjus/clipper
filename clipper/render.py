"""FFmpeg rendering: EditPlan + analysed clips -> 1080x1920 MP4.

Each clip is rendered to its own segment (layout, zooms, shakes, flashes,
captions, SFX), then the segments are concatenated and loudness-normalised.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .captions import write_ass
from .config import FONT_DIR, get_settings
from .models import ClipEdit, ClipInfo, EditPlan, TextOverlay
from .sfx import sfx_path

W, H, FPS = 1080, 1920, 30
Log = Callable[[str], None]
Progress = Callable[[float, str], None]


def _ff_path(p: str | Path) -> str:
    """Escape a path for use inside an FFmpeg filter option (Windows drive colons!)."""
    s = str(Path(p).resolve()).replace("\\", "/")
    return s.replace(":", "\\:").replace("'", "\\'")


def _even(x: float) -> int:
    return int(round(x / 2.0)) * 2


# --------------------------------------------------------------------------
# Expression builders (evaluated per frame inside zoompan)
# --------------------------------------------------------------------------
def _zoom_expr(edit: ClipEdit) -> str:
    """zoom(t) = 1 + sum of punch-ins; a fast ease-in and a slower ease-out."""
    terms = []
    for z in edit.zooms:
        a = z.t
        amp = z.strength - 1.0
        up = f"min(1,max(0,(T-{a:.3f})/0.07))"
        down = f"(1-min(1,max(0,(T-{a + z.hold:.3f})/0.22)))"
        terms.append(f"{amp:.3f}*{up}*{down}")
    for s in edit.shakes:  # a little zoom gives the shake room to move
        a, b = s.t, s.t + s.duration
        terms.append(f"0.08*between(T,{a:.3f},{b:.3f})")
    return "1+" + "+".join(terms) if terms else "1"


def _shake_expr(edit: ClipEdit, seed: int) -> str:
    terms = []
    for i, s in enumerate(edit.shakes):
        a, b = s.t, s.t + s.duration
        amp = 28 * s.intensity
        decay = f"(1-(T-{a:.3f})/{s.duration:.3f})"
        terms.append(f"between(T,{a:.3f},{b:.3f})*(random({seed + i})-0.5)*2*{amp:.1f}*{decay}")
    return "+".join(terms) if terms else "0"


def _with_t(expr: str) -> str:
    return expr.replace("T", f"(in/{FPS})")


# --------------------------------------------------------------------------
# Per-clip segment
# --------------------------------------------------------------------------
def _layout_chain(clip: ClipInfo, layout: str) -> str:
    """Return a filter chain that maps [v0] -> [base] at 1080x1920."""
    if layout == "auto":
        layout = "split" if clip.facecam else "blur"
    if layout == "split" and clip.facecam is None:
        layout = "blur"

    if layout == "fill":
        return (f"[v0]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},setsar=1[base]")

    if layout == "split":
        fc = clip.facecam
        cw, ch = fc.w * clip.width, fc.h * clip.height
        cam_h = _even(W * ch / cw)
        cam_h = max(400, min(cam_h, 760))
        game_h = H - cam_h
        # the gameplay: take the full source height, centred, with the 1080:game_h aspect
        return (
            f"[v0]split=2[c][g];"
            f"[c]crop={_even(cw)}:{_even(ch)}:{int(fc.x * clip.width)}:{int(fc.y * clip.height)},"
            f"scale={W}:{cam_h}:flags=lanczos,setsar=1[cam];"
            f"[g]crop='min(iw,ih*{W}/{game_h})':ih:(iw-ow)/2:0,scale={W}:{game_h}:flags=lanczos,setsar=1[game];"
            f"[cam][game]vstack[base]"
        )

    # blur (default)
    return (
        f"[v0]split=2[bg][fg];"
        f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"gblur=sigma=28,eq=brightness=-0.12:saturation=1.25[bgb];"
        f"[fg]scale={W}:-2:flags=lanczos[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[base]"
    )


def render_segment(clip: ClipInfo, edit: ClipEdit, plan: EditPlan, out: Path, work: Path,
                   extra_overlays: Sequence[TextOverlay] = (), is_first: bool = False,
                   log: Optional[Log] = None, progress: Optional[Progress] = None) -> Path:
    log = log or (lambda m: None)
    s = get_settings()
    src_in = edit.trim_start
    src_dur = max(0.5, clip.duration - edit.trim_start - edit.trim_end)
    length = src_dur / edit.speed

    overlays = list(edit.overlays) + list(extra_overlays)
    ass_file = write_ass(work / f"{clip.id}.ass", clip.transcript.words, plan.caption_style,
                         edit.emphasis_words, overlays, time_offset=src_in, speed=edit.speed)

    # --- inputs ------------------------------------------------------------
    cmd: List[str] = [s.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
                      "-ss", f"{src_in:.3f}", "-t", f"{src_dur:.3f}", "-i", clip.path]
    sfx_cues = list(edit.sfx)
    if not is_first and plan.transition_sfx:
        sfx_cues.insert(0, _mk_sfx(plan.transition_sfx))
    sfx_inputs: List[str] = []
    for cue in sfx_cues:
        cmd += ["-i", str(sfx_path(cue.name))]
        sfx_inputs.append(cue.name)
    has_audio = _has_audio(clip)
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    audio_src = "0:a" if has_audio else f"{1 + len(sfx_cues)}:a"

    # --- video graph ----------------------------------------------------------
    v = [f"[0:v]setpts=(PTS-STARTPTS)/{edit.speed:.4f},fps={FPS},format=yuv420p[v0]", _layout_chain(clip, edit.layout)]
    zoom = _with_t(_zoom_expr(edit))
    shake_x = _with_t(_shake_expr(edit, 11))
    shake_y = _with_t(_shake_expr(edit, 57))
    v.append(
        f"[base]zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)+({shake_x})':y='ih/2-(ih/zoom/2)+({shake_y})'"
        f":d=1:s={W}x{H}:fps={FPS}[vz]"
    )
    flashes = list(edit.flashes)
    if not is_first and plan.transition_flash:
        flashes.insert(0, _mk_flash(0.0, 0.06))
    chain = "[vz]"
    last = "vz"
    for i, f in enumerate(flashes):
        nxt = f"vf{i}"
        v.append(f"[{last}]drawbox=c=white@0.8:t=fill:enable='between(t,{f.t:.3f},{f.t + f.duration:.3f})'[{nxt}]")
        last = nxt
    v.append(f"[{last}]ass='{_ff_path(ass_file)}':fontsdir='{_ff_path(FONT_DIR)}'[vout]")

    # --- audio graph ----------------------------------------------------------
    a = [f"[{audio_src}]asetpts=PTS-STARTPTS,{_atempo(edit.speed)}aresample=48000,aformat=channel_layouts=stereo,"
         f"dynaudnorm=f=250:g=15:p=0.9[a0]"]
    mix_in = ["[a0]"]
    for i, cue in enumerate(sfx_cues):
        ms = int(cue.t * 1000)
        a.append(f"[{1 + i}:a]aresample=48000,aformat=channel_layouts=stereo,volume={cue.gain * 0.9:.2f},"
                 f"adelay={ms}|{ms}[sfx{i}]")
        mix_in.append(f"[sfx{i}]")
    if len(mix_in) > 1:
        a.append("".join(mix_in) + f"amix=inputs={len(mix_in)}:duration=first:normalize=0:dropout_transition=0[aout]")
    else:
        a.append("[a0]anull[aout]")

    graph = ";".join(v + a)
    graph_file = work / f"{clip.id}.filter"
    graph_file.write_text(graph, encoding="utf-8")

    enc = _encoder_args(s.pick_encoder())
    cmd += ["-filter_complex_script", str(graph_file), "-map", "[vout]", "-map", "[aout]",
            "-t", f"{length:.3f}", "-r", str(FPS), *enc,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", "-progress", "pipe:1", str(out)]
    log(f"[{clip.id}] rendering segment ({length:.1f}s, layout={edit.layout}, "
        f"{len(edit.zooms)} zooms, {len(sfx_cues)} sfx)")
    _run_ffmpeg(cmd, length, log, progress)
    return out


def _mk_sfx(name: str):
    from .models import SfxCue
    return SfxCue(t=0.0, name=name, gain=0.8)


def _mk_flash(t: float, d: float):
    from .models import FlashCue
    return FlashCue(t=t, duration=d)


def _has_audio(clip: ClipInfo) -> bool:
    from .media import probe
    try:
        return bool(probe(clip.path)["has_audio"])
    except Exception:
        return True


def _atempo(speed: float) -> str:
    if abs(speed - 1.0) < 1e-3:
        return ""
    parts = []
    sp = speed
    while sp > 2.0:
        parts.append("atempo=2.0")
        sp /= 2.0
    while sp < 0.5:
        parts.append("atempo=0.5")
        sp /= 0.5
    parts.append(f"atempo={sp:.4f}")
    return ",".join(parts) + ","


def _encoder_args(encoder: str) -> List[str]:
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-profile:v", "high",
            "-pix_fmt", "yuv420p"]


_TIME_RE = re.compile(r"out_time_ms=(\d+)")


def _run_ffmpeg(cmd: List[str], total: float, log: Log, progress: Optional[Progress]) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    assert proc.stdout is not None
    for line in proc.stdout:
        m = _TIME_RE.search(line)
        if m and progress and total > 0:
            progress(min(1.0, int(m.group(1)) / 1_000_000 / total), "")
    _, err = proc.communicate()
    if proc.returncode != 0:
        tail = "\n".join(err.strip().splitlines()[-15:])
        log("ffmpeg failed:\n" + tail)
        raise RuntimeError(f"ffmpeg exited with {proc.returncode}: {tail[-400:]}")


# --------------------------------------------------------------------------
# Whole video
# --------------------------------------------------------------------------
def render(plan: EditPlan, clips: Sequence[ClipInfo], out: Path, work: Path,
           log: Optional[Log] = None, progress: Optional[Progress] = None) -> Path:
    log = log or (lambda m: None)
    s = get_settings()
    work.mkdir(parents=True, exist_ok=True)
    by_id: Dict[str, ClipInfo] = {c.id: c for c in clips}
    edits = [e for e in plan.clips if e.clip_id in by_id]
    if not edits:
        raise RuntimeError("plan contains no renderable clips")

    segments: List[Path] = []
    n = len(edits)
    for i, edit in enumerate(edits):
        clip = by_id[edit.clip_id]
        extra = []
        if i == 0 and plan.hook:
            extra.append(TextOverlay(text=plan.hook, start=0.12, end=2.6, position="top"))
        seg = work / f"seg_{i:02d}_{clip.id}.mp4"

        def seg_progress(frac: float, _msg: str, i=i):
            if progress:
                progress((i + frac) / n * 0.92, f"rendering clip {i + 1}/{n}")

        render_segment(clip, edit, plan, seg, work, extra_overlays=extra, is_first=(i == 0),
                       log=log, progress=seg_progress)
        segments.append(seg)

    lst = work / "concat.txt"
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in segments), encoding="utf-8")
    log("joining segments and normalising loudness")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [s.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
           "-f", "concat", "-safe", "0", "-i", str(lst),
           "-c:v", "copy", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
           "-progress", "pipe:1", str(out)]
    total = sum(_seg_len(p) for p in segments)

    def final_progress(frac: float, _m: str):
        if progress:
            progress(0.92 + frac * 0.08, "finalising")

    _run_ffmpeg(cmd, total, log, final_progress)
    if progress:
        progress(1.0, "done")
    return out


def _seg_len(p: Path) -> float:
    from .media import probe
    try:
        return probe(p)["duration"]
    except Exception:
        return 0.0
