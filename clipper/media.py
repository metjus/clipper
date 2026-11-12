"""Media probing and signal analysis: ffprobe, audio loudness peaks, facecam detection."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List, Optional

import numpy as np

from .config import get_settings
from .models import Box, Peak


# --------------------------------------------------------------------------
# ffprobe
# --------------------------------------------------------------------------
def probe(path: str | Path) -> dict:
    s = get_settings()
    r = subprocess.run(
        [s.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(r.stdout)
    v = next((st for st in data["streams"] if st["codec_type"] == "video"), {})
    a = next((st for st in data["streams"] if st["codec_type"] == "audio"), None)
    num, _, den = (v.get("avg_frame_rate") or "30/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except ZeroDivisionError:
        fps = 30.0
    width, height = int(v.get("width") or 0), int(v.get("height") or 0)
    rot = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            rot = int(abs(float(sd["rotation"])))
    if rot in (90, 270):
        width, height = height, width
    return {
        "duration": float(data["format"].get("duration") or 0.0),
        "width": width,
        "height": height,
        "fps": fps or 30.0,
        "has_audio": a is not None,
    }


def frame_jpeg(path: str | Path, t: float = 1.0, width: int = 640) -> bytes:
    """One frame as JPEG bytes (for the UI)."""
    s = get_settings()
    r = subprocess.run(
        [s.ffmpeg, "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-frames:v", "1",
         "-vf", f"scale={width}:-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "-"],
        capture_output=True, check=True,
    )
    return r.stdout


# --------------------------------------------------------------------------
# Audio energy
# --------------------------------------------------------------------------
def extract_audio(path: str | Path, out_wav: Path, sr: int = 16000) -> Path:
    s = get_settings()
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [s.ffmpeg, "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr),
         "-c:a", "pcm_s16le", str(out_wav)],
        check=True, capture_output=True,
    )
    return out_wav


def _read_pcm(path: str | Path, sr: int = 16000) -> np.ndarray:
    s = get_settings()
    r = subprocess.run(
        [s.ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
        capture_output=True, check=True,
    )
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def loudness_peaks(path: str | Path, window: float = 0.05, max_peaks: int = 12,
                   min_gap: float = 1.2) -> List[Peak]:
    """Return the loudest moments of the clip, normalised 0..1.

    We compute short-window RMS, smooth it, and pick local maxima that stand
    well above the clip's median loudness. Those are the screams, laughs and
    bangs that deserve a punch-in or a sound effect.
    """
    sr = 16000
    try:
        pcm = _read_pcm(path, sr)
    except subprocess.CalledProcessError:
        return []
    if pcm.size < sr:
        return []
    n = int(window * sr)
    frames = pcm[: (pcm.size // n) * n].reshape(-1, n)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-9)
    # smooth with a ~0.25 s moving average
    k = max(1, int(0.25 / window))
    sm = np.convolve(rms, np.ones(k) / k, mode="same")
    if sm.max() <= 0:
        return []
    norm = sm / sm.max()
    median = float(np.median(norm))
    thresh = max(0.45, median * 2.2)

    order = np.argsort(norm)[::-1]
    picked: List[int] = []
    gap = int(min_gap / window)
    for i in order:
        if norm[i] < thresh:
            break
        if all(abs(i - j) >= gap for j in picked):
            picked.append(int(i))
        if len(picked) >= max_peaks:
            break
    picked.sort()
    return [Peak(t=round(i * window, 2), loudness=round(float(norm[i]), 3)) for i in picked]


# --------------------------------------------------------------------------
# Facecam detection
# --------------------------------------------------------------------------
def grab_frames(path: str | Path, count: int = 6, duration: Optional[float] = None) -> List[np.ndarray]:
    """Decode ``count`` evenly spaced frames as BGR arrays (via OpenCV)."""
    import cv2  # lazy

    cap = cv2.VideoCapture(str(path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    frames = []
    if total <= 0:
        cap.release()
        return frames
    for i in range(count):
        idx = int(total * (i + 0.5) / count)
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if ok:
            frames.append(frame)
    cap.release()
    return frames


YUNET_MODEL = Path(__file__).resolve().parent / "assets" / "models" / "face_detection_yunet_2023mar.onnx"
_yunet = None


def _detect_faces(frame: np.ndarray) -> List[tuple]:
    """Return (cx, cy, w, h) for faces in a BGR frame.

    Uses OpenCV's YuNet DNN detector (bundled ONNX model, MIT licence); falls
    back to the Haar cascade on OpenCV builds that still ship it.
    """
    import cv2  # lazy

    global _yunet
    h, w = frame.shape[:2]
    if hasattr(cv2, "FaceDetectorYN") and YUNET_MODEL.is_file():
        scale = 640 / max(w, h)
        small = cv2.resize(frame, (int(w * scale), int(h * scale)))
        if _yunet is None:
            _yunet = cv2.FaceDetectorYN.create(str(YUNET_MODEL), "", (small.shape[1], small.shape[0]),
                                               score_threshold=0.7, nms_threshold=0.3, top_k=50)
        _yunet.setInputSize((small.shape[1], small.shape[0]))
        _, faces = _yunet.detect(small)
        out = []
        for f in (faces if faces is not None else []):
            x, y, fw, fh = (float(v) / scale for v in f[:4])
            if fw < w * 0.025:
                continue
            out.append((x + fw / 2, y + fh / 2, fw, fh))
        return out
    if hasattr(cv2, "CascadeClassifier"):
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        gray = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                         minSize=(int(w * 0.03), int(w * 0.03)))
        return [(x + fw / 2, y + fh / 2, fw, fh) for (x, y, fw, fh) in faces]
    return []


def detect_facecam(path: str | Path) -> Optional[Box]:
    """Find the streamer's webcam overlay.

    Strategy: run a Haar face detector on a few frames, cluster detections that
    appear in roughly the same place across frames (a game character moves,
    the webcam does not), then expand the face into a typical cam rectangle
    and snap it to the nearest frame corner / edge.
    Returns ``None`` if nothing consistent is found.
    """
    frames = grab_frames(path, count=7)
    if not frames:
        return None
    h, w = frames[0].shape[:2]
    dets: List[tuple] = [d for f in frames for d in _detect_faces(f)]
    if len(dets) < 2:
        return None

    # cluster by centre proximity
    best: List[tuple] = []
    for d in dets:
        cluster = [e for e in dets if abs(e[0] - d[0]) < w * 0.06 and abs(e[1] - d[1]) < h * 0.08]
        if len(cluster) > len(best):
            best = cluster
    # need the face in at least ~40% of sampled frames
    if len(best) < max(2, int(len(frames) * 0.4)):
        return None

    cx = float(np.median([d[0] for d in best]))
    cy = float(np.median([d[1] for d in best]))
    fw = float(np.median([d[2] for d in best]))
    fh = float(np.median([d[3] for d in best]))

    # typical cam box: ~3.2x face width, 16:9-ish, face in the upper-middle
    bw = min(w * 0.45, max(w * 0.18, fw * 3.4))
    bh = bw * 9 / 16
    bx = cx - bw / 2
    by = cy - bh * 0.42
    # snap to edges when close
    if bx < w * 0.08:
        bx = 0
    if bx + bw > w * 0.92:
        bx = w - bw
    if by < h * 0.08:
        by = 0
    if by + bh > h * 0.92:
        by = h - bh
    bx = max(0, min(bx, w - bw))
    by = max(0, min(by, h - bh))
    return Box(x=round(bx / w, 4), y=round(by / h, 4), w=round(bw / w, 4), h=round(bh / h, 4))
