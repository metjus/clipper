"""Local web UI (FastAPI). Everything runs on this machine; nothing is uploaded anywhere."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from ..config import get_settings
from ..models import EditPlan
from ..pipeline import JobManager, JobOptions

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


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/settings")
def get_settings_api():
    s = get_settings()
    d = s.public()
    d["encoder"] = s.pick_encoder()
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
    return [j.to_dict() | {"log": [], "plan": None} for j in sorted(manager.jobs.values(), key=lambda j: -j.created)]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = manager.jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    return job.to_dict()


@app.post("/api/jobs/{job_id}/rerender")
def rerender_job(job_id: str, plan: EditPlan):
    job = manager.jobs.get(job_id)
    if not job or not job.clips:
        raise HTTPException(404)
    if job.status == "running":
        raise HTTPException(409, "job is running")
    job.plan = plan
    manager.submit_rerender(job, plan)
    return job.to_dict()


@app.get("/api/jobs/{job_id}/video")
def job_video(job_id: str):
    job = manager.jobs.get(job_id)
    if not job or not job.output or not Path(job.output).exists():
        raise HTTPException(404)
    return FileResponse(job.output, media_type="video/mp4", filename=Path(job.output).name)


@app.get("/api/health")
def health():
    return JSONResponse({"ok": True})
