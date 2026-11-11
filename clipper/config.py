"""Runtime configuration.

Settings are read from (highest priority first):
  1. environment variables (``ANTHROPIC_API_KEY``, ``CLIPPER_*``)
  2. a ``.env`` file in the current directory
  3. ``~/.clipper/config.json`` (written by the web UI settings page)
  4. built-in defaults
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

APP_DIR = Path(os.environ.get("CLIPPER_HOME", Path.home() / ".clipper"))
CONFIG_FILE = APP_DIR / "config.json"
PACKAGE_DIR = Path(__file__).resolve().parent
FONT_DIR = PACKAGE_DIR / "assets" / "fonts"


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Settings:
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    whisper_model: str = "small"          # tiny / base / small / medium / large-v3
    whisper_device: str = "auto"          # auto / cpu / cuda
    whisper_compute_type: str = "auto"    # auto / int8 / float16 / float32
    output_dir: str = str(APP_DIR / "output")
    work_dir: str = str(APP_DIR / "work")
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    video_encoder: str = "auto"           # auto / libx264 / h264_nvenc
    extra: dict = field(default_factory=dict)

    # ---- persistence -------------------------------------------------
    @classmethod
    def load(cls) -> "Settings":
        _load_dotenv()
        data: dict = {}
        if CONFIG_FILE.is_file():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        s = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        env = os.environ
        s.anthropic_api_key = env.get("ANTHROPIC_API_KEY", s.anthropic_api_key)
        s.claude_model = env.get("CLIPPER_CLAUDE_MODEL", s.claude_model)
        s.whisper_model = env.get("CLIPPER_WHISPER_MODEL", s.whisper_model)
        s.whisper_device = env.get("CLIPPER_WHISPER_DEVICE", s.whisper_device)
        s.output_dir = env.get("CLIPPER_OUTPUT_DIR", s.output_dir)
        s.work_dir = env.get("CLIPPER_WORK_DIR", s.work_dir)
        s.ffmpeg = env.get("CLIPPER_FFMPEG", s.ffmpeg)
        s.ffprobe = env.get("CLIPPER_FFPROBE", s.ffprobe)
        s.resolve_ffmpeg()
        return s

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    def public(self) -> dict:
        d = asdict(self)
        d["anthropic_api_key"] = ("set" if self.anthropic_api_key else "")
        d["ffmpeg_ok"] = self.ffmpeg_ok()
        return d

    # ---- helpers -----------------------------------------------------
    def resolve_ffmpeg(self) -> None:
        """Find ffmpeg: configured path -> PATH -> imageio-ffmpeg bundled binary."""
        if shutil.which(self.ffmpeg):
            return
        try:
            import imageio_ffmpeg  # type: ignore

            exe = imageio_ffmpeg.get_ffmpeg_exe()
            self.ffmpeg = exe
            probe = Path(exe).with_name(Path(exe).name.replace("ffmpeg", "ffprobe"))
            if probe.exists():
                self.ffprobe = str(probe)
        except Exception:
            pass

    def ffmpeg_ok(self) -> bool:
        return shutil.which(self.ffmpeg) is not None or Path(self.ffmpeg).is_file()

    def ffmpeg_has_filters(self, *names: str) -> dict[str, bool]:
        try:
            out = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-filters"],
                capture_output=True, text=True, timeout=20,
            ).stdout
        except Exception:
            return {n: False for n in names}
        present = {line.split()[1] for line in out.splitlines() if line.startswith(" ") and len(line.split()) > 1}
        return {n: n in present for n in names}

    def pick_encoder(self) -> str:
        if self.video_encoder != "auto":
            return self.video_encoder
        try:
            out = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=20,
            ).stdout
        except Exception:
            return "libx264"
        if "h264_nvenc" in out and _nvenc_works(self.ffmpeg):
            return "h264_nvenc"
        return "libx264"

    @property
    def out_path(self) -> Path:
        p = Path(self.output_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def work_path(self) -> Path:
        p = Path(self.work_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


def _nvenc_works(ffmpeg: str) -> bool:
    """NVENC is listed even without a GPU; do a 1-frame smoke test."""
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "color=c=black:s=256x256:d=0.1", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


_settings: Optional[Settings] = None


def get_settings(reload: bool = False) -> Settings:
    global _settings
    if _settings is None or reload:
        _settings = Settings.load()
    return _settings
