"""Job orchestration: URLs -> analysed clips -> plan -> rendered video."""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .config import get_settings
from .director import plan_edit
from .downloader import download
from .media import detect_facecam, loudness_peaks, probe
from .models import CaptionStyle, ClipInfo, EditPlan
from .render import render
from .transcribe import transcribe


@dataclass
class JobOptions:
    target_length: float = 45.0
    layout: str = "auto"              # auto / split / blur / fill
    language: Optional[str] = None    # force whisper language, e.g. "sk"
    style_hint: str = ""
    font: str = "LuckiestGuy"
    highlight_color: str = "#FFE600"
    accent_color: str = "#FF3B3B"
    caption_size: int = 96
    words_per_line: int = 3
    output_name: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "JobOptions":
        o = cls()
        for k, v in (d or {}).items():
            if hasattr(o, k) and v not in (None, ""):
                setattr(o, k, type(getattr(o, k))(v) if getattr(o, k) is not None else v)
        return o


@dataclass
class Job:
    id: str
    urls: List[str]
    options: JobOptions
    status: str = "queued"          # queued / running / done / error
    stage: str = ""
    progress: float = 0.0
    log: List[str] = field(default_factory=list)
    error: str = ""
    clips: List[ClipInfo] = field(default_factory=list)
    plan: Optional[EditPlan] = None
    output: Optional[Path] = None
    created: float = field(default_factory=time.time)

    def say(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.append(f"{stamp} {msg}")

    def set(self, stage: str, progress: float) -> None:
        self.stage, self.progress = stage, max(self.progress if stage == self.stage else 0.0, progress)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "urls": self.urls, "status": self.status, "stage": self.stage,
            "progress": round(self.progress, 3), "log": self.log[-200:], "error": self.error,
            "output": str(self.output) if self.output else None,
            "plan": self.plan.model_dump() if self.plan else None,
            "clips": [{"id": c.id, "title": c.title, "broadcaster": c.broadcaster, "game": c.game,
                       "duration": c.duration, "views": c.view_count, "language": c.transcript.language,
                       "words": len(c.transcript.words), "facecam": c.facecam.model_dump() if c.facecam else None}
                      for c in self.clips],
            "created": self.created,
        }

    @property
    def work(self) -> Path:
        p = get_settings().work_path / self.id
        p.mkdir(parents=True, exist_ok=True)
        return p


def _slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.U).strip().lower()
    return re.sub(r"[\s_-]+", "-", text)[:50] or "video"


def run_job(job: Job) -> None:
    s = get_settings()
    job.status = "running"
    try:
        n = len(job.urls)
        # 1. download + analyse
        clips: List[ClipInfo] = []
        for i, url in enumerate(job.urls):
            job.set("download", i / n)
            info = download(url, s.work_path / "downloads", job.say)
            meta = probe(info.path)
            info.duration = meta["duration"] or info.duration
            info.width, info.height, info.fps = meta["width"], meta["height"], meta["fps"]
            clips.append(info)
            job.say(f"[{info.id}] {info.title or 'untitled'} - {info.duration:.1f}s "
                    f"{info.width}x{info.height}" + (f", {info.view_count} views" if info.view_count else ""))
        job.set("download", 1.0)

        for i, c in enumerate(clips):
            job.set("transcribe", i / n)
            job.say(f"[{c.id}] transcribing with whisper '{s.whisper_model}'")
            try:
                c.transcript = transcribe(c.path, language=job.options.language,
                                          log=lambda m, c=c: job.say(f"[{c.id}] {m}"))
            except Exception as exc:  # noqa: BLE001 - a broken whisper must not kill the render
                job.say(f"[{c.id}] transcription failed ({type(exc).__name__}: {exc}); continuing without captions")
            c.peaks = loudness_peaks(c.path)
            job.say(f"[{c.id}] {len(c.peaks)} loud peaks")
            if job.options.layout in ("auto", "split"):
                c.facecam = detect_facecam(c.path)
                job.say(f"[{c.id}] facecam " + (f"found at {c.facecam.x:.2f},{c.facecam.y:.2f} "
                                                 f"{c.facecam.w:.2f}x{c.facecam.h:.2f}" if c.facecam else "not found -> blur layout"))
        job.set("transcribe", 1.0)
        job.clips = clips

        # 2. plan
        job.set("plan", 0.1)
        plan = plan_edit(clips, job.options.target_length, job.options.style_hint, job.say)
        _apply_options(plan, job.options)
        job.plan = plan
        (job.work / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        (job.work / "clips.json").write_text(json.dumps([c.model_dump() for c in clips], indent=2), encoding="utf-8")
        job.set("plan", 1.0)

        # 3. render
        _render(job)
        job.status = "done"
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.say("ERROR " + job.error)
        job.say(traceback.format_exc().splitlines()[-1])


def rerender(job: Job, plan: EditPlan) -> None:
    """Render again with an edited plan (no download / transcription)."""
    job.status = "running"
    job.error = ""
    job.plan = plan
    try:
        (job.work / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        _render(job)
        job.status = "done"
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.say("ERROR " + job.error)


def _render(job: Job) -> None:
    s = get_settings()
    assert job.plan is not None
    job.set("render", 0.0)
    name = job.options.output_name or f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(job.plan.title)}"
    out = s.out_path / f"{name}.mp4"
    render(job.plan, job.clips, out, job.work,
           log=job.say, progress=lambda f, m: job.set("render", f))
    sidecar = {
        "title": job.plan.title, "hook": job.plan.hook, "caption": job.plan.tiktok_caption,
        "hashtags": job.plan.hashtags, "post_text": job.plan.tiktok_caption + "\n\n" +
        " ".join("#" + h for h in job.plan.hashtags), "clips": job.urls,
    }
    out.with_suffix(".json").write_text(json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8")
    job.output = out
    job.say(f"done -> {out}")


def _apply_options(plan: EditPlan, o: JobOptions) -> None:
    cs: CaptionStyle = plan.caption_style
    cs.font = o.font if o.font in ("LuckiestGuy", "Anton", "Bangers") else cs.font
    cs.highlight_color = o.highlight_color or cs.highlight_color
    cs.accent_color = o.accent_color or cs.accent_color
    cs.size = int(o.caption_size or cs.size)
    cs.words_per_line = int(o.words_per_line or cs.words_per_line)
    if o.layout != "auto":
        for e in plan.clips:
            e.layout = o.layout  # type: ignore[assignment]


# --------------------------------------------------------------------------
# In-process job manager (used by the web UI)
# --------------------------------------------------------------------------
class JobManager:
    def __init__(self) -> None:
        self.jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: List[str] = []
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def submit(self, urls: List[str], options: JobOptions) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], urls=urls, options=options)
        with self._lock:
            self.jobs[job.id] = job
            self._queue.append(job.id)
        job.say(f"queued {len(urls)} clip(s)")
        return job

    def submit_rerender(self, job: Job, plan: EditPlan) -> None:
        job.status = "queued"
        job.say("re-render queued")
        with self._lock:
            self._queue.append(f"rerender:{job.id}")

    def _loop(self) -> None:
        while True:
            jid = None
            with self._lock:
                if self._queue:
                    jid = self._queue.pop(0)
            if jid is None:
                time.sleep(0.3)
                continue
            if jid.startswith("rerender:"):
                job = self.jobs[jid.split(":", 1)[1]]
                assert job.plan is not None
                rerender(job, job.plan)
            else:
                run_job(self.jobs[jid])
