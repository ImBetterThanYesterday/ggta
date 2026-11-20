"""Download video + chat from Twitch / Kick / YouTube into the pipeline layout."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess  # nosec B404
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from ..models.chat import HYPE_EMOTES, ChatLog, ChatMessage
from ..models.twitch import Clip, Video

log = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# Extra yt-dlp args shared by every call. YouTube needs a JS runtime (node) and, for
# age-restricted videos (most GTA streams), browser cookies: set
# CLIPSMITH_COOKIES_BROWSER=chrome (or firefox/safari/brave...).
_YTDLP_BASE = ["--no-warnings", "--js-runtimes", "node"]
if os.environ.get("CLIPSMITH_COOKIES_BROWSER"):
    _YTDLP_BASE += ["--cookies-from-browser", os.environ["CLIPSMITH_COOKIES_BROWSER"]]


@dataclass
class FetchResult:
    video: Video
    mp4_path: Path
    chat: ChatLog
    signal_clips: list[Clip] = field(default_factory=list)  # extra hard candidates
    platform: str = ""


# --------------------------------------------------------------------------- helpers


def detect_platform(url: str) -> str:
    u = url.lower()
    if "kick.com" in u:
        return "kick"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "twitch.tv" in u or re.fullmatch(r"\d{6,}", url.strip()):
        return "twitch"
    raise ValueError(f"Unsupported URL (expected Twitch, Kick or YouTube): {url}")


_KICK_EMOTE = re.compile(r"\[emote:\d+:([^\]]+)\]")


def _make_message(t: float, author: str, text: str) -> ChatMessage:
    text = _KICK_EMOTE.sub(r"\1", (text or "")).strip()
    return ChatMessage(
        time_in_seconds=float(t),
        message=text,
        author=author or "",
        is_clip_command=text.lower().startswith("!clip"),
        hype_emote_count=len(set(text.split()) & HYPE_EMOTES),
    )


def _fmt_hms(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _run_ytdlp(args: list[str]) -> None:
    cmd = [sys.executable, "-m", "yt_dlp", *_YTDLP_BASE, *args]
    log.info("running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)  # nosec B603 — args are built internally from the URL


_INFO_CACHE: dict[str, dict] = {}
_INFO_DIR = Path("work/meta")


def _ytdlp_info(url: str) -> dict:
    """yt-dlp -J, cached in memory and on disk.

    Long YouTube streams return ~900 MB of JSON (per-format fragment lists); calling it
    once per moment got rate-limited by YouTube. Fetch once, drop fragments, reuse.
    """
    if url in _INFO_CACHE:
        return _INFO_CACHE[url]
    import hashlib
    import tempfile
    import time

    _INFO_DIR.mkdir(parents=True, exist_ok=True)
    cache = _INFO_DIR / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".json")  # nosec B324
    if cache.exists() and time.time() - cache.stat().st_mtime < 6 * 3600:
        info = json.loads(cache.read_text(encoding="utf-8"))
        _INFO_CACHE[url] = info
        return info
    err = ""
    for attempt in range(3):
        with tempfile.TemporaryFile() as tmp:
            r = subprocess.run(  # nosec B603
                [sys.executable, "-m", "yt_dlp", *_YTDLP_BASE, "-J", "--no-playlist", url],
                stdout=tmp, stderr=subprocess.PIPE, text=False,
            )
            if r.returncode == 0:
                tmp.seek(0)
                info = json.load(tmp)
                for f in info.get("formats") or []:
                    f.pop("fragments", None)
                info.pop("requested_formats", None)
                cache.write_text(json.dumps(info), encoding="utf-8")
                _INFO_CACHE[url] = info
                return info
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        err = err[0]
        log.warning("yt-dlp -J failed (attempt %d): %s", attempt + 1, err)
        time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"yt-dlp no pudo leer {url}: {err}")


def _download_video(
    url: str, dest: Path, start_s: float, end_s: float, max_height: int, *, live: bool = False
) -> None:
    if dest.exists():
        log.info("video already downloaded: %s", dest)
        return
    args = [
        url,
        "--no-playlist",
        "-f",
        # Prefer H.264 + AAC: AV1/Opus merge badly into MP4 and decode slowly on CPU.
        f"bv*[height<={max_height}][vcodec^=avc1]+ba[ext=m4a]/"
        f"bv*[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b",
        "--merge-output-format",
        "mp4",
        "-o",
        str(dest),
        "--concurrent-fragments",
        "8",
    ]
    if start_s > 0 or end_s > 0:
        end = _fmt_hms(end_s) if end_s > 0 else "inf"
        # No --force-keyframes-at-cuts: it re-encodes everything (~25x slower). The cut
        # lands on the nearest keyframe (≤2 s off), negligible vs the 25 s clip pre-roll.
        args += ["--download-sections", f"*{_fmt_hms(start_s)}-{end}"]
        if live:  # YouTube DVR: needed to grab a past slice of a stream that is still live
            args.append("--live-from-start")
    _run_ytdlp(args)


_YT_LIVE_DVR_S = 14 * 60  # YouTube live HLS keeps ~15 min (450 x 2 s segments)


def _download_youtube_live(url: str, dest: Path, seconds: float, max_height: int) -> float:
    """Grab the last `seconds` of a YouTube stream that is still live, from its HLS DVR.

    DASH (--live-from-start) can't be partially downloaded, but the HLS playlists can be
    started N segments back with ffmpeg's -live_start_index. Returns seconds captured.
    """
    if dest.exists():
        return seconds
    seconds = min(seconds, _YT_LIVE_DVR_S)
    fmt = (
        f"bv*[height<={max_height}][protocol^=m3u8][vcodec^=avc1]+ba[protocol^=m3u8]/"
        f"b[protocol^=m3u8][height<={max_height}]"
    )
    urls = subprocess.run(  # nosec B603
        [sys.executable, "-m", "yt_dlp", *_YTDLP_BASE, "-g", "-f", fmt, url],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    back = -int(seconds // 2) - 1  # segments are 2 s
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for u in urls[:2]:
        cmd += ["-live_start_index", str(back), "-i", u]
    if len(urls) >= 2:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    cmd += ["-t", str(int(seconds)), "-c", "copy", "-movflags", "+faststart", str(dest)]
    log.info("youtube live: capturing last %ds from HLS DVR", seconds)
    subprocess.run(cmd, check=True)  # nosec B603
    return seconds


def _download_youtube_postlive(info: dict, dest: Path, start_s: float, end_s: float,
                               max_height: int) -> None:
    """Slice of a stream that just ended but YouTube hasn't processed yet ("post_live").

    Its DASH formats are a sequence of 2 s fMP4 segments addressed by `&sq=N`;
    ffmpeg/yt-dlp can't seek into them, so fetch exactly the needed segments.
    """
    if dest.exists():
        return
    fmts = info.get("formats") or []
    vids = [f for f in fmts if f.get("vcodec", "none") != "none" and f.get("acodec") in (None, "none")
            and (f.get("height") or 0) <= max_height and str(f.get("vcodec", "")).startswith("avc1")]
    auds = [f for f in fmts if f.get("vcodec") == "none" and f.get("ext") == "m4a"
            and "drc" not in str(f.get("format_id"))]
    if not vids or not auds:
        raise RuntimeError("post_live: no H.264/m4a DASH formats")
    v = max(vids, key=lambda f: ((f.get("height") or 0), f.get("tbr") or 0))
    a = max(auds, key=lambda f: f.get("abr") or 0)
    seg = float(v.get("target_duration") or 2)
    first, last = int(start_s // seg), int(end_s // seg) + 1
    headers = v.get("http_headers") or {}

    def grab(fmt: dict, out: Path) -> None:
        def one(sq: int) -> bytes:
            for attempt in range(4):
                r = httpx.get(f"{fmt['url']}&sq={sq}", headers=headers, timeout=60,
                              follow_redirects=True)
                if r.status_code == 200:
                    return r.content
                time.sleep(2 * (attempt + 1))
            raise RuntimeError(f"post_live: segment {sq} -> HTTP {r.status_code}")

        with ThreadPoolExecutor(6) as ex:
            parts = list(ex.map(one, range(first, last)))
        out.write_bytes(b"".join(parts))

    tmp_v, tmp_a = dest.with_suffix(".v.mp4"), dest.with_suffix(".a.m4a")
    log.info("youtube post_live: %d segments of %ss (%s + %s)", last - first, seg,
             v["format_id"], a["format_id"])
    grab(v, tmp_v)
    grab(a, tmp_a)
    subprocess.run(  # nosec B603
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp_v), "-i", str(tmp_a),
         "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-avoid_negative_ts", "make_zero",
         "-movflags", "+faststart", str(dest)],
        check=True,
    )
    tmp_v.unlink(missing_ok=True)
    tmp_a.unlink(missing_ok=True)


def _copy(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [ChatMessage(**vars(m)) for m in messages]


def _window(messages: list[ChatMessage], start_s: float, end_s: float) -> list[ChatMessage]:
    """Keep messages inside [start_s, end_s] and shift them so start_s becomes t=0."""
    out = []
    for m in messages:
        if m.time_in_seconds < start_s or (end_s > 0 and m.time_in_seconds > end_s):
            continue
        m.time_in_seconds -= start_s
        out.append(m)
    return out


# --------------------------------------------------------------------------- Kick

_KICK_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class _RateLimiter:
    """Global pacing shared by all threads (Kick blocks bursts with HTTP 429)."""

    def __init__(self, per_second: float) -> None:
        self.interval = 1.0 / per_second
        self.next_t = time.monotonic()
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next_t)
            self.next_t = t + self.interval
        time.sleep(max(0.0, t - now))

    def penalize(self, seconds: float) -> None:
        with self.lock:
            self.next_t = max(self.next_t, time.monotonic() + seconds)


def _kick_chat(
    channel_id: int,
    stream_start: datetime,
    start_s: float,
    end_s: float,
    workers: int = 3,
    per_second: float = 4.0,
) -> list[ChatMessage]:
    """Kick's replay endpoint returns ~5 s of chat per call; walk the range in 5 s steps.

    ~4 req/s is what Kick tolerates without blocking: a 1 h slice takes ~3 min.
    """
    step = 5
    offsets = list(range(int(start_s), int(end_s), step))
    base = "https://kick.com/api/v2/channels/{}/messages?start_time={}"
    limiter = _RateLimiter(per_second)
    local = threading.local()

    def fetch(off: int) -> list[ChatMessage]:
        ts = (stream_start + timedelta(seconds=off)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if not hasattr(local, "client"):
            local.client = httpx.Client(headers=_UA, timeout=20)
        for attempt in range(8):
            limiter.wait()
            try:
                r = local.client.get(base.format(channel_id, ts))
                if r.status_code == 429:
                    limiter.penalize(5 * (attempt + 1))  # back off everyone
                    continue
                r.raise_for_status()
                msgs = (r.json().get("data") or {}).get("messages") or []
                out = []
                for m in msgs:
                    t = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00"))
                    rel = (t - stream_start).total_seconds()
                    if off <= rel < off + step:  # avoid duplicates between windows
                        sender = (m.get("sender") or {}).get("username", "")
                        out.append(_make_message(rel, sender, m.get("content", "")))
                return out
            except (httpx.HTTPError, ValueError):
                time.sleep(1 + attempt)
        log.warning("kick chat: giving up on offset %ss", off)
        return []

    messages: list[ChatMessage] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, chunk in enumerate(ex.map(fetch, offsets)):
            messages.extend(chunk)
            if i and i % 500 == 0:
                log.info("kick chat: %d/%d windows, %d msgs", i, len(offsets), len(messages))
    messages.sort(key=lambda m: m.time_in_seconds)
    return messages


def _fetch_kick(
    url: str, work_dir: Path, start_s: float, end_s: float, max_height: int,
    suffix: str = "", chat_messages: list[ChatMessage] | None = None,
):
    m = _KICK_UUID.search(url)
    if not m:
        raise ValueError("Kick URL must contain the video UUID, e.g. kick.com/<canal>/videos/<uuid>")
    uuid = m.group(0)
    meta = httpx.get(f"https://kick.com/api/v1/video/{uuid}", headers=_UA, timeout=30).json()
    ls = meta.get("livestream") or {}
    duration_s = (ls.get("duration") or 0) / 1000
    stream_start = datetime.fromisoformat(ls["start_time"]).astimezone(UTC)
    channel_id = ls["channel_id"]
    channel = (ls.get("channel") or {}).get("slug", "kick")
    end = end_s if end_s > 0 else duration_s

    video_id = f"kick_{uuid[:8]}{suffix}"
    vod_dir = work_dir / video_id
    vod_dir.mkdir(parents=True, exist_ok=True)
    mp4 = vod_dir / f"{video_id}.mp4"

    chat_path = vod_dir / "chat.json"
    if chat_path.exists():
        chat = ChatLog.from_json(chat_path.read_text(encoding="utf-8"))
    elif chat_messages is not None:  # chat already downloaded for the whole stream
        chat = ChatLog(video_id=video_id, messages=_window(_copy(chat_messages), start_s, end_s))
        chat_path.write_text(chat.to_json(), encoding="utf-8")
    else:
        log.info("kick chat: downloading %s -> %s", _fmt_hms(start_s), _fmt_hms(end))
        msgs = _kick_chat(channel_id, stream_start, start_s, end)
        chat = ChatLog(video_id=video_id, messages=_window(msgs, start_s, end_s))
        chat_path.write_text(chat.to_json(), encoding="utf-8")

    # yt-dlp's Kick extractor is flaky; the API exposes the HLS master playlist directly.
    src = meta.get("source") or f"https://kick.com/{channel}/videos/{uuid}"
    _download_video(src, mp4, start_s, end_s, max_height)
    video = Video(
        id=video_id,
        user_id="",
        user_login=channel,
        title=ls.get("session_title", video_id),
        created_at=ls.get("start_time", ""),
        published_at="",
        url=url,
        duration=f"{int(duration_s)}s",
        type="archive",
    )
    return video, mp4, chat, []


# --------------------------------------------------------------------------- YouTube


def _parse_youtube_live_chat(path: Path) -> list[ChatMessage]:
    """Parse yt-dlp's .live_chat.json (one JSON object per line)."""
    out: list[ChatMessage] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        replay = d.get("replayChatItemAction") or {}
        try:
            t = int(replay.get("videoOffsetTimeMsec", 0)) / 1000
        except (TypeError, ValueError):
            continue
        for action in replay.get("actions", []):
            item = (action.get("addChatItemAction") or {}).get("item") or {}
            r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
            if not r:
                continue
            runs = (r.get("message") or {}).get("runs", [])
            text = "".join(
                x.get("text")
                or ((x.get("emoji") or {}).get("shortcuts") or [""])[0]
                for x in runs
            )
            author = (r.get("authorName") or {}).get("simpleText", "")
            out.append(_make_message(t, author, text))
    out.sort(key=lambda m: m.time_in_seconds)
    return out


