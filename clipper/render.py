"""FFmpeg rendering: EditPlan + analysed clips -> 1080x1920 MP4.

Each clip is rendered to its own segment (jump cuts, layout, look, zooms,
shakes, flashes, captions, SFX). The segments are then concatenated in a
final pass that adds the progress bar, the music bed (ducked under speech)
and loudness normalisation, and exports a cover image.
"""
from __future__ import annotations

import random
import re
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .captions import write_ass
from .config import APP_DIR, FONT_DIR, get_settings
from .models import ClipEdit, ClipInfo, EditPlan, FlashCue, SfxCue, TextOverlay
from .sfx import sfx_path
from .silence import detect_silences, keep_ranges, kept_length, remapper, select_expr

W, H, FPS = 1080, 1920, 30
MUSIC_DIR = APP_DIR / "music"
Log = Callable[[str], None]
Progress = Callable[[float, str], None]
Cancel = Callable[[], bool]


class Cancelled(RuntimeError):
    pass


def _ff_path(p: str | Path) -> str:
    """Escape a path for use inside an FFmpeg filter option (Windows drive colons!)."""
    s = str(Path(p).resolve()).replace("\\", "/")
    return s.replace(":", "\\:").replace("'", "\\'")


def _even(x: float) -> int:
    return int(round(x / 2.0)) * 2


def _hex_to_ff(color: str) -> str:
    return "0x" + color.lstrip("#")


# --------------------------------------------------------------------------
# Expression builders (evaluated per frame inside zoompan)
# --------------------------------------------------------------------------
def _zoom_expr(edit: ClipEdit, tmap: Callable[[float], float]) -> str:
    """zoom(t) = 1 + sum of punch-ins; a fast ease-in and a slower ease-out."""
    terms = []
    for z in edit.zooms:
        a = tmap(z.t)
        amp = z.strength - 1.0
        up = f"min(1,max(0,(T-{a:.3f})/0.07))"
        down = f"(1-min(1,max(0,(T-{a + z.hold:.3f})/0.22)))"
        terms.append(f"{amp:.3f}*{up}*{down}")
    for s in edit.shakes:  # a little zoom gives the shake room to move
        a = tmap(s.t)
        terms.append(f"0.08*between(T,{a:.3f},{a + s.duration:.3f})")
    return "1+" + "+".join(terms) if terms else "1"


def _shake_expr(edit: ClipEdit, seed: int, tmap: Callable[[float], float] = lambda t: t) -> str:
    terms = []
    for i, s in enumerate(edit.shakes):
        a = tmap(s.t)
        b = a + s.duration
        amp = 28 * s.intensity
        decay = f"(1-(T-{a:.3f})/{s.duration:.3f})"
        terms.append(f"between(T,{a:.3f},{b:.3f})*(random({seed + i})-0.5)*2*{amp:.1f}*{decay}")
    return "+".join(terms) if terms else "0"


def _with_t(expr: str) -> str:
    return expr.replace("T", f"(in/{FPS})")


# --------------------------------------------------------------------------
# Per-clip segment
# --------------------------------------------------------------------------
LOOKS = {
    "none": "",
    "punchy": "eq=contrast=1.07:saturation=1.22:brightness=0.01,unsharp=5:5:0.55:5:5:0.0",
    "warm": "eq=contrast=1.05:saturation=1.15,colorbalance=rs=0.06:gs=0.01:bs=-0.06,unsharp=5:5:0.4",
    "cold": "eq=contrast=1.06:saturation=1.1,colorbalance=rs=-0.05:bs=0.07,unsharp=5:5:0.4",
    "vintage": "eq=contrast=0.98:saturation=0.85:gamma=1.05,colorbalance=rs=0.05:bs=-0.03,vignette=PI/5",
}


