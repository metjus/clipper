"""The AI director: turns analysed clips into an :class:`EditPlan`.

Claude receives every clip's transcript (with word times), loudness peaks and
metadata and returns a structured plan: clip order and trims, zoom / shake /
SFX / flash cues, emphasis words, the hook and the TikTok caption + hashtags.

If no API key is configured (or the call fails) a heuristic planner produces a
reasonable plan so the tool still works offline.
"""
from __future__ import annotations

import json
from typing import Callable, List, Optional, Sequence

from .config import get_settings
from .models import (ClipEdit, ClipInfo, EditPlan, FlashCue, SfxCue, ShakeCue, TextOverlay, ZoomCue)

Log = Callable[[str], None]

SYSTEM_PROMPT = """You are a short-form video editor who cuts Twitch clips into viral TikToks in the style of the big streamer clip channels (CaseOh, xQc, etc.): fast, loud, punchy, zero dead air.

You receive analysed source clips: metadata, a word-level transcript (times in seconds from the clip start) and "peaks" (the loudest moments, 0-1). You return an edit plan as JSON matching the schema.

Editing rules:
- ORDER: open with the clip that hooks fastest (biggest reaction, funniest line). The first 1-2 seconds must already be interesting. Save a strong one for last.
- TRIMS: cut silence, setup and rambling. Start a clip at most ~0.5 s before the first interesting word; end right after the payoff. Trim values are in seconds removed from the start / end of the ORIGINAL clip. Be aggressive: a 30 s clip is often 8-15 s of real content. Respect the requested target length.
- ALL CUE TIMES (zooms, shakes, sfx, flashes, overlays) are seconds from the start of the TRIMMED clip, i.e. original_time - trim_start.
- ZOOMS: punch-in on reactions, punchlines, screams and the loud peaks. 1.10-1.25 for emphasis, up to 1.4-1.6 for a huge scream. Roughly one every 2-4 s; do not stack them within 0.8 s.
- SHAKES: only on the biggest impacts (screams, bangs, rage). Pair with a "boom" or "bass_drop".
- SFX: "boom"/"bass_drop" on impacts and shocked reactions, "ding" on a clever or wholesome moment, "pop" on quick cuts/reveals, "whoosh" with a zoom, "record_scratch" on an awkward or sudden stop, "riser" building into a payoff (start it ~1.2 s before). Max ~1 SFX per 2 s per clip.
- FLASHES: a 0.08 s white flash on the hardest hits only.
- OVERLAYS: an optional short callout ("NO WAY", "HE'S DONE") at a key beat, 1-2 s, top or center. Use sparingly (0-1 per clip). No emoji.
- EMPHASIS WORDS: 1-4 words per clip that get the accent colour in the captions: swear words, names, the punchline keyword.
- HOOK: 3-7 words max, shown over the first ~2 s. Curiosity or stakes, never clickbait that the clip does not pay off. No emoji, no hashtags.
- TIKTOK CAPTION: one punchy sentence plus a question or call to comment. HASHTAGS: 5-10, no # sign, mix of streamer, game and generic (fyp, twitch, streamer, gaming, funny...).
- LANGUAGE: write hook, overlays and caption in the language the streamer speaks in the transcript (field "language"). If the transcript is empty, use the metadata/title language.
- speed stays 1.0 unless a long boring stretch must be kept for context (then 1.15-1.3).
- remove_silence (default true) jump-cuts pauses longer than ~0.6 s inside the clip automatically; set it to false only when the silence IS the joke (awkward pause, stare).
- caption_style.preset: "pop" (default, MrBeast-like), "karaoke" (spoken words stay coloured), "bounce" (every word bounces, hyper), "minimal" (calm, lowercase). Pick what fits the energy.
- look: "punchy" (default), "warm", "cold", "vintage" or "none".
- Never invent words that are not in the transcript as emphasis words.
Return only the JSON."""


def plan_edit(clips: Sequence[ClipInfo], target_length: float = 45.0,
              style_hint: str = "", log: Optional[Log] = None) -> EditPlan:
    log = log or (lambda m: None)
    s = get_settings()
    if s.anthropic_api_key:
        try:
            plan = _plan_with_claude(clips, target_length, style_hint, log)
            return _sanitise(plan, clips)
        except Exception as exc:  # noqa: BLE001
            log(f"Claude planning failed ({type(exc).__name__}: {exc}); using heuristic plan")
    else:
        log("no ANTHROPIC_API_KEY configured; using heuristic plan")
    return _sanitise(heuristic_plan(clips, target_length), clips)


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------
def _clip_payload(c: ClipInfo) -> dict:
    return {
        "clip_id": c.id,
        "title": c.title,
        "broadcaster": c.broadcaster,
        "game": c.game,
        "view_count": c.view_count,
        "duration": round(c.duration, 2),
        "language": c.transcript.language,
        "has_facecam": c.facecam is not None,
        "words": [[w.text, round(w.start, 2), round(w.end, 2)] for w in c.transcript.words],
        "peaks": [[p.t, p.loudness] for p in c.peaks],
    }


