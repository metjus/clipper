"""Download clips with yt-dlp (Twitch clips, Kick, YouTube, plain URLs, local files)."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Callable, Optional

from .models import ClipInfo

Log = Callable[[str], None]

_TWITCH_CLIP_RE = re.compile(
    r"(?:https?://)?(?:(?:www|m)\.)?(?:clips\.twitch\.tv/|twitch\.tv/[^/]+/clip/)([A-Za-z0-9_-]+)"
)


def clip_id_for(url: str) -> str:
    m = _TWITCH_CLIP_RE.search(url)
    if m:
        return "tw_" + m.group(1)[:40]
    return "c_" + hashlib.sha1(url.encode()).hexdigest()[:12]


def normalise_url(url: str) -> str:
    url = url.strip()
    m = _TWITCH_CLIP_RE.search(url)
    if m:
        return f"https://clips.twitch.tv/{m.group(1)}"
    return url


def download(url: str, dest_dir: Path, log: Optional[Log] = None) -> ClipInfo:
    """Download ``url`` into ``dest_dir`` and return basic metadata.

    Local file paths are accepted too (handy for testing and for clips the
    user already has on disk).
    """
    log = log or (lambda m: None)
    dest_dir.mkdir(parents=True, exist_ok=True)
    cid = clip_id_for(url)

    local = Path(url.strip().strip('"'))
    if local.is_file():
        log(f"[{cid}] using local file {local.name}")
        return ClipInfo(id=cid, url=str(local), path=str(local), title=local.stem)

    url = normalise_url(url)
    out_tmpl = str(dest_dir / f"{cid}.%(ext)s")
    existing = list(dest_dir.glob(f"{cid}.*"))
    existing = [p for p in existing if p.suffix in {".mp4", ".mkv", ".webm", ".mov"}]

    import yt_dlp  # imported lazily: slow import

    opts = {
        "outtmpl": out_tmpl,
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "logger": _YdlLogger(log, cid),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        if existing:
            log(f"[{cid}] already downloaded, fetching metadata only")
            info = ydl.extract_info(url, download=False)
            path = existing[0]
        else:
            log(f"[{cid}] downloading {url}")
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
            if not path.exists():
                cands = list(dest_dir.glob(f"{cid}.*"))
                if not cands:
                    raise RuntimeError(f"yt-dlp finished but no file found for {url}")
                path = cands[0]

    return ClipInfo(
        id=cid,
        url=url,
        path=str(path),
        title=(info.get("title") or "").strip(),
        broadcaster=(info.get("creator") or info.get("channel") or info.get("uploader") or "").strip(),
        game=(info.get("game") or info.get("category") or "").strip(),
        view_count=int(info.get("view_count") or 0),
        duration=float(info.get("duration") or 0.0),
    )


class _YdlLogger:
    def __init__(self, log: Log, cid: str):
        self.log, self.cid = log, cid

    def debug(self, msg):  # yt-dlp sends info lines here too
        if msg.startswith("[download]") and "%" in msg:
            return
    def info(self, msg):
        pass
    def warning(self, msg):
        self.log(f"[{self.cid}] warning: {msg}")
    def error(self, msg):
        self.log(f"[{self.cid}] error: {msg}")