def _heatmap_clips(info: dict, start_s: float, end_s: float, top: int = 8) -> list[Clip]:
    """YouTube 'Most replayed' graph -> top peaks as hard candidates."""
    heat = info.get("heatmap") or []
    if not heat:
        return []
    vals = sorted(h["value"] for h in heat)
    median = vals[len(vals) // 2]
    peaks = [
        h for h in heat
        if h["value"] > max(median * 1.5, 0.25)
        and h["start_time"] >= start_s
        and (end_s <= 0 or h["end_time"] <= end_s)
    ]
    peaks.sort(key=lambda h: h["value"], reverse=True)
    clips = []
    for h in peaks[:top]:
        center = (h["start_time"] + h["end_time"]) / 2 - start_s
        clips.append(
            Clip(
                id=f"most_replayed_{int(center)}",
                url="",
                title=f"YouTube 'most replayed' peak (value={h['value']:.2f})",
                creator_name="youtube_heatmap",
                video_id=info.get("id", ""),
                vod_offset=int(center),
                duration=h["end_time"] - h["start_time"],
                view_count=int(h["value"] * 1000),
                created_at="",
            )
        )
    return clips


def _fetch_youtube(
    url: str, work_dir: Path, start_s: float, end_s: float, max_height: int,
    suffix: str = "", chat_messages: list[ChatMessage] | None = None,
):
    info = _ytdlp_info(url)
    video_id = f"yt_{info['id']}{suffix}"
    is_live = info.get("live_status") == "is_live"
    vod_dir = work_dir / video_id
    vod_dir.mkdir(parents=True, exist_ok=True)
    mp4 = vod_dir / f"{video_id}.mp4"

    chat_path = vod_dir / "chat.json"
    if chat_path.exists():
        chat = ChatLog.from_json(chat_path.read_text(encoding="utf-8"))
    elif chat_messages is not None:  # chat already downloaded for the whole stream
        chat = ChatLog(video_id=video_id, messages=_window(_copy(chat_messages), start_s, end_s))
        chat_path.write_text(chat.to_json(), encoding="utf-8")
    else:
        msgs: list[ChatMessage] = []
        if is_live:
            # The live_chat "subtitle" of a stream that is still live follows the chat in
            # real time and never ends; and past messages aren't available until it ends.
            log.info("youtube: stream is live -> no chat replay yet (using audio/transcript)")
        elif info.get("was_live") or "live_chat" in (info.get("subtitles") or {}):
            log.info("youtube: downloading live chat replay")
            try:
                _run_ytdlp([
                    url, "--no-playlist", "--skip-download", "--write-subs",
                    "--sub-langs", "live_chat", "-o", str(vod_dir / "yt"),
                ])
                lc = vod_dir / "yt.live_chat.json"
                if lc.exists():
                    msgs = _parse_youtube_live_chat(lc)
            except subprocess.CalledProcessError:
                log.warning("youtube: live chat replay not available")
        else:
            log.info("youtube: not a past livestream -> no chat (using 'most replayed' instead)")
        chat = ChatLog(video_id=video_id, messages=_window(msgs, start_s, end_s))
        chat_path.write_text(chat.to_json(), encoding="utf-8")

    if is_live:
        # Past slices of a live stream: only the recent HLS DVR window is reachable.
        want = (end_s - start_s) if end_s > start_s else 600
        _download_youtube_live(url, mp4, want, max_height)
    elif info.get("live_status") == "post_live":
        _download_youtube_postlive(info, mp4, start_s, end_s, max_height)
    else:
        _download_video(url, mp4, start_s, end_s, max_height)
    video = Video(
        id=video_id,
        user_id="",
        user_login=info.get("channel") or info.get("uploader") or "youtube",
        title=info.get("title", video_id),
        created_at=info.get("upload_date", ""),
        published_at="",
        url=url,
        duration=f"{int(info.get('duration') or 0)}s",
        type="archive",
    )
    return video, mp4, chat, _heatmap_clips(info, start_s, end_s)


# --------------------------------------------------------------------------- Twitch


def _twitch_chat_range(vod: str, start_s: float, end_s: float) -> list[ChatMessage]:
    """Twitch GQL chat replay from start_s to end_s (works on in-progress VODs too)."""
    from ..twitch.chat import _CLIENT_ID, _COMMENTS_HASH, _GQL_URL

    out: list[ChatMessage] = []
    cursor: str | None = None
    with httpx.Client(timeout=30, headers={"Client-ID": _CLIENT_ID}) as c:
        while True:
            variables: dict = {"videoID": vod}
            if cursor:
                variables["cursor"] = cursor
            else:
                variables["contentOffsetSeconds"] = int(start_s)
            payload = [{
                "operationName": "VideoCommentsByOffsetOrCursor",
                "variables": variables,
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": _COMMENTS_HASH}},
            }]
            body = c.post(_GQL_URL, json=payload).json()
            comments = ((body[0].get("data") or {}).get("video") or {}).get("comments") or {}
            last_t = start_s
            for e in comments.get("edges") or []:
                cursor = e.get("cursor")
                n = e.get("node") or {}
                t = n.get("contentOffsetSeconds")
                if t is None:
                    continue
                last_t = float(t)
                text = "".join(f.get("text") or "" for f in (n.get("message") or {}).get("fragments") or [])
                author = (n.get("commenter") or {}).get("login", "")
                out.append(_make_message(last_t, author, text))
            if not comments.get("pageInfo", {}).get("hasNextPage") or (end_s > 0 and last_t > end_s):
                break
    return out


def _fetch_twitch(
    url: str, work_dir: Path, start_s: float, end_s: float, max_height: int,
    suffix: str = "", chat_messages: list[ChatMessage] | None = None,
):
    m = re.search(r"(\d{6,})", url)
    if not m:
        raise ValueError("Twitch URL must be a VOD, e.g. twitch.tv/videos/123456789")
    vod = m.group(1)
    video_id = f"{vod}{suffix}"
    vod_dir = work_dir / video_id
    vod_dir.mkdir(parents=True, exist_ok=True)
    mp4 = vod_dir / f"{video_id}.mp4"

    chat_path = vod_dir / "chat.json"
    if chat_path.exists():
        chat = ChatLog.from_json(chat_path.read_text(encoding="utf-8"))
    elif chat_messages is not None:  # chat already downloaded for the whole stream
        chat = ChatLog(video_id=video_id, messages=_window(_copy(chat_messages), start_s, end_s))
        chat_path.write_text(chat.to_json(), encoding="utf-8")
    else:
        full = _twitch_chat_range(vod, start_s, end_s)
        chat = ChatLog(video_id=video_id, messages=_window(full.messages, start_s, end_s))
        chat_path.write_text(chat.to_json(), encoding="utf-8")

    info = _ytdlp_info(f"https://www.twitch.tv/videos/{vod}")
    _download_video(f"https://www.twitch.tv/videos/{vod}", mp4, start_s, end_s, max_height)
    video = Video(
        id=video_id,
        user_id="",
        user_login=info.get("uploader_id") or info.get("uploader") or "twitch",
        title=info.get("title", vod),
        created_at="",
        published_at="",
        url=url,
        duration=f"{int(info.get('duration') or 0)}s",
        type="archive",
    )
    return video, mp4, chat, []


# --------------------------------------------------------------------------- entry point


def fetch_source(
    url: str,
    work_dir: Path,
    *,
    start_s: float = 0.0,
    end_s: float = 0.0,
    max_height: int = 1080,
    id_suffix: str = "",
    chat_messages: list[ChatMessage] | None = None,
) -> FetchResult:
    """Download video + chat for a Twitch/Kick/YouTube URL.

    start_s/end_s download only that slice of the stream (recommended for long
    streams). All timestamps in the result are relative to start_s.
    """
    platform = detect_platform(url)
    fn = {"kick": _fetch_kick, "youtube": _fetch_youtube, "twitch": _fetch_twitch}[platform]
    video, mp4, chat, clips = fn(url, work_dir.expanduser(), start_s, end_s, max_height, id_suffix, chat_messages)
    return FetchResult(
        video=video, mp4_path=mp4, chat=chat, signal_clips=clips, platform=platform
    )