def _plan_with_claude(clips: Sequence[ClipInfo], target_length: float, style_hint: str, log: Log) -> EditPlan:
    import anthropic

    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key)
    user = {
        "target_length_seconds": target_length,
        "style_hint": style_hint or "default: CaseOh / xQc clip-channel energy",
        "clips": [_clip_payload(c) for c in clips],
    }
    log(f"asking {s.claude_model} for the edit plan ({len(clips)} clips)")
    with client.messages.stream(
        model=s.claude_model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
        output_format=EditPlan,
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError("Claude declined to plan this edit")
    plan = getattr(msg, "parsed_output", None)
    if plan is None:
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        if not text.strip():
            raise RuntimeError("no structured output returned")
        plan = EditPlan.model_validate_json(text)
    log(f"plan: '{plan.hook}' with {len(plan.clips)} clips")
    return plan


# --------------------------------------------------------------------------
# Heuristic fallback
# --------------------------------------------------------------------------
def heuristic_plan(clips: Sequence[ClipInfo], target_length: float = 45.0) -> EditPlan:
    ordered = sorted(clips, key=lambda c: (max((p.loudness for p in c.peaks), default=0), c.view_count), reverse=True)
    edits: List[ClipEdit] = []
    budget = target_length
    for c in ordered:
        words = c.transcript.words
        trim_start = max(0.0, words[0].start - 0.4) if words else 0.0
        trim_end = max(0.0, c.duration - (words[-1].end + 0.6)) if words else 0.0
        length = max(1.0, c.duration - trim_start - trim_end)
        if budget <= 3 and edits:
            break
        if length > budget + 4 and edits:
            trim_end += length - budget
            length = budget
        budget -= length
        zooms, shakes, sfx, flashes = [], [], [], []
        last = -10.0
        for p in c.peaks:
            t = p.t - trim_start
            if t < 0.2 or t > length - 0.3 or t - last < 1.0:
                continue
            last = t
            if p.loudness > 0.85:
                zooms.append(ZoomCue(t=round(t, 2), strength=1.35, hold=0.6))
                shakes.append(ShakeCue(t=round(t, 2), duration=0.4, intensity=1.0))
                sfx.append(SfxCue(t=round(t, 2), name="boom"))
                flashes.append(FlashCue(t=round(t, 2)))
            elif p.loudness > 0.6:
                zooms.append(ZoomCue(t=round(t, 2), strength=1.18, hold=0.5))
                sfx.append(SfxCue(t=round(t, 2), name="whoosh", gain=0.7))
        if not zooms and length > 3:
            zooms.append(ZoomCue(t=round(min(1.5, length / 3), 2), strength=1.12, hold=0.6))
        long_words = sorted((w for w in words if len(w.text) > 4), key=lambda w: -len(w.text))[:2]
        edits.append(ClipEdit(
            clip_id=c.id, trim_start=round(trim_start, 2), trim_end=round(trim_end, 2),
            emphasis_words=[w.text for w in long_words], zooms=zooms, shakes=shakes, sfx=sfx,
            flashes=flashes, reason="heuristic: ordered by loudest reaction",
        ))
    first = ordered[0] if ordered else None
    lang = first.transcript.language if first else "en"
    who = (first.broadcaster or "streamer") if first else "streamer"
    game = (first.game if first else "") or ""
    hook = (first.title[:40] if first and first.title else "WAIT FOR IT").upper()
    tags = ["fyp", "twitch", "twitchclips", "streamer", "gaming", "funny", "viral"]
    if who:
        tags.insert(1, who.lower().replace(" ", ""))
    if game:
        tags.insert(2, game.lower().replace(" ", "").replace(":", "")[:30])
    return EditPlan(
        title=f"{who} compilation",
        hook=hook,
        tiktok_caption=(f"{who} moments that went too far 💀 which one is the best?" if lang == "en"
                        else f"{who} - najlepšie momenty. Ktorý je top?"),
        hashtags=tags[:10],
        clips=edits,
        notes="heuristic plan (no Claude key)",
    )


# --------------------------------------------------------------------------
# Sanity checks on whatever the planner returned
# --------------------------------------------------------------------------
def _sanitise(plan: EditPlan, clips: Sequence[ClipInfo]) -> EditPlan:
    by_id = {c.id: c for c in clips}
    seen = set()
    clean: List[ClipEdit] = []
    for e in plan.clips:
        c = by_id.get(e.clip_id)
        if c is None or e.clip_id in seen:
            continue
        seen.add(e.clip_id)
        total = c.duration or 1.0
        e.trim_start = min(max(0.0, e.trim_start), max(0.0, total - 1.0))
        e.trim_end = min(max(0.0, e.trim_end), max(0.0, total - e.trim_start - 1.0))
        length = total - e.trim_start - e.trim_end
        inside = lambda t: 0.0 <= t <= length  # noqa: E731
        e.zooms = _dedupe([z for z in e.zooms if inside(z.t)], 0.8)
        e.shakes = [z for z in e.shakes if inside(z.t)]
        e.sfx = [z for z in e.sfx if inside(z.t)]
        e.flashes = [z for z in e.flashes if inside(z.t)]
        e.overlays = [TextOverlay(text=o.text[:40], start=max(0.0, o.start), end=min(length, o.end),
                                  position=o.position, color=o.color)
                      for o in e.overlays if o.end > o.start and o.start < length]
        clean.append(e)
    # any clip the planner dropped is appended untouched at the end
    for c in clips:
        if c.id not in seen:
            clean.append(ClipEdit(clip_id=c.id, reason="not placed by planner; appended"))
    plan.clips = clean
    seen_tags: set = set()
    plan.hashtags = [t for t in (h.lstrip("#").replace(" ", "") for h in plan.hashtags if h.strip())
                     if t and not (t.lower() in seen_tags or seen_tags.add(t.lower()))][:12]
    plan.hook = plan.hook.strip()[:60]
    return plan


def _dedupe(cues, min_gap: float):
    out = []
    for z in sorted(cues, key=lambda z: z.t):
        if not out or z.t - out[-1].t >= min_gap:
            out.append(z)
    return out
