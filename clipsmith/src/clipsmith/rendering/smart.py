"""'smart' vertical layout for gaming streams.

- Facecam found  -> streamer's cam on top (face + surroundings, aspect preserved),
                    gameplay on the bottom (crop, aspect preserved).
- No facecam     -> gameplay large in the middle over a blurred copy of itself.

A face only counts as the facecam if it sits in the SAME spot in most sampled
frames (NPC faces in GTA move around; a webcam overlay doesn't).
"""

from __future__ import annotations

import json
import logging
import subprocess  # nosec B404
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

OUT_W, OUT_H = 1080, 1920
CAM_PANEL_H = 700  # top panel height when a facecam exists
_CACHE = "facecam.json"


def probe_size(mp4: Path) -> tuple[int, int]:
    out = subprocess.run(  # nosec B603
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0:s=x", str(mp4)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h = out.split("x")[:2]
    return int(w), int(h)


def detect_facecam(mp4: Path, cache_dir: Path, samples: int = 16) -> list[int] | None:
    """Return the facecam crop [x, y, w, h] in source pixels, or None."""
    cache = cache_dir / _CACHE
    if cache.exists():
        data = json.loads(cache.read_text())
        return data or None
    try:
        import cv2  # type: ignore[import-untyped]
    except ImportError:
        log.warning("opencv not installed -> no facecam detection")
        return None
    from ..io.media import video_duration
    from .detect import _cluster_rects, _extract_frame

    dur = video_duration(mp4)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    rects: list[tuple[int, int, int, int]] = []
    frames = 0
    W = H = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(samples):
            t = dur * (0.05 + 0.9 * i / max(1, samples - 1))
            png = Path(tmp) / f"f{i}.png"
            if not _extract_frame(mp4, t, png):
                continue
            img = cv2.imread(str(png))
            if img is None:
                continue
            frames += 1
            H, W = img.shape[:2]
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            short = min(W, H)
            faces = cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=6,
                minSize=(int(short * 0.04), int(short * 0.04)),
                maxSize=(int(short * 0.4), int(short * 0.4)),
            )
            rects += [tuple(int(v) for v in f) for f in faces]
    result: list[int] | None = None
    if rects and frames:
        (fx, fy, fw, fh), count = _cluster_rects(rects)[0]
        if count >= 0.4 * frames:
            result = _expand_face(fx, fy, fw, fh, W, H)
            log.info("facecam: face %s in %d/%d frames -> crop %s", (fx, fy, fw, fh), count, frames, result)
        else:
            log.info("facecam: no stable face (best cluster %d/%d frames)", count, frames)
    cache.write_text(json.dumps(result or []))
    return result


def _expand_face(fx: int, fy: int, fw: int, fh: int, W: int, H: int) -> list[int]:
    """Grow the face box into a cam crop with the top panel's aspect ratio."""
    aspect = OUT_W / CAM_PANEL_H
    cw = min(W, int(fw * 3.6))
    ch = int(cw / aspect)
    if ch > H:
        ch = H
        cw = int(ch * aspect)
    cx = fx + fw / 2
    cy = fy + fh * 0.65  # keep some headroom; shoulders/desk below
    x = int(min(max(0, cx - cw / 2), W - cw))
    y = int(min(max(0, cy - ch / 2), H - ch))
    return [x, y, cw & ~1, ch & ~1]


def smart_filter(
    mp4: Path,
    cache_dir: Path,
    captions: bool = False,
    *,
    captions_input: int | None = None,
    hook_input: int | None = None,
    hook_dur: float = 0.0,
) -> str:
    """ffmpeg -filter_complex producing [out] at 1080x1920.

    captions_input: index of the PNG caption track input (see captions_png).
    hook_input:     index of the hook PNG input, shown for the first `hook_dur` seconds.
    """
    if captions and captions_input is None:
        captions_input = 1
    W, H = probe_size(mp4)
    cam = detect_facecam(mp4, cache_dir)

    if cam:
        x, y, w, h = cam
        bot_h = OUT_H - CAM_PANEL_H
        # Gameplay: crop the frame to the bottom panel's aspect, centered, avoiding the cam.
        gw = min(W, int(H * OUT_W / bot_h))
        gx = (W - gw) // 2
        if x + w / 2 < W / 2 and gx < x + w and W - (x + w) >= gw:  # cam on the left
            gx = max(gx, x + w - gw // 6)
        gx = min(max(0, gx), W - gw)
        base = (
            f"[0:v]crop={w}:{h}:{x}:{y},scale={OUT_W}:{CAM_PANEL_H}:flags=lanczos,setsar=1[top];"
            f"[0:v]crop={gw & ~1}:{H}:{gx}:0,scale={OUT_W}:{bot_h}:flags=lanczos,setsar=1[bot];"
            f"[top][bot]vstack[base]"
        )
        cap_y, hook_cy = CAM_PANEL_H - 60, CAM_PANEL_H
    else:
        # No facecam: gameplay big in the middle (~80% of the width) over a blurred copy.
        fg_w = int(OUT_W * 1.25)
        base = (
            f"[0:v]split[a][b];"
            f"[a]scale=-2:{OUT_H},crop={OUT_W}:{OUT_H},boxblur=24:2,eq=brightness=-0.08[bg];"
            f"[b]scale={fg_w}:-2:flags=lanczos,crop={OUT_W}:ih[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]"
        )
        cap_y, hook_cy = 1180, 520

    chain, cur = [base], "[base]"
    if captions_input is not None:
        chain.append(f"{cur}[{captions_input}:v]overlay=(W-w)/2:{cap_y}:eof_action=pass:format=auto[c]")
        cur = "[c]"
    if hook_input is not None and hook_dur > 0:
        chain.append(
            f"{cur}[{hook_input}:v]overlay=(W-w)/2:{hook_cy}-h/2:"
            f"enable='lt(t,{hook_dur:.2f})':eof_action=pass:format=auto[h]"
        )
        cur = "[h]"
    chain.append(f"{cur}null[out]")
    return ";".join(chain)
