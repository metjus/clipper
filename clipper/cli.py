"""Command line entry point: ``clipper web`` / ``clipper run <urls>`` / ``clipper settings``."""
from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser

from . import __version__
from .config import get_settings


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="clipper", description="AI clipper: Twitch clips -> TikTok video")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    w = sub.add_parser("web", help="start the local web UI (default)")
    w.add_argument("--port", type=int, default=8787)
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--no-browser", action="store_true")

    r = sub.add_parser("run", help="render a video from the command line")
    r.add_argument("urls", nargs="+", help="clip URLs or local video files")
    r.add_argument("--target", type=float, default=45, help="target length in seconds")
    r.add_argument("--layout", default="auto", choices=["auto", "split", "blur", "fill"])
    r.add_argument("--language", default=None, help="force transcription language (e.g. en, sk)")
    r.add_argument("--style", default="", help="free-text style hint for the AI director")
    r.add_argument("--font", default="LuckiestGuy", choices=["LuckiestGuy", "Anton", "Bangers"])
    r.add_argument("--name", default="", help="output file name (without .mp4)")
    r.add_argument("--preset", default="pop", choices=["pop", "karaoke", "bounce", "minimal"], help="caption animation preset")
    r.add_argument("--look", default="punchy", choices=["none", "punchy", "warm", "cold", "vintage"], help="colour grade")
    r.add_argument("--music", default="", help="'random' or a file name from ~/.clipper/music")
    r.add_argument("--no-progress-bar", action="store_true")
    r.add_argument("--no-jump-cuts", action="store_true", help="keep silent pauses")
    r.add_argument("--no-portrait", action="store_true", help="ignore Twitch's vertical clip version")

    d = sub.add_parser("discover", help="list a channel's top clips")
    d.add_argument("channel", help="Twitch/Kick channel name or URL")
    d.add_argument("--range", default="7d", choices=["24h", "7d", "30d", "all"])
    d.add_argument("--limit", type=int, default=20)

    st = sub.add_parser("settings", help="show or change settings")
    st.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")

    sub.add_parser("doctor", help="check ffmpeg, whisper and the API key")

    args = p.parse_args(argv)
    if args.cmd in (None, "web"):
        return _web(args)
    if args.cmd == "run":
        return _run(args)
    if args.cmd == "settings":
        return _settings(args)
    if args.cmd == "doctor":
        return _doctor()
    if args.cmd == "discover":
        from .downloader import discover

        for c in discover(args.channel, "twitch", args.range, args.limit, print):
            print(f"{c['views']:>8} views  {c['duration']:>5.0f}s  {c['title'][:50]:50}  {c['url']}")
        return 0
    return 0


def _web(args) -> int:
    import uvicorn

    from .web.app import app

    port = getattr(args, "port", 8787)
    host = getattr(args, "host", "127.0.0.1")
    url = f"http://{host}:{port}"
    if not getattr(args, "no_browser", False):
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()
    print(f"AI clipper running at {url}  (Ctrl+C to stop)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def _run(args) -> int:
    from .pipeline import Job, JobOptions, run_job

    opts = JobOptions(target_length=args.target, layout=args.layout, language=args.language,
                      style_hint=args.style, font=args.font, output_name=args.name, preset=args.preset,
                      look=args.look, music=args.music, progress_bar=not args.no_progress_bar,
                      remove_silence=not args.no_jump_cuts, prefer_portrait=not args.no_portrait)
    job = Job(id=time.strftime("cli-%Y%m%d-%H%M%S"), urls=args.urls, options=opts)
    printed = 0

    def pump():
        nonlocal printed
        while job.status in ("queued", "running"):
            while printed < len(job.log):
                print(job.log[printed]); printed += 1
            time.sleep(0.3)

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    run_job(job)
    t.join(timeout=1)
    while printed < len(job.log):
        print(job.log[printed]); printed += 1
    if job.status != "done":
        print("FAILED:", job.error, file=sys.stderr)
        return 1
    print("\nOutput:", job.output)
    if job.plan:
        print("Caption:", job.plan.tiktok_caption)
        print("Hashtags:", " ".join("#" + h for h in job.plan.hashtags))
    return 0


def _settings(args) -> int:
    s = get_settings()
    for kv in args.set:
        k, _, v = kv.partition("=")
        if not hasattr(s, k):
            print("unknown setting", k, file=sys.stderr)
            return 2
        setattr(s, k, v)
    if args.set:
        s.save()
        print("saved to", __import__("clipper.config", fromlist=["CONFIG_FILE"]).CONFIG_FILE)
    for k, v in s.public().items():
        print(f"{k:22} {v}")
    return 0


def _doctor() -> int:
    s = get_settings()
    ok = True
    print("ffmpeg:   ", s.ffmpeg, "OK" if s.ffmpeg_ok() else "NOT FOUND")
    ok &= s.ffmpeg_ok()
    if s.ffmpeg_ok():
        filt = s.ffmpeg_has_filters("ass", "zoompan", "drawbox", "gblur", "loudnorm", "dynaudnorm")
        for k, v in filt.items():
            print(f"  filter {k:12}", "OK" if v else "MISSING")
            ok &= v
        print("encoder:  ", s.pick_encoder())
    print("whisper:  ", s.whisper_model, "/", s.whisper_device)
    try:
        import faster_whisper  # noqa: F401
        print("  faster-whisper import OK")
    except Exception as e:  # noqa: BLE001
        print("  faster-whisper import FAILED:", e); ok = False
    print("claude:   ", "API key set" if s.anthropic_api_key else "no API key (heuristic planner will be used)")
    print("output:   ", s.output_dir)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