def _layout_chain(clip: ClipInfo, layout: str, accent: str) -> str:
    """Return a filter chain that maps [v0] -> [base] at 1080x1920."""
    if clip.portrait or (clip.height and clip.width and clip.height > clip.width):
        layout = "fill"
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
            f"[cam][game]vstack,drawbox=y={cam_h - 3}:h=6:w={W}:c={_hex_to_ff(accent)}:t=fill[base]"
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
                   log: Optional[Log] = None, progress: Optional[Progress] = None,
                   cancel: Optional[Cancel] = None) -> float:
    """Render one clip; returns the segment length in seconds."""
    log = log or (lambda m: None)
    s = get_settings()
    src_in = edit.trim_start
    src_dur = max(0.5, clip.duration - edit.trim_start - edit.trim_end)

    # --- jump cuts ---------------------------------------------------------------
    keeps = [(0.0, src_dur)]
    if edit.remove_silence and src_dur > 3:
        sil = detect_silences(clip.path, src_in, src_dur)
        protect = [z.t for z in edit.zooms] + [x.t for x in edit.sfx] + [p.t - src_in for p in clip.peaks]
        keeps = keep_ranges(src_dur, sil, protect=protect)
        removed = src_dur - kept_length(keeps)
        if removed > 0.05:
            log(f"[{clip.id}] jump cuts: removed {removed:.1f}s of silence in {len(keeps) - 1} places")
    tmap = remapper(keeps)
    length = kept_length(keeps) / edit.speed
    cut = len(keeps) > 1

    overlays = list(edit.overlays) + list(extra_overlays)
    ass_file = write_ass(work / f"{clip.id}.ass", clip.transcript.words, plan.caption_style,
                         edit.emphasis_words, overlays, time_offset=src_in, speed=edit.speed, remap=tmap)

    def ct(t: float) -> float:  # cue time -> output time
        return tmap(t) / edit.speed

    # --- inputs ------------------------------------------------------------
    cmd: List[str] = [s.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
                      "-ss", f"{src_in:.3f}", "-t", f"{src_dur:.3f}", "-i", clip.path]
    sfx_cues = list(edit.sfx)
    if not is_first and plan.transition_sfx:
        sfx_cues.insert(0, SfxCue(t=0.0, name=plan.transition_sfx, gain=0.8))
    for cue in sfx_cues:
        cmd += ["-i", str(sfx_path(cue.name))]
    has_audio = _has_audio(clip)
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    audio_src = "0:a" if has_audio else f"{1 + len(sfx_cues)}:a"

    # --- video graph ----------------------------------------------------------
    pre = "[0:v]setpts=PTS-STARTPTS,"
    if cut:
        pre += f"select='{select_expr(keeps)}',setpts=N/FRAME_RATE/TB,"
    pre += f"setpts=PTS/{edit.speed:.4f},fps={FPS},format=yuv420p"
    look = LOOKS.get(plan.look, "")
    if look:
        pre += "," + look
    v = [pre + "[v0]", _layout_chain(clip, edit.layout, plan.caption_style.highlight_color)]
    zoom = _with_t(_zoom_expr(edit, ct))
    shake_x = _with_t(_shake_expr(edit, 11, ct))
    shake_y = _with_t(_shake_expr(edit, 57, ct))
    v.append(
        f"[base]zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)+({shake_x})':y='ih/2-(ih/zoom/2)+({shake_y})'"
        f":d=1:s={W}x{H}:fps={FPS}[vz]"
    )
    flashes = [FlashCue(t=ct(f.t), duration=f.duration) for f in edit.flashes]
    if not is_first and plan.transition_flash:
        flashes.insert(0, FlashCue(t=0.0, duration=0.06))
    last = "vz"
    for i, f in enumerate(flashes):
        nxt = f"vf{i}"
        v.append(f"[{last}]drawbox=c=white@0.8:t=fill:enable='between(t,{f.t:.3f},{f.t + f.duration:.3f})'[{nxt}]")
        last = nxt
    v.append(f"[{last}]ass='{_ff_path(ass_file)}':fontsdir='{_ff_path(FONT_DIR)}'[vout]")

    # --- audio graph ----------------------------------------------------------
    apre = f"[{audio_src}]asetpts=PTS-STARTPTS,"
    if cut:
        apre += f"aselect='{select_expr(keeps)}',asetpts=N/SR/TB,"
    a = [apre + f"{_atempo(edit.speed)}aresample=48000,aformat=channel_layouts=stereo,"
         f"dynaudnorm=f=250:g=15:p=0.9[a0]"]
    mix_in = ["[a0]"]
    for i, cue in enumerate(sfx_cues):
        ms = int(ct(cue.t) * 1000)
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
    log(f"[{clip.id}] rendering segment ({length:.1f}s, layout={edit.layout}, look={plan.look}, "
        f"{len(edit.zooms)} zooms, {len(sfx_cues)} sfx)")
    _run_ffmpeg(cmd, length, log, progress, cancel)
    return length


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


def _run_ffmpeg(cmd: List[str], total: float, log: Log, progress: Optional[Progress],
                cancel: Optional[Cancel] = None) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    assert proc.stdout is not None
    for line in proc.stdout:
        if cancel and cancel():
            proc.kill()
            proc.wait()
            raise Cancelled("cancelled by user")
        m = _TIME_RE.search(line)
        if m and progress and total > 0:
            progress(min(1.0, int(m.group(1)) / 1_000_000 / total), "")
    _, err = proc.communicate()
    if proc.returncode != 0:
        tail = "\n".join(err.strip().splitlines()[-15:])
        log("ffmpeg failed:\n" + tail)
        raise RuntimeError(f"ffmpeg exited with {proc.returncode}: {tail[-400:]}")


