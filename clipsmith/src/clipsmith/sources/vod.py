"""Chat-first processing of FINISHED streams.

1. Download the chat of the WHOLE stream (cheap: a few MB).
2. Find the moments where chat exploded (relative to that stream's own baseline).
3. Download ONLY those few minutes in 1080p, transcribe them, let the LLM decide
   the exact cut, render vertical clips.

This avoids downloading/transcribing a 12-hour stream to get 10 clips.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import subprocess  # nosec B404
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ..models.chat import ChatMessage
from ..models.twitch import Clip
from .fetch import (
    _KICK_UUID,
    _UA,
    _YTDLP_BASE,
    _heatmap_clips,
    _kick_chat,
    _parse_youtube_live_chat,
    _twitch_chat_range,
    _ytdlp_info,
    detect_platform,
)

log = logging.getLogger(__name__)

# Messages that signal laughter / shock / hype, across EN + ES (+ some PT).
_REACTION = re.compile(
    r"\b(KEKW|LUL|LULW|OMEGALUL|ICANT|Pog|PogChamp|POGGERS|monkaS|monkaW|PepeLaugh|Aware|"
    r"LMAO|LMFAO|WTF|NO WAY|CLIP|clip it|W+|L+|xd+|ja(ja)+|je(je)+|js(js)+|kk+k|"
    r"NOO+|DIOS|QU[EÉ] PAS[OÓ]|HAHA+)\b|\?{3,}|!{3,}|💀|😂|🤣",
    re.IGNORECASE,
)


# Giveaway entries / roll-calls / bot commands: lots of messages, nothing happened on screen.
_NOISE = re.compile(
    r"^(!\w+.*|p?ar?t[ie]?c?[ií]?p[oó]+|participar|me apunto|yo+|aqu[ií]|presente|sorteo|"
    r"giveaway|enter|join|first|primero|hola+|hello|hi|buenas|saludos.*|"
    r"@\w+)$",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


@dataclass
class Peak:
    t: float  # seconds into the stream (chat time, i.e. AFTER the event)
    score: float
    msgs: int
    unique: int
    reactions: int
    samples: list[str] = field(default_factory=list)


@dataclass
class VodChat:
    platform: str
    url: str
    title: str
    channel: str
    duration_s: float
    messages: list[ChatMessage]
    signal_clips: list[Clip]
    started_at: datetime | None = None  # when the stream went live (UTC), if known


# --------------------------------------------------------------------------- full chat


def _load_chat(path: Path) -> list[ChatMessage] | None:
    """Full chats of finished streams never change: reuse the first download."""
    if not path.exists():
        return None
    return [ChatMessage(**m) for m in json.loads(path.read_text(encoding="utf-8"))]


def _save_chat(path: Path, msgs: list[ChatMessage]) -> None:
    if not msgs:  # don't cache a failed/empty download
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([vars(m) for m in msgs], ensure_ascii=False), encoding="utf-8")


def _info_started(info: dict) -> datetime | None:
    """Stream start from yt-dlp metadata: exact timestamp if present, else the upload day."""
    ts = info.get("release_timestamp") or info.get("timestamp")
    if ts:
        return datetime.fromtimestamp(float(ts), UTC)
    day = info.get("release_date") or info.get("upload_date")
    if day and len(str(day)) == 8:
        return datetime.strptime(str(day), "%Y%m%d").replace(tzinfo=UTC)
    return None


def fetch_full_chat(url: str, work_dir: Path) -> VodChat:
    platform = detect_platform(url)
    if platform == "youtube":
        info = _ytdlp_info(url)
        d = work_dir / f"vod_yt_{info['id']}"
        d.mkdir(parents=True, exist_ok=True)
        lc = d / "yt.live_chat.json"
        if not lc.exists():
            log.info("youtube: downloading full chat replay (can take a few minutes)")
            subprocess.run(  # nosec B603
                [sys.executable, "-m", "yt_dlp", *_YTDLP_BASE, url, "--skip-download",
                 "--write-subs", "--sub-langs", "live_chat", "-o", str(d / "yt")],
                check=True,
            )
        msgs = _parse_youtube_live_chat(lc) if lc.exists() else []
        return VodChat("youtube", url, info.get("title", ""), info.get("channel", ""),
                       float(info.get("duration") or 0), msgs, _heatmap_clips(info, 0, 0, top=15),
                       _info_started(info))

    if platform == "kick":
        uuid = _KICK_UUID.search(url).group(0)  # type: ignore[union-attr]
        meta = httpx.get(f"https://kick.com/api/v1/video/{uuid}", headers=_UA, timeout=30).json()
        ls = meta.get("livestream") or {}
        dur = (ls.get("duration") or 0) / 1000
        start = datetime.fromisoformat(ls["start_time"]).astimezone(UTC)
        cache = work_dir / "vod_chat" / f"kick_{uuid}.json"
        msgs = _load_chat(cache)
        if msgs is None:
            log.info("kick: downloading full chat (%.1f h ≈ %.0f min)", dur / 3600, dur / 5 / 4 / 60)
            msgs = _kick_chat(ls["channel_id"], start, 0, dur)
            _save_chat(cache, msgs)
        return VodChat("kick", url, ls.get("session_title", ""),
                       (ls.get("channel") or {}).get("slug", ""), dur, msgs, [], start)

    vod = re.search(r"(\d{6,})", url).group(1)  # type: ignore[union-attr]
    info = _ytdlp_info(f"https://www.twitch.tv/videos/{vod}")
    cache = work_dir / "vod_chat" / f"twitch_{vod}.json"
    msgs = _load_chat(cache)
    if msgs is None:
        msgs = _twitch_chat_range(vod, 0, 0)
        _save_chat(cache, msgs)
    return VodChat("twitch", f"https://www.twitch.tv/videos/{vod}", info.get("title", ""),
                   info.get("uploader", ""), float(info.get("duration") or 0), msgs, [],
                   _info_started(info))


# --------------------------------------------------------------------------- peaks


def find_peaks(
    messages: list[ChatMessage],
    *,
    window_s: int = 10,
    baseline_s: int = 300,
    top: int = 10,
    min_gap_s: int = 180,
    min_score: float = 3.0,
) -> list[Peak]:
    """Chat explosions relative to the stream's own rolling baseline.

    Combines message rate, unique chatters (so one spammer can't fake a peak) and
    reaction messages (laughs / shock / CLIP), each as a z-score vs the surrounding
    `baseline_s` seconds.
    """
    if not messages:
        return []
    n = int(messages[-1].time_in_seconds // window_s) + 1
    msgs = [0] * n
    reacts = [0] * n
    users: dict[int, set[str]] = defaultdict(set)
    samples: dict[int, list[str]] = defaultdict(list)
    top_msg: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for m in messages:
        text = _norm(m.message)
        if not text or _NOISE.match(text):
            continue
        b = int(m.time_in_seconds // window_s)
        top_msg[b][text] += 1
        msgs[b] += 1
        users[b].add(m.author)
        if _REACTION.search(m.message) or m.is_clip_command:
            reacts[b] += 1
        if len(samples[b]) < 8 and m.message:
            samples[b].append(m.message[:70])
    uniq = [len(users[b]) for b in range(n)]
    k = max(3, baseline_s // window_s)

    def z(series: list[int]) -> list[float]:
        out = [0.0] * n
        for i in range(n):
            win = series[max(0, i - k): i] + series[i + 1: i + k + 1]
            if len(win) < 3:
                continue
            mu = statistics.fmean(win)
            sd = statistics.pstdev(win) or 1.0
            out[i] = (series[i] - mu) / sd
        return out

    zm, zu, zr = z(msgs), z(uniq), z(reacts)
    score = [0.3 * zm[i] + 0.35 * zu[i] + 0.35 * zr[i] for i in range(n)]
    # Copypasta/command floods: one non-reaction message dominating the window.
    for i in range(n):
        if msgs[i] >= 10 and top_msg[i]:
            text, cnt = max(top_msg[i].items(), key=lambda kv: kv[1])
            if cnt / msgs[i] > 0.35 and not _REACTION.search(text):
                score[i] *= 0.3
        # Lots of chat but nobody laughing/shocked -> probably a roll-call, not a moment.
        if reacts[i] < max(3, 0.08 * msgs[i]):
            score[i] *= 0.5
    # Ignore the first/last minutes (intro screens, raids, goodbyes).
    edge = max(1, 120 // window_s)
    order = sorted(range(edge, max(edge, n - edge)), key=lambda i: score[i], reverse=True)
    picked: list[int] = []
    gap = min_gap_s // window_s
    for i in order:
        if score[i] < min_score:
            break
        if all(abs(i - j) > gap for j in picked):
            picked.append(i)
        if len(picked) >= top:
            break
    return [
        Peak(t=i * window_s + window_s / 2, score=round(score[i], 2), msgs=msgs[i],
             unique=uniq[i], reactions=reacts[i], samples=samples[i])
        for i in picked
    ]


def merge_heatmap(peaks: list[Peak], heat: list[Clip], top: int, min_gap_s: int = 180) -> list[Peak]:
    """Add YouTube 'most replayed' peaks that aren't already covered by a chat peak."""
    out = list(peaks)
    for c in heat:
        t = float(c.vod_offset or 0)
        if all(abs(t - p.t) > min_gap_s for p in out) and len(out) < top:
            out.append(Peak(t=t + 15, score=0.0, msgs=0, unique=0, reactions=0,
                            samples=["(YouTube: 'lo más repetido')"]))
    return out


# --------------------------------------------------------------------------- loudness ("bulla")


def fetch_audio(url: str, platform: str, work_dir: Path) -> Path:
    """Lowest-bitrate AUDIO-ONLY copy of the whole stream (12 h ≈ 250 MB on YouTube).

    Avoids YouTube's '-drc' tracks: dynamic-range compression flattens the very screams
    we want to find.
    """
    out_dir = work_dir / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^\w]+", "_", url)[-60:]
    out = out_dir / f"{key}.m4a"
    if out.exists():
        return out
    src = url
    if platform == "kick":
        uuid = _KICK_UUID.search(url).group(0)  # type: ignore[union-attr]
        meta = httpx.get(f"https://kick.com/api/v1/video/{uuid}", headers=_UA, timeout=30).json()
        src = meta.get("source") or url
    fmt = "wa[format_id!*=drc][vcodec=none]/ba[format_id!*=drc][vcodec=none]/worst"
    log.info("bulla: downloading audio-only track of the whole stream")
    subprocess.run(  # nosec B603
        [sys.executable, "-m", "yt_dlp", *_YTDLP_BASE, src, "--no-playlist", "-f", fmt,
         "-x", "--audio-format", "m4a", "--concurrent-fragments", "8",
         "-o", str(out.with_suffix(".%(ext)s"))],
        check=True,
    )
    return out


