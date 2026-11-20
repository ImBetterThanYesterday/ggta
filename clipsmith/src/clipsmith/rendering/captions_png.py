"""TikTok-style captions rendered as transparent PNGs and overlaid with ffmpeg.

Works with ANY ffmpeg build (only needs the `overlay` filter — no libass/drawtext).
Style: 2–3 words at a time, UPPERCASE, heavy white font with black outline, the word
being spoken highlighted in yellow.
"""

from __future__ import annotations

from pathlib import Path

from ..models.transcript import Transcript, Word

_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]
W, H = 1080, 360
FONT_SIZE = 84
STROKE = 9
MAX_WORDS = 3
MAX_CHARS = 18
YELLOW = (255, 230, 0, 255)
WHITE = (255, 255, 255, 255)


def _font():
    from PIL import ImageFont

    for f in _FONTS:
        if Path(f).exists():
            return ImageFont.truetype(f, FONT_SIZE)
    return ImageFont.load_default()


def _words(transcript: Transcript, start: float, end: float) -> list[Word]:
    out: list[Word] = []
    for seg in transcript.segments:
        if seg.end < start or seg.start > end:
            continue
        for w in seg.words:
            if w.start >= start - 0.05 and w.end <= end + 0.3 and w.word.strip():
                out.append(w)
    return out


def _chunks(words: list[Word]) -> list[list[Word]]:
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        text = " ".join(x.word.strip() for x in cur)
        ends_sentence = w.word.strip()[-1:] in ".?!,"
        if len(cur) >= MAX_WORDS or len(text) >= MAX_CHARS or ends_sentence:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def _render(words: list[str], active: int, font, path: Path) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    space = d.textlength(" ", font=font)
    # word-wrap into lines that fit in 1000 px
    lines: list[list[int]] = [[]]
    width = 0.0
    for i, w in enumerate(words):
        wl = d.textlength(w, font=font)
        if lines[-1] and width + space + wl > W - 80:
            lines.append([])
            width = 0.0
        width += (space if lines[-1] else 0) + wl
        lines[-1].append(i)
    line_h = FONT_SIZE + 16
    y = (H - line_h * len(lines)) // 2
    for line in lines:
        total = sum(d.textlength(words[i], font=font) for i in line) + space * (len(line) - 1)
        x = (W - total) / 2
        for i in line:
            d.text((x, y), words[i], font=font, fill=YELLOW if i == active else WHITE,
                   stroke_width=STROKE, stroke_fill=(0, 0, 0, 255))
            x += d.textlength(words[i], font=font) + space
        y += line_h
    img.save(path)


def write_caption_track(
    transcript: Transcript, start: float, end: float, out_dir: Path, skip_until: float = 0.0
) -> Path | None:
    """Render PNG frames + an ffmpeg concat list. Returns the list path (or None).

    skip_until: seconds (relative to clip start) with no captions, e.g. while a hook plays.
    """
    words = [w for w in _words(transcript, start, end) if w.start - start >= skip_until]
    if not words:
        return None
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    font = _font()
    blank = out_dir / "blank.png"
    from PIL import Image

    Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(blank)

    # (t0, t1, png) timeline relative to clip start
    timeline: list[tuple[float, float, Path]] = []
    n = 0
    for chunk in _chunks(words):
        texts = [w.word.strip().upper() for w in chunk]
        for j, w in enumerate(chunk):
            t0 = max(0.0, w.start - start)
            t1 = (chunk[j + 1].start if j + 1 < len(chunk) else w.end) - start
            png = out_dir / f"c{n:04d}.png"
            n += 1
            _render(texts, j, font, png)
            timeline.append((t0, max(t0 + 0.05, t1), png))

    lines, t = [], 0.0
    for t0, t1, png in timeline:
        if t0 > t + 0.02:
            lines += [f"file '{blank}'", f"duration {t0 - t:.3f}"]
            t = t0
        dur = max(0.05, t1 - t)
        lines += [f"file '{png}'", f"duration {dur:.3f}"]
        t += dur
    total = end - start
    if total > t:
        lines += [f"file '{blank}'", f"duration {total - t:.3f}"]
    lines.append(f"file '{blank}'")  # concat demuxer needs the last file repeated
    lst = out_dir / "captions.txt"
    lst.write_text("ffconcat version 1.0\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return lst
