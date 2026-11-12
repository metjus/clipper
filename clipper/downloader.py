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


def download(url: str, dest_dir: Path, log: Optional[Log] = None, prefer_portrait: bool = True) -> ClipInfo:
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

    fmt = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*+ba/b"
    if prefer_portrait:
        # Twitch publishes a creator-made vertical cut for some clips ("portrait" formats): use it when present
        fmt = "b[format_id^=portrait][height<=1080]/" + fmt
    opts = {
        "outtmpl": out_tmpl,
        "format": fmt,
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
        thumbnail=info.get("thumbnail") or "",
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


# --------------------------------------------------------------------------
# Discovery: top clips of a channel
# --------------------------------------------------------------------------
_RANGES = {"24h": "24hr", "7d": "7d", "30d": "30d", "all": "all"}


def discover(channel: str, platform: str = "twitch", range_: str = "7d", limit: int = 20,
             log: Optional[Log] = None) -> list[dict]:
    """List a channel's top clips (title, url, views, duration, thumbnail) without downloading."""
    import yt_dlp

    channel = channel.strip().rstrip("/")
    if "kick.com/" in channel:
        platform = "kick"
    if "twitch.tv/" in channel or "kick.com/" in channel:
        channel = channel.split("/")[-1].split("?")[0]
    channel = channel.lstrip("@")
    if platform == "kick":
        url = f"https://kick.com/{channel}/clips"
    else:
        url = f"https://www.twitch.tv/{channel}/clips?filter=clips&range={_RANGES.get(range_, '7d')}"
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": max(1, min(limit, 100)),
            "logger": _YdlLogger(log or (lambda m: None), "discover")}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    out = []
    for e in (info or {}).get("entries") or []:
        if not e or not e.get("url"):
            continue
        out.append({
            "url": e["url"], "title": e.get("title") or "", "views": e.get("view_count") or 0,
            "duration": e.get("duration") or 0, "thumbnail": e.get("thumbnail") or "",
            "language": e.get("language") or "", "created": e.get("timestamp") or 0,
        })
    out.sort(key=lambda c: -c["views"])
    return out[:limit]
