"""Fast unit tests that need no network, no whisper model and no API key."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clipper import captions, sfx
from clipper.director import heuristic_plan
from clipper.media import loudness_peaks, probe
from clipper.models import Box, CaptionStyle, ClipInfo, EditPlan, Peak, TextOverlay, Transcript, Word
from clipper.render import _shake_expr, _zoom_expr, render

FFMPEG = __import__("shutil").which("ffmpeg")
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


def _words(text: str, start: float = 0.5, step: float = 0.4):
    return [Word(text=w, start=round(start + i * step, 2), end=round(start + i * step + 0.3, 2))
            for i, w in enumerate(text.split())]


def test_chunking_and_ass():
    words = _words("what the hell was that bro. no way he did it")
    chunks = captions.chunk_words(words, max_words=3)
    assert all(len(c) <= 3 for c in chunks)
    assert chunks[0][0].text == "what"
    doc = captions.build_ass(words, CaptionStyle(), emphasis=["hell"],
                             overlays=[TextOverlay(text="NO WAY", start=0, end=1)])
    assert "[Events]" in doc and "Dialogue:" in doc
    assert doc.count("Dialogue:") == len(words) + 1
    assert "\\pos(540," in doc


def test_sfx_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(sfx, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(sfx, "USER_SFX_DIR", tmp_path / "nope")
    for name in sfx.GENERATORS:
        p = sfx.sfx_path(name)
        assert p.exists() and p.stat().st_size > 1000
        assert 0.05 < sfx.sfx_duration(name) < 3


def test_expressions():
    from clipper.models import ClipEdit, ShakeCue, ZoomCue
    e = ClipEdit(clip_id="x", zooms=[ZoomCue(t=1.0, strength=1.3)], shakes=[ShakeCue(t=2.0)])
    assert "0.300*" in _zoom_expr(e)
    assert "random(11)" in _shake_expr(e, 11)


@pytest.fixture(scope="module")
def synthetic_clip(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("clips")
    p = d / "a.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=4",
         "-f", "lavfi", "-i", "sine=f=440:d=4,volume='if(between(t,2,2.3),1,0.05)':eval=frame",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(p)],
        check=True,
    )
    return p


def test_peaks_and_render(synthetic_clip, tmp_path):
    m = probe(synthetic_clip)
    peaks = loudness_peaks(synthetic_clip)
    assert peaks and abs(peaks[0].t - 2.1) < 0.4
    clip = ClipInfo(id="a", url="x", path=str(synthetic_clip), title="A", duration=m["duration"],
                    width=m["width"], height=m["height"], fps=m["fps"],
                    transcript=Transcript(language="en", words=_words("hello chat this is a test")),
                    peaks=peaks, facecam=Box(x=0, y=0, w=0.3, h=0.3))
    plan = heuristic_plan([clip, clip.model_copy(update={"id": "b", "facecam": None})], 10)
    assert isinstance(plan, EditPlan) and len(plan.clips) == 2
    out = render(plan, [clip, clip.model_copy(update={"id": "b", "facecam": None})], tmp_path / "out.mp4", tmp_path / "work")
    info = probe(out)
    assert info["width"] == 1080 and info["height"] == 1920 and info["has_audio"]
    assert info["duration"] > 2
