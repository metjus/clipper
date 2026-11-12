"""Local web UI (FastAPI). Everything runs on this machine; nothing is uploaded anywhere."""
from __future__ import annotations

import os
import platform
import re
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from ..captions import build_ass
from ..config import FONT_DIR, get_settings
from ..downloader import discover
from ..media import frame_jpeg
from ..models import Box, CaptionStyle, EditPlan, Word
from ..pipeline import JobManager, JobOptions
from ..render import MUSIC_DIR, list_music

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="AI clipper")
manager = JobManager()


class NewJob(BaseModel):
    urls: List[str]
    options: dict = {}


class SettingsIn(BaseModel):
    anthropic_api_key: Optional[str] = None
    claude_model: Optional[str] = None
    whisper_model: Optional[str] = None
    whisper_device: Optional[str] = None
    output_dir: Optional[str] = None
    video_encoder: Optional[str] = None


class FacecamIn(BaseModel):
    box: Optional[Box] = None


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
@app.get("/api/settings")
def get_settings_api():
    s = get_settings()
    d = s.public()
    d["encoder"] = s.pick_encoder()
    d["music_dir"] = str(MUSIC_DIR)
    d["music_files"] = list_music()
    d["platform"] = platform.system()
    return d


@app.post("/api/settings")
def set_settings_api(body: SettingsIn):
    s = get_settings()
    for k, v in body.model_dump(exclude_none=True).items():
        if k == "anthropic_api_key" and v == "set":
            continue
        setattr(s, k, v)
    s.save()
    return s.public()


@app.post("/api/settings/test")
def test_settings():
    s = get_settings()
    if not s.anthropic_api_key:
        return {"ok": False, "message": "no API key set"}
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=s.anthropic_api_key)
        client.models.retrieve(s.claude_model)
        return {"ok": True, "message": f"API key works, model {s.claude_model} available"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}


@app.post("/api/open-output")
def open_output():
    p = get_settings().out_path
    try:
        if platform.system() == "Windows":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return {"ok": True, "path": str(p)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "path": str(p), "message": str(exc)}


# --------------------------------------------------------------------------
# discovery + uploads
# --------------------------------------------------------------------------
@app.get("/api/discover")
def discover_api(channel: str, platform_: str = "twitch", range: str = "7d", limit: int = 20):  # noqa: A002
    if not channel.strip():
        raise HTTPException(400, "channel is empty")
    try:
        return discover(channel, platform_, range, limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"{type(exc).__name__}: {exc}")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    dest_dir = get_settings().work_path / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w.\-]+", "_", file.filename or "clip.mp4")
    dest = dest_dir / name
    i = 1
    while dest.exists():
        dest = dest_dir / f"{Path(name).stem}_{i}{Path(name).suffix}"
        i += 1
    with dest.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            fh.write(chunk)
    return {"path": str(dest), "name": dest.name}


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
@app.post("/api/jobs")
def create_job(body: NewJob):
    urls = [u.strip() for u in body.urls if u.strip()]
    if not urls:
        raise HTTPException(400, "no URLs given")
    if not get_settings().ffmpeg_ok():
        raise HTTPException(400, "ffmpeg not found - install it (winget install Gyan.FFmpeg) and restart")
    job = manager.submit(urls, JobOptions.from_dict(body.options))
    return job.to_dict()


@app.get("/api/jobs")
def list_jobs():
    return [j.to_dict(full=False) for j in sorted(manager.jobs.values(), key=lambda j: -j.created)]


def _job(job_id: str):
    job = manager.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _job(job_id).to_dict()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = _job(job_id)
    manager.cancel(job)
    return job.to_dict(full=False)


@app.post("/api/jobs/{job_id}/rerender")
def rerender_job(job_id: str, plan: EditPlan):
    job = _job(job_id)
    if not job.clips:
        raise HTTPException(409, "job has no analysed clips")
    if job.status == "running":
        raise HTTPException(409, "job is running")
    manager.submit_rerender(job, plan)
    return job.to_dict()


@app.post("/api/jobs/{job_id}/clips/{clip_id}/facecam")
def set_facecam(job_id: str, clip_id: str, body: FacecamIn):
    job = _job(job_id)
    manager.set_facecam(job, clip_id, body.box)
    return job.to_dict(full=False)


@app.get("/api/jobs/{job_id}/clips/{clip_id}/frame")
def clip_frame(job_id: str, clip_id: str, t: float = 1.0, w: int = 640):
    job = _job(job_id)
    clip = next((c for c in job.clips if c.id == clip_id), None)
    if not clip:
        raise HTTPException(404, "clip not found")
    try:
        return Response(frame_jpeg(clip.path, t, w), media_type="image/jpeg")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc))


@app.get("/api/jobs/{job_id}/video")
def job_video(job_id: str):
    job = _job(job_id)
    if not job.output or not Path(job.output).exists():
        raise HTTPException(404, "no video yet")
    return FileResponse(job.output, media_type="video/mp4", filename=Path(job.output).name)


@app.get("/api/jobs/{job_id}/cover")
def job_cover(job_id: str):
    job = _job(job_id)
    if not job.output or not job.output.with_suffix(".jpg").exists():
        raise HTTPException(404, "no cover")
    return FileResponse(job.output.with_suffix(".jpg"), media_type="image/jpeg")


# --------------------------------------------------------------------------
# caption preview
# --------------------------------------------------------------------------
@app.get("/api/preview/captions")
def preview_captions(font: str = "LuckiestGuy", preset: str = "pop", highlight: str = "#FFE600",
                     accent: str = "#FF3B3B", size: int = 96, text: str = "NO WAY HE DID THAT"):
    s = get_settings()
    try:
        style = CaptionStyle(font=font, preset=preset, highlight_color=highlight, accent_color=accent,  # type: ignore[arg-type]
                             size=size, y_position=0.5, words_per_line=3)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))
    words = [Word(text=w, start=i * 0.5, end=i * 0.5 + 0.45) for i, w in enumerate(text.split()[:6])]
    emphasis = [words[2].text] if len(words) > 2 else []
    ass = build_ass(words, style, emphasis)
    with tempfile.TemporaryDirectory() as td:
        ass_path = Path(td) / "p.ass"
        ass_path.write_text(ass, encoding="utf-8")
        from ..render import _ff_path

        cmd = [s.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=0x1b1e26:s=1080x1920:r=30:d=2",
               "-vf", f"ass='{_ff_path(ass_path)}':fontsdir='{_ff_path(FONT_DIR)}',crop=1080:420:0:750,scale=540:-1",
               "-ss", "0.62", "-frames:v", "1", "-f", "image2", "-c:v", "png", "-"]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise HTTPException(500, r.stderr.decode(errors="replace")[-300:])
    return Response(r.stdout, media_type="image/png")


@app.get("/api/health")
def health():
    return JSONResponse({"ok": True})