def loudness_series(audio: Path, sr: int = 8000) -> list[float]:
    """Loudness in dBFS for every second of the audio (streamed, low memory)."""
    import numpy as np

    cache = audio.with_suffix(".loud.json")
    if cache.exists():
        return json.loads(cache.read_text())
    proc = subprocess.Popen(  # nosec B603
        ["ffmpeg", "-v", "error", "-i", str(audio), "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
        stdout=subprocess.PIPE,
    )
    out: list[float] = []
    chunk = sr * 2
    assert proc.stdout is not None
    while True:
        buf = proc.stdout.read(chunk)
        if not buf:
            break
        x = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        out.append(round(20 * np.log10(max(rms, 1e-5)), 2))
    proc.wait()
    cache.write_text(json.dumps(out))
    return out


def find_loud_peaks(
    db: list[float],
    *,
    top: int = 10,
    min_gap_s: int = 180,
    baseline_s: int = 150,
    min_score: float = 3.0,
) -> list[Peak]:
    """Moments where the audio gets much louder than the stream's surrounding level.

    Short-term loudness (3 s) vs a robust rolling baseline (median ± MAD over ±baseline_s),
    so a stream that is loud all the time doesn't flag everything.
    """
    import numpy as np

    if len(db) < 600:
        return []
    a = np.array(db, dtype=np.float32)
    power = 10 ** (a / 10)
    k = 3
    short = 10 * np.log10(np.convolve(power, np.ones(k) / k, mode="same") + 1e-12)
    n = len(a)
    score = np.zeros(n, dtype=np.float32)
    step = 10  # compute the baseline every 10 s (fast enough for 12 h streams)
    for i in range(0, n, step):
        win = a[max(0, i - baseline_s): i + baseline_s]
        win = win[win > -60]  # ignore silence/muted parts
        if win.size < 60:
            continue
        med = float(np.median(win))
        mad = float(np.median(np.abs(win - med))) or 1.0
        seg = short[i: i + step]
        score[i: i + step] = (seg - med) / (1.4826 * mad)
    # Must also be loud in absolute terms for this stream (top 25% of its loudness),
    # otherwise "a bit louder than a silent menu" would count as a scream.
    loud_floor = float(np.percentile(a[a > -60], 75)) if (a > -60).any() else -30.0
    score[short < loud_floor] = 0
    edge = 120
    order = np.argsort(-score)
    picked: list[int] = []
    for i in order:
        i = int(i)
        if score[i] < min_score:
            break
        if i < edge or i > n - edge:
            continue
        if all(abs(i - j) > min_gap_s for j in picked):
            picked.append(i)
        if len(picked) >= top:
            break
    return [
        Peak(t=float(i), score=round(float(score[i]), 2), msgs=0, unique=0, reactions=0,
             samples=[f"(BULLA: {float(short[i]):.0f} dB, {float(score[i]):.1f}σ sobre lo normal)"])
        for i in picked
    ]


def merge_peaks(chat: list[Peak], loud: list[Peak], top: int, near_s: int = 45,
                min_gap_s: int = 180) -> list[Peak]:
    """Chat peaks + loudness peaks. Both at once = strongest signal (goes first)."""
    both, chat_only, used = [], [], set()
    for p in chat:
        match = next((j for j, q in enumerate(loud)
                      if j not in used and -near_s <= p.t - q.t <= near_s), None)
        if match is not None:
            used.add(match)
            q = loud[match]
            p.score = round(p.score + q.score, 2)
            p.samples = q.samples + p.samples
            both.append(p)
        else:
            chat_only.append(p)
    loud_only = [q for j, q in enumerate(loud) if j not in used]
    out: list[Peak] = []
    for p in sorted(both, key=lambda x: -x.score) + _interleave(chat_only, loud_only):
        if all(abs(p.t - q.t) > min_gap_s for q in out):
            out.append(p)
        if len(out) >= top:
            break
    return out


def _interleave(a: list[Peak], b: list[Peak]) -> list[Peak]:
    out = []
    for i in range(max(len(a), len(b))):
        out += a[i:i + 1] + b[i:i + 1]
    return out


# --------------------------------------------------------------------------- singing along

# Chat reacting to the streamer singing a song (in-game radio etc.) — very viral format.
_SONG = re.compile(
    r"(temaz[o0]+|temax[o0]+|tema+z+o|canta|cant[aá]ndo|cantante|karaoke|la canci[oó]n|"
    r"qu[eé] canci[oó]n|qu[eé] tema|el tema|perrea|perreo|bailando|\bsing(ing)?\b|\bsong\b|"
    r"\bbanger\b|\bvib(e|ing|in)\b|\bjam\b|ARTIST|concierto|concert|🎵|🎶|🎤|🎧|💃|🕺)",
    re.IGNORECASE,
)


def find_song_peaks(messages: list[ChatMessage], *, window_s: int = 30, top: int = 6,
                    min_msgs: int = 6, min_gap_s: int = 180) -> list[Peak]:
    """Windows where many different people talk about singing / the song at once."""
    if not messages:
        return []
    n = int(messages[-1].time_in_seconds // window_s) + 1
    hits = [0] * n
    users: dict[int, set[str]] = defaultdict(set)
    samples: dict[int, list[str]] = defaultdict(list)
    for m in messages:
        if _SONG.search(m.message):
            b = int(m.time_in_seconds // window_s)
            if m.author not in users[b]:
                users[b].add(m.author)
                hits[b] += 1
                if len(samples[b]) < 6:
                    samples[b].append(m.message[:60])
    base = statistics.fmean(hits) if hits else 0
    sd = statistics.pstdev(hits) or 1.0
    order = sorted(range(n), key=lambda i: hits[i], reverse=True)
    picked: list[int] = []
    for i in order:
        if hits[i] < min_msgs or (hits[i] - base) / sd < 3:
            break
        if all(abs(i - j) * window_s > min_gap_s for j in picked):
            picked.append(i)
        if len(picked) >= top:
            break
    return [
        Peak(t=i * window_s + window_s / 2, score=round(10 + (hits[i] - base) / sd, 2),
             msgs=hits[i], unique=hits[i], reactions=0,
             samples=[f"(CANTANDO? {hits[i]} personas hablan de la canción)"] + samples[i])
        for i in picked
    ]
