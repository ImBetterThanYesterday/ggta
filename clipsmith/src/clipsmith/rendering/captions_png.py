"""TikTok-style captions rendered as transparent PNGs and overlaid with ffmpeg.

Works with ANY ffmpeg build (only needs the `overlay` filter — no libass/drawtext).
Several presets (see PRESETS) so clips don't all look the same: the edit plan picks one.
"""

from __future__ import annotations

from dataclasses import dataclass
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
BLACK = (0, 0, 0, 255)


@dataclass(frozen=True)
class Style:
    font: tuple[str, int] = ("/System/Library/Fonts/Supplemental/Arial Black.ttf", 0)
    size: int = FONT_SIZE
    stroke: int = STROKE
    upper: bool = True
    color: tuple = WHITE
    active: tuple | None = YELLOW  # colour of the word being spoken (None = no karaoke)
    accent: tuple = (255, 70, 70, 255)  # emphasis words from the edit plan
    box: tuple | None = None  # pill behind each chunk
    max_words: int = MAX_WORDS
    max_chars: int = MAX_CHARS
    shadow: bool = False


_SUP = "/System/Library/Fonts/Supplemental/"
PRESETS: dict[str, Style] = {
    "yellow_pop": Style(),
    "green_pop": Style(font=(_SUP + "Impact.ttf", 0), size=96, stroke=8,
                       active=(57, 255, 20, 255), accent=(255, 230, 0, 255)),
    "boxed": Style(size=72, stroke=0, active=YELLOW, box=(0, 0, 0, 205), max_words=4, max_chars=22),
    "single_word": Style(font=(_SUP + "Impact.ttf", 0), size=128, stroke=10, active=None,
                         max_words=1, max_chars=14),
    "clean": Style(font=("/System/Library/Fonts/Avenir Next.ttc", 8), size=76, stroke=4, upper=False,
                   active=None, accent=(255, 214, 10, 255), shadow=True, max_words=4, max_chars=24),
    "din_caps": Style(font=(_SUP + "DIN Condensed Bold.ttf", 0), size=112, stroke=7,
                      active=(255, 60, 60, 255), accent=(255, 230, 0, 255), max_words=3, max_chars=20),
}


def _font(style: Style | None = None):
    from PIL import ImageFont

    if style is not None and Path(style.font[0]).exists():
        return ImageFont.truetype(style.font[0], style.size, index=style.font[1])
    size = style.size if style else FONT_SIZE
    for f in _FONTS:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
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


def _chunks(words: list[Word], max_words: int = MAX_WORDS, max_chars: int = MAX_CHARS) -> list[list[Word]]:
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        text = " ".join(x.word.strip() for x in cur)
        ends_sentence = w.word.strip()[-1:] in ".?!,"
        if len(cur) >= max_words or len(text) >= max_chars or ends_sentence:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def _clean(word: str) -> str:
    return word.strip(" .,!?¡¿\"'«»").upper()


def _render(words: list[str], active: int, font, path: Path, style: Style | None = None,
            emphasis: frozenset[str] = frozenset()) -> None:
    from PIL import Image, ImageDraw

    st = style or Style()
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
    line_h = st.size + 16
    y = (H - line_h * len(lines)) // 2
    for line in lines:
        total = sum(d.textlength(words[i], font=font) for i in line) + space * (len(line) - 1)
        x = (W - total) / 2
        if st.box is not None:
            asc, desc = font.getmetrics()
            d.rounded_rectangle((x - 26, y - 10, x + total + 26, y + asc + desc + 6),
                                radius=22, fill=st.box)
        for i in line:
            fill = st.color
            if _clean(words[i]) in emphasis:
                fill = st.accent
            elif st.active is not None and i == active:
                fill = st.active
            if st.shadow:
                d.text((x + 4, y + 5), words[i], font=font, fill=(0, 0, 0, 150))
            d.text((x, y), words[i], font=font, fill=fill,
                   stroke_width=st.stroke, stroke_fill=BLACK)
            x += d.textlength(words[i], font=font) + space
        y += line_h
    img.save(path)


def write_caption_track(
    transcript: Transcript, start: float, end: float, out_dir: Path, skip_until: float = 0.0,
    *, preset: str = "yellow_pop", emphasis: list[str] | tuple[str, ...] = (), offset: float = 0.0,
) -> Path | None:
    """Render PNG frames + an ffmpeg concat list. Returns the list path (or None).

    skip_until: seconds (relative to clip start) with no captions, e.g. while a hook plays.
    offset:     seconds of other footage before the clip (a cold-open teaser): captions shift.
    """
    style = PRESETS.get(preset, PRESETS["yellow_pop"])
    emph = frozenset(_clean(e) for e in emphasis)
    words = [w for w in _words(transcript, start, end) if w.start - start >= skip_until]
    if not words:
        return None
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    font = _font(style)
    blank = out_dir / "blank.png"
    from PIL import Image

    Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(blank)

    # (t0, t1, png) timeline relative to clip start
    timeline: list[tuple[float, float, Path]] = []
    n = 0
    for chunk in _chunks(words, style.max_words, style.max_chars):
        texts = [w.word.strip().upper() if style.upper else w.word.strip() for w in chunk]
        for j, w in enumerate(chunk):
            t0 = max(0.0, w.start - start) + offset
            t1 = (chunk[j + 1].start if j + 1 < len(chunk) else w.end) - start + offset
            png = out_dir / f"c{n:04d}.png"
            n += 1
            _render(texts, j, font, png, style, emph)
            timeline.append((t0, max(t0 + 0.05, t1), png))

    lines, t = [], 0.0
    for t0, t1, png in timeline:
        if t0 > t + 0.02:
            lines += [f"file '{blank}'", f"duration {t0 - t:.3f}"]
            t = t0
        dur = max(0.05, t1 - t)
        lines += [f"file '{png}'", f"duration {dur:.3f}"]
        t += dur
    total = end - start + offset
    if total > t:
        lines += [f"file '{blank}'", f"duration {total - t:.3f}"]
    lines.append(f"file '{blank}'")  # concat demuxer needs the last file repeated
    lst = out_dir / "captions.txt"
    lst.write_text("ffconcat version 1.0\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return lst
