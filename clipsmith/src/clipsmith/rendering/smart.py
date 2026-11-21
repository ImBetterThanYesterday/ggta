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
_SMALL_W = 480  # analysis width for the static-overlay search


def probe_size(mp4: Path) -> tuple[int, int]:
    out = subprocess.run(  # nosec B603
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0:s=x", str(mp4)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h = out.split("x")[:2]
    return int(w), int(h)


def _stream_key(cache_dir: Path) -> str:
    """work/<stream>_p<sec>/ -> <stream> (all moments of one stream share a layout)."""
    name = cache_dir.name
    return name.rsplit("_p", 1)[0] if "_p" in name else name


def _stream_cache(cache_dir: Path) -> Path:
    return cache_dir.parent / "facecam_by_stream.json"


def detect_facecam(mp4: Path, cache_dir: Path, samples: int = 24) -> list[int] | None:
    """Return the facecam crop [x, y, w, h] in source pixels, or None.

    1. find the streamer's face (same spot in many frames),
    2. snap to the webcam overlay's borders (edges that stay put while the
       gameplay moves), so we crop the whole cam, never half a face,
    3. fall back to the layout found in another moment of the same stream.
    """
    cache = cache_dir / _CACHE
    if cache.exists():
        data = json.loads(cache.read_text())
        return data or None
    try:
        import cv2  # type: ignore[import-untyped]
        import numpy as np
    except ImportError:
        log.warning("opencv not installed -> no facecam detection")
        return None
    from ..io.media import video_duration
    from .detect import _cluster_rects, _extract_frame

    dur = video_duration(mp4)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    rects: list[tuple[int, int, int, int]] = []
    small: list = []
    frames = 0
    W = H = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(samples):
            t = dur * (0.03 + 0.94 * i / max(1, samples - 1))
            png = Path(tmp) / f"f{i}.png"
            if not _extract_frame(mp4, t, png):
                continue
            img = cv2.imread(str(png))
            if img is None:
                continue
            frames += 1
            H, W = img.shape[:2]
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            small.append(cv2.resize(gray, (_SMALL_W, int(H * _SMALL_W / W))).astype(np.float32))
            short = min(W, H)
            faces = cascade.detectMultiScale(
                gray, scaleFactor=1.08, minNeighbors=5,
                minSize=(int(short * 0.035), int(short * 0.035)),
                maxSize=(int(short * 0.4), int(short * 0.4)),
            )
            rects += [tuple(int(v) for v in f) for f in faces]
    result: list[int] | None = None
    if rects and frames:
        (fx, fy, fw, fh), count = _cluster_rects(rects)[0]
        if count >= 0.3 * frames:
            box = _overlay_box(small, fx, fy, fw, fh, W, H) if len(small) >= 6 else None
            result = _fit_panel(box, fx, fy, fw, fh, W, H)
            log.info("facecam: face %s in %d/%d frames, overlay %s -> crop %s",
                     (fx, fy, fw, fh), count, frames, box, result)
        else:
            log.info("facecam: no stable face (best cluster %d/%d frames)", count, frames)
    shared = _stream_cache(cache_dir)
    try:
        by_stream = json.loads(shared.read_text()) if shared.exists() else {}
    except (OSError, ValueError):
        by_stream = {}
    key = _stream_key(cache_dir)
    if result:
        by_stream[key] = result
        shared.write_text(json.dumps(by_stream))
    elif by_stream.get(key) and rects:
        # Same stream, same overlay: reuse it if a face shows up inside it at least sometimes.
        x, y, w, h = by_stream[key]
        inside = sum(1 for (a, b, c, d) in rects
                     if x <= a + c / 2 <= x + w and y <= b + d / 2 <= y + h)
        if inside >= 2:
            result = list(by_stream[key])
            log.info("facecam: reusing stream layout %s (%d faces inside)", result, inside)
    cache.write_text(json.dumps(result or []))
    return result


def _overlay_box(small: list, fx: int, fy: int, fw: int, fh: int, W: int, H: int):
    """Find the webcam overlay around the face, or None. Returns (x0, y0, x1, y1).

    The cam's room (walls, shelves, chair) barely changes between frames, while
    the gameplay around it always does: the overlay is the static blob that
    touches the face.
    """
    import cv2  # type: ignore[import-untyped]
    import numpy as np

    st = np.std(np.stack(small), axis=0)
    h, w = st.shape
    k = w / W
    static = (st < 10).astype(np.uint8)
    static = cv2.morphologyEx(static, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    static = cv2.morphologyEx(static, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(static)
    # face box grown 1.5x, in small coords
    m = 0.75
    a0, b0 = (fx - fw * m) * k, (fy - fh * m) * k
    a1, b1 = (fx + fw * (1 + m)) * k, (fy + fh * (1 + m)) * k
    best = None
    for i in range(1, n):
        x, y, cw, ch, area = (int(v) for v in stats[i])
        if area < 0.01 * w * h:
            continue
        touches = x <= a1 and x + cw >= a0 and y <= b1 and y + ch >= b0
        if touches and (best is None or area > best[4]):
            best = (x, y, cw, ch, area)
    if best is None:
        return None
    x, y, cw, ch, _ = best
    x0 = int(min(x / k, fx - fw * 1.0))
    y0 = int(min(y / k, fy - fh * 0.5))
    x1 = int(max((x + cw) / k, fx + fw * 2.0))
    y1 = int(max((y + ch) / k, fy + fh * 1.5))
    x0, y0, x1, y1 = max(0, x0 + 4), max(0, y0 + 4), min(W, x1 - 4), min(H, y1 - 4)
    bw, bh = x1 - x0, y1 - y0
    if not (fw * 2.5 <= bw <= W * 0.6 and fh * 2.5 <= bh <= H * 0.8):
        return None
    return (x0, y0, x1, y1)


def _fit_panel(box, fx: int, fy: int, fw: int, fh: int, W: int, H: int) -> list[int]:
    """Largest crop with the top panel's aspect inside `box`, framed on the face."""
    aspect = OUT_W / CAM_PANEL_H
    if box is None:  # no borders found: generous box around the face
        cw = min(W, max(int(fw * 4.5), int(W * 0.22)))
        ch = int(cw / aspect)
        x0, y0 = 0, 0
        x1, y1 = W, H
    else:
        x0, y0, x1, y1 = box
        bw, bh = x1 - x0, y1 - y0
        if bw / bh > aspect:
            ch, cw = bh, int(bh * aspect)
        else:
            cw, ch = bw, int(bw / aspect)
    cx = fx + fw / 2
    cy = fy + fh * 0.75  # face in the upper part, shoulders below
    x = int(min(max(x0, cx - cw / 2), x1 - cw))
    y = int(min(max(y0, cy - ch / 2), y1 - ch))
    # never cut the head: top of the crop stays above the forehead
    y = int(max(y0, min(y, fy - fh * 0.35)))
    return [max(0, x), max(0, y), cw & ~1, ch & ~1]


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
            f"[0:v]crop={w}:{h}:{x}:{y},scale={OUT_W}:{CAM_PANEL_H}:flags=lanczos,"
            f"unsharp=5:5:0.6:5:5:0.0,setsar=1[top];"
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
