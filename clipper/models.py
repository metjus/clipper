"""Data models shared by the pipeline stages.

Two families live here:

* **Analysis models** (``Word``, ``Transcript``, ``ClipInfo``) describe what we
  learned about each source clip.
* **EditPlan** is what the AI director (or the heuristic fallback) produces and
  what the renderer consumes.  It is a Pydantic model so Claude's structured
  output is validated against it.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
class Word(BaseModel):
    text: str
    start: float
    end: float
    prob: float = 1.0


class Transcript(BaseModel):
    language: str = "en"
    language_prob: float = 0.0
    words: List[Word] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


class Peak(BaseModel):
    """A loud moment in the audio (candidate for a zoom / shake / SFX)."""
    t: float
    loudness: float  # 0..1 relative to the clip's max


class Box(BaseModel):
    """Normalised rectangle (fractions of width / height)."""
    x: float
    y: float
    w: float
    h: float


class ClipInfo(BaseModel):
    id: str
    url: str
    path: str
    title: str = ""
    broadcaster: str = ""
    game: str = ""
    view_count: int = 0
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 30.0
    thumbnail: str = ""
    portrait: bool = Field(False, description="Source is already vertical (e.g. Twitch's portrait clip asset)")
    transcript: Transcript = Field(default_factory=Transcript)
    peaks: List[Peak] = Field(default_factory=list)
    facecam: Optional[Box] = None
    silences: List[List[float]] = Field(default_factory=list, description="[start, end] of silent stretches")


# --------------------------------------------------------------------------
# Edit plan (output of the director)
# --------------------------------------------------------------------------
Layout = Literal["auto", "split", "blur", "fill"]
CaptionPreset = Literal["pop", "karaoke", "bounce", "minimal"]
Look = Literal["none", "punchy", "warm", "cold", "vintage"]
SfxName = Literal["boom", "whoosh", "ding", "pop", "riser", "record_scratch", "bass_drop"]


class ZoomCue(BaseModel):
    t: float = Field(description="Seconds from the start of the (trimmed) clip")
    strength: float = Field(1.15, ge=1.02, le=1.6, description="Zoom factor at the peak of the punch-in")
    hold: float = Field(0.5, ge=0.1, le=4.0, description="How long to hold the zoom before easing back")


class ShakeCue(BaseModel):
    t: float
    duration: float = Field(0.4, ge=0.1, le=2.0)
    intensity: float = Field(1.0, ge=0.2, le=2.0)


class SfxCue(BaseModel):
    t: float
    name: SfxName
    gain: float = Field(1.0, ge=0.1, le=2.0)


class FlashCue(BaseModel):
    t: float
    duration: float = Field(0.08, ge=0.03, le=0.3)


class TextOverlay(BaseModel):
    """Big text burned over the video (hook at the start, callouts, etc.)."""
    text: str
    start: float
    end: float
    position: Literal["top", "center", "bottom"] = "top"
    color: str = "#FFFFFF"


class ClipEdit(BaseModel):
    clip_id: str
    trim_start: float = Field(0.0, ge=0.0, description="Seconds to cut from the start of the clip")
    trim_end: float = Field(0.0, ge=0.0, description="Seconds to cut from the end of the clip")
    layout: Layout = "auto"
    speed: float = Field(1.0, ge=0.5, le=2.0, description="Playback speed (1.0 = normal)")
    emphasis_words: List[str] = Field(default_factory=list, description="Words to render in the accent colour")
    zooms: List[ZoomCue] = Field(default_factory=list)
    shakes: List[ShakeCue] = Field(default_factory=list)
    sfx: List[SfxCue] = Field(default_factory=list)
    flashes: List[FlashCue] = Field(default_factory=list)
    overlays: List[TextOverlay] = Field(default_factory=list)
    remove_silence: bool = Field(True, description="Jump-cut silent stretches longer than ~0.6 s inside the clip")
    reason: str = Field("", description="One sentence: why this clip is placed here / cut this way")


class CaptionStyle(BaseModel):
    font: Literal["LuckiestGuy", "Anton", "Bangers"] = "LuckiestGuy"
    size: int = Field(96, ge=40, le=160)
    primary_color: str = "#FFFFFF"
    highlight_color: str = "#FFE600"
    accent_color: str = "#FF3B3B"
    outline: int = Field(7, ge=0, le=16)
    uppercase: bool = True
    words_per_line: int = Field(3, ge=1, le=5)
    y_position: float = Field(0.68, ge=0.3, le=0.9, description="Vertical centre of the caption block as a fraction of height")
    pop: bool = True
    preset: CaptionPreset = Field("pop", description="pop: chunk pops in, spoken word highlighted. karaoke: spoken words stay coloured. bounce: each word bounces. minimal: calmer, no uppercase.")


class EditPlan(BaseModel):
    title: str = Field(description="Internal name for the output video")
    hook: str = Field(description="Short on-screen hook shown in the first ~2 seconds. No emoji. In the streamer's language.")
    tiktok_caption: str = Field(description="Caption text for the TikTok post, in the streamer's language, no hashtags")
    hashtags: List[str] = Field(default_factory=list, description="Without the # sign, 5-10 items")
    caption_style: CaptionStyle = Field(default_factory=CaptionStyle)
    clips: List[ClipEdit]
    transition_sfx: Optional[SfxName] = Field("whoosh", description="SFX played on every cut between clips")
    transition_flash: bool = True
    background_music: bool = Field(False, description="Mix a music bed from the user's music folder (auto-ducked under speech)")
    music_file: str = Field("", description="Specific music file name from the music folder, empty = random")
    music_volume: float = Field(0.12, ge=0.0, le=1.0)
    progress_bar: bool = Field(True, description="Thin progress bar at the bottom edge (retention trick)")
    look: Look = Field("punchy", description="Colour grade applied to the whole video")
    notes: str = ""
