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
from .models import Box, CaptionStyle, ClipInfo, EditPlan
from .render import Cancelled, render
from .transcribe import transcribe


@dataclass
class JobOptions:
    target_length: float = 45.0
    layout: str = "auto"              # auto / split / blur / fill
    language: Optional[str] = None    # force whisper language, e.g. "sk"
    style_hint: str = ""
    font: str = "LuckiestGuy"
    preset: str = "pop"               # pop / karaoke / bounce / minimal
    highlight_color: str = "#FFE600"
    accent_color: str = "#FF3B3B"
    caption_size: int = 96
    words_per_line: int = 3
    look: str = "punchy"
    progress_bar: bool = True
    remove_silence: bool = True
    prefer_portrait: bool = True
    music: str = ""                   # "" = none, "random", or a file name from the music folder
    music_volume: float = 0.12
    output_name: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "JobOptions":
        o = cls()
        for k, v in (d or {}).items():
            if not hasattr(o, k) or v is None:
                continue
            cur = getattr(o, k)
            if isinstance(cur, bool):
                setattr(o, k, bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes", "on"))
            elif cur is None:
                setattr(o, k, v or None)
            elif v == "" and not isinstance(cur, str):
                continue
            else:
                setattr(o, k, type(cur)(v))
        return o

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Job:
    id: str
    urls: List[str]
    options: JobOptions
    status: str = "queued"          # queued / running / done / error / cancelled
    stage: str = ""
    progress: float = 0.0
    log: List[str] = field(default_factory=list)
    error: str = ""
    clips: List[ClipInfo] = field(default_factory=list)
    plan: Optional[EditPlan] = None
    output: Optional[Path] = None
    created: float = field(default_factory=time.time)
    cancelled: bool = False

    def say(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.append(f"{stamp} {msg}")

    def set(self, stage: str, progress: float) -> None:
        self.stage, self.progress = stage, max(self.progress if stage == self.stage else 0.0, progress)

    def check_cancel(self) -> None:
        if self.cancelled:
            raise Cancelled("cancelled by user")

    def to_dict(self, full: bool = True) -> dict:
        d = {
            "id": self.id, "urls": self.urls, "status": self.status, "stage": self.stage,
            "progress": round(self.progress, 3), "error": self.error,
            "output": str(self.output) if self.output else None,
            "cover": str(self.output.with_suffix(".jpg")) if self.output and self.output.with_suffix(".jpg").exists() else None,
            "created": self.created, "options": self.options.to_dict(),
            "clips": [{"id": c.id, "url": c.url, "title": c.title, "broadcaster": c.broadcaster, "game": c.game,
                       "duration": c.duration, "views": c.view_count, "language": c.transcript.language,
                       "words": len(c.transcript.words), "portrait": c.portrait, "thumbnail": c.thumbnail,
                       "width": c.width, "height": c.height, "peaks": [p.model_dump() for p in c.peaks],
                       "facecam": c.facecam.model_dump() if c.facecam else None,
                       "text": c.transcript.text[:300]}
                      for c in self.clips],
        }
        if full:
            d["log"] = self.log[-300:]
            d["plan"] = self.plan.model_dump() if self.plan else None
        return d

    @property
    def work(self) -> Path:
        p = get_settings().work_path / self.id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def persist(self) -> None:
        try:
            (self.work / "job.json").write_text(json.dumps(self.to_dict(full=True), indent=1, ensure_ascii=False),
                                                encoding="utf-8")
            (self.work / "clips.json").write_text(json.dumps([c.model_dump() for c in self.clips], indent=1),
                                                  encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    @classmethod
    def restore(cls, work: Path) -> Optional["Job"]:
        try:
            d = json.loads((work / "job.json").read_text(encoding="utf-8"))
            clips = [ClipInfo(**c) for c in json.loads((work / "clips.json").read_text(encoding="utf-8"))]
        except Exception:  # noqa: BLE001
            return None
        job = cls(id=d["id"], urls=d["urls"], options=JobOptions.from_dict(d.get("options", {})),
                  status=d["status"] if d["status"] in ("done", "error", "cancelled") else "error",
                  stage=d.get("stage", ""), progress=1.0, log=d.get("log", []), error=d.get("error", ""),
                  clips=clips, plan=EditPlan(**d["plan"]) if d.get("plan") else None,
                  output=Path(d["output"]) if d.get("output") else None, created=d.get("created", 0))
        if job.output and not job.output.exists():
            job.output = None
        return job


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
            job.check_cancel()
            job.set("download", i / n)
            info = download(url, s.work_path / "downloads", job.say, prefer_portrait=job.options.prefer_portrait)
            meta = probe(info.path)
            info.duration = meta["duration"] or info.duration
            info.width, info.height, info.fps = meta["width"], meta["height"], meta["fps"]
            info.portrait = bool(info.height and info.width and info.height > info.width)
            clips.append(info)
            job.say(f"[{info.id}] {info.title or 'untitled'} - {info.duration:.1f}s "
                    f"{info.width}x{info.height}" + (" (portrait)" if info.portrait else "")
                    + (f", {info.view_count} views" if info.view_count else ""))
        job.set("download", 1.0)
        job.clips = clips

        for i, c in enumerate(clips):
            job.check_cancel()
            job.set("transcribe", i / n)
            job.say(f"[{c.id}] transcribing with whisper '{s.whisper_model}'")
            try:
                c.transcript = transcribe(c.path, language=job.options.language,
                                          log=lambda m, c=c: job.say(f"[{c.id}] {m}"))
            except Exception as exc:  # noqa: BLE001 - a broken whisper must not kill the render
                job.say(f"[{c.id}] transcription failed ({type(exc).__name__}: {exc}); continuing without captions")
            c.peaks = loudness_peaks(c.path)
            job.say(f"[{c.id}] {len(c.peaks)} loud peaks")
            if job.options.layout in ("auto", "split") and not c.portrait:
                c.facecam = detect_facecam(c.path)
                job.say(f"[{c.id}] facecam " + (f"found at {c.facecam.x:.2f},{c.facecam.y:.2f} "
                                                 f"{c.facecam.w:.2f}x{c.facecam.h:.2f}" if c.facecam else "not found -> blur layout"))
        job.set("transcribe", 1.0)
        job.persist()

        # 2. plan
        job.check_cancel()
        job.set("plan", 0.1)
        plan = plan_edit(clips, job.options.target_length, job.options.style_hint, job.say)
        apply_options(plan, job.options)
        job.plan = plan
        (job.work / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        job.set("plan", 1.0)
        job.persist()

        # 3. render
        _render(job)
        job.status = "done"
    except Cancelled:
        job.status = "cancelled"
        job.say("cancelled")
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.say("ERROR " + job.error)
        job.say(traceback.format_exc().splitlines()[-1])
    job.persist()


def rerender(job: Job, plan: EditPlan) -> None:
    """Render again with an edited plan (no download / transcription)."""
    job.status = "running"
    job.error = ""
    job.cancelled = False
    job.plan = plan
    try:
        (job.work / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        _render(job)
        job.status = "done"
    except Cancelled:
        job.status = "cancelled"
        job.say("cancelled")
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.say("ERROR " + job.error)
    job.persist()


def _render(job: Job) -> None:
    s = get_settings()
    assert job.plan is not None
    job.set("render", 0.0)
    name = job.options.output_name or f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(job.plan.title)}"
    out = s.out_path / f"{name}.mp4"
    render(job.plan, job.clips, out, job.work,
           log=job.say, progress=lambda f, m: job.set("render", f), cancel=lambda: job.cancelled)
    sidecar = {
        "title": job.plan.title, "hook": job.plan.hook, "caption": job.plan.tiktok_caption,
        "hashtags": job.plan.hashtags, "post_text": job.plan.tiktok_caption + "\n\n" +
        " ".join("#" + h for h in job.plan.hashtags), "clips": job.urls,
    }
    out.with_suffix(".json").write_text(json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8")
    job.output = out
    job.say(f"done -> {out}")


def apply_options(plan: EditPlan, o: JobOptions) -> None:
    cs: CaptionStyle = plan.caption_style
    if o.font in ("LuckiestGuy", "Anton", "Bangers"):
        cs.font = o.font  # type: ignore[assignment]
    if o.preset in ("pop", "karaoke", "bounce", "minimal"):
        cs.preset = o.preset  # type: ignore[assignment]
    cs.highlight_color = o.highlight_color or cs.highlight_color
    cs.accent_color = o.accent_color or cs.accent_color
    cs.size = int(o.caption_size or cs.size)
    cs.words_per_line = int(o.words_per_line or cs.words_per_line)
    if o.look in ("none", "punchy", "warm", "cold", "vintage"):
        plan.look = o.look  # type: ignore[assignment]
    plan.progress_bar = o.progress_bar
    plan.background_music = bool(o.music)
    plan.music_file = "" if o.music in ("", "random") else o.music
    plan.music_volume = o.music_volume
    for e in plan.clips:
        if o.layout != "auto":
            e.layout = o.layout  # type: ignore[assignment]
        if not o.remove_silence:
            e.remove_silence = False


# --------------------------------------------------------------------------
# In-process job manager (used by the web UI)
# --------------------------------------------------------------------------
class JobManager:
    def __init__(self) -> None:
        self.jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: List[str] = []
        self._load_history()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def _load_history(self) -> None:
        root = get_settings().work_path
        for d in sorted(root.iterdir() if root.exists() else []):
            if d.is_dir() and (d / "job.json").exists():
                job = Job.restore(d)
                if job:
                    self.jobs[job.id] = job

    def submit(self, urls: List[str], options: JobOptions) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], urls=urls, options=options)
        with self._lock:
            self.jobs[job.id] = job
            self._queue.append(job.id)
        job.say(f"queued {len(urls)} clip(s)")
        return job

    def submit_rerender(self, job: Job, plan: EditPlan) -> None:
        job.status = "queued"
        job.plan = plan
        job.say("re-render queued")
        with self._lock:
            self._queue.append(f"rerender:{job.id}")

    def cancel(self, job: Job) -> None:
        job.cancelled = True
        with self._lock:
            self._queue = [q for q in self._queue if not q.endswith(job.id)]
        if job.status == "queued":
            job.status = "cancelled"
            job.say("cancelled")

    def set_facecam(self, job: Job, clip_id: str, box: Optional[Box]) -> None:
        for c in job.clips:
            if c.id == clip_id:
                c.facecam = box
        job.persist()

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