# --------------------------------------------------------------------------
# Music
# --------------------------------------------------------------------------
MUSIC_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}


def list_music() -> List[str]:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p.name for p in MUSIC_DIR.iterdir() if p.suffix.lower() in MUSIC_EXT)


def pick_music(plan: EditPlan) -> Optional[Path]:
    if not plan.background_music:
        return None
    files = list_music()
    if not files:
        return None
    if plan.music_file and plan.music_file in files:
        return MUSIC_DIR / plan.music_file
    return MUSIC_DIR / random.choice(files)


# --------------------------------------------------------------------------
# Whole video
# --------------------------------------------------------------------------
def render(plan: EditPlan, clips: Sequence[ClipInfo], out: Path, work: Path,
           log: Optional[Log] = None, progress: Optional[Progress] = None,
           cancel: Optional[Cancel] = None) -> Path:
    log = log or (lambda m: None)
    s = get_settings()
    work.mkdir(parents=True, exist_ok=True)
    by_id: Dict[str, ClipInfo] = {c.id: c for c in clips}
    edits = [e for e in plan.clips if e.clip_id in by_id]
    if not edits:
        raise RuntimeError("plan contains no renderable clips")

    segments: List[Path] = []
    lengths: List[float] = []
    n = len(edits)
    for i, edit in enumerate(edits):
        if cancel and cancel():
            raise Cancelled("cancelled by user")
        clip = by_id[edit.clip_id]
        extra = []
        if i == 0 and plan.hook:
            extra.append(TextOverlay(text=plan.hook, start=0.12, end=2.6, position="top"))
        seg = work / f"seg_{i:02d}_{clip.id}.mp4"

        def seg_progress(frac: float, _msg: str, i=i):
            if progress:
                progress((i + frac) / n * 0.85, f"rendering clip {i + 1}/{n}")

        lengths.append(render_segment(clip, edit, plan, seg, work, extra_overlays=extra, is_first=(i == 0),
                                      log=log, progress=seg_progress, cancel=cancel))
        segments.append(seg)

    total = sum(lengths)
    lst = work / "concat.txt"
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in segments), encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)

    music = pick_music(plan)
    log("final pass: " + ", ".join(x for x in [
        "progress bar" if plan.progress_bar else "", f"music ({music.name})" if music else "",
        "loudness -14 LUFS"] if x))
    cmd = [s.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
           "-f", "concat", "-safe", "0", "-i", str(lst)]
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]

    vf: List[str] = []
    if plan.progress_bar:
        col = _hex_to_ff(plan.caption_style.highlight_color)
        vf.append(f"color=c={col}@0.9:s={W}x12:r={FPS}[bar];"
                  f"[0:v][bar]overlay=x='-{W}+{W}*t/{total:.3f}':y={H - 12}:shortest=1:format=auto[v]")
    else:
        vf.append("[0:v]null[v]")
    af: List[str] = []
    if music:
        vol = plan.music_volume
        af.append("[0:a]asplit=2[sp][sc];"
                  f"[1:a]aformat=channel_layouts=stereo:sample_rates=48000,volume={vol:.3f}[m];"
                  "[m][sc]sidechaincompress=threshold=0.03:ratio=8:attack=40:release=500:makeup=1[md];"
                  "[sp][md]amix=inputs=2:duration=first:normalize=0:dropout_transition=0,"
                  "loudnorm=I=-14:TP=-1.5:LRA=11[a]")
    else:
        af.append("[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]")
    graph_file = work / "final.filter"
    graph_file.write_text(";".join(vf + af), encoding="utf-8")

    enc = _encoder_args(s.pick_encoder())
    cmd += ["-filter_complex_script", str(graph_file), "-map", "[v]", "-map", "[a]",
            "-t", f"{total:.3f}", "-r", str(FPS), *enc,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
            "-progress", "pipe:1", str(out)]

    def final_progress(frac: float, _m: str):
        if progress:
            progress(0.85 + frac * 0.15, "finalising")

    _run_ffmpeg(cmd, total, log, final_progress, cancel)

    # cover image: the first punch-in of the first clip is usually the money shot
    first = edits[0]
    cover_t = min(lengths[0] - 0.1, first.zooms[0].t + 0.15) if first.zooms else min(1.0, lengths[0] / 2)
    try:
        subprocess.run([s.ffmpeg, "-y", "-v", "error", "-ss", f"{max(0.0, cover_t):.3f}", "-i", str(out),
                        "-frames:v", "1", "-q:v", "2", str(out.with_suffix(".jpg"))],
                       check=True, capture_output=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        log(f"cover image failed: {exc}")
    if progress:
        progress(1.0, "done")
    return out
