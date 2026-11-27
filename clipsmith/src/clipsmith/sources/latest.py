"""Find each streamer's most recent FINISHED stream (optionally of a given game)."""

from __future__ import annotations

import json
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from .fetch import _UA, _YTDLP_BASE
from .live import _TWITCH_CLIENT_ID, _TWITCH_GQL


@dataclass
class PastStream:
    streamer: str
    platform: str
    url: str
    title: str
    game: str
    duration_s: float
    started_at: datetime
    dated: bool = True  # False: YouTube listing without a date (started_at is a guess)


def _matches(game_filter: str, title: str, game: str) -> bool:
    """'gta' = only GTA streams, '!gta' = everything except GTA, '' = any."""
    if game_filter.startswith("!"):
        return not _matches(game_filter[1:], title, game)
    if not game_filter:
        return True
    hay = f"{title} {game}".lower()
    g = game_filter.lower()
    return g in hay or (g == "gta" and ("grand theft auto" in hay or "gta" in hay))


def twitch_past(streamer: str, login: str, n: int = 5) -> list[PastStream]:
    q = """query($l:String!){ user(login:$l){ stream{ archiveVideo{ id } }
      videos(first:%d, type:ARCHIVE){ edges{ node{ id title lengthSeconds createdAt game{ name } } } } } }""" % n
    r = httpx.post(_TWITCH_GQL, json={"query": q, "variables": {"l": login}},
                   headers={"Client-ID": _TWITCH_CLIENT_ID}, timeout=30).json()
    u = (r.get("data") or {}).get("user") or {}
    recording = ((u.get("stream") or {}).get("archiveVideo") or {}).get("id")
    out = []
    for e in (u.get("videos") or {}).get("edges") or []:
        v = e["node"]
        if v["id"] == recording:  # still live
            continue
        out.append(PastStream(streamer, "twitch", f"https://www.twitch.tv/videos/{v['id']}",
                              v.get("title") or "", (v.get("game") or {}).get("name", ""),
                              float(v.get("lengthSeconds") or 0),
                              datetime.fromisoformat(v["createdAt"].replace("Z", "+00:00"))))
    return out


def kick_past(streamer: str, slug: str) -> list[PastStream]:
    vids = httpx.get(f"https://kick.com/api/v2/channels/{slug}/videos", headers=_UA, timeout=30).json()
    out = []
    for v in vids[:6]:
        if v.get("is_live") or not v.get("duration"):
            continue
        cats = v.get("categories") or []
        out.append(PastStream(streamer, "kick", f"https://kick.com/{slug}/videos/{v['video']['uuid']}",
                              v.get("session_title") or "", cats[0]["name"] if cats else "",
                              v["duration"] / 1000,
                              datetime.fromisoformat(v["start_time"].replace(" ", "T")).replace(
                                  tzinfo=UTC)))
    return out


def youtube_past(streamer: str, handle: str, n: int = 5) -> list[PastStream]:
    h = handle if handle.startswith("@") else f"@{handle}"
    out = subprocess.run(  # nosec B603
        [sys.executable, "-m", "yt_dlp", *_YTDLP_BASE, "--flat-playlist", "--playlist-end", str(n),
         "-J", f"https://www.youtube.com/{h}/streams"],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        return []
    res = []
    for e in json.loads(out.stdout).get("entries") or []:
        if e.get("live_status") != "was_live":
            continue
        ts = e.get("release_timestamp") or e.get("timestamp")
        started = datetime.fromtimestamp(ts, UTC) if ts else datetime.now(UTC)
        res.append(PastStream(streamer, "youtube", f"https://www.youtube.com/watch?v={e['id']}",
                              e.get("title") or "", "", float(e.get("duration") or 0), started,
                              dated=bool(ts)))
    return res


def latest_finished(entry: dict, game: str = "gta", min_hours: float = 0.5) -> PastStream | None:
    """Most recent finished stream across all of the streamer's channels."""
    found = recent_finished(entry, game=game, days=0, min_hours=min_hours)
    return found[0] if found else None


def recent_finished(entry: dict, game: str = "gta", days: float = 7,
                    min_hours: float = 0.5) -> list[PastStream]:
    """Finished streams of the last `days` days, newest first (days=0: only the latest)."""
    cands: list[PastStream] = []
    fns = {"twitch": twitch_past, "kick": kick_past, "youtube": youtube_past}
    for platform, handle in (entry.get("channels") or {}).items():
        if not handle or platform not in fns:
            continue
        try:
            cands += fns[platform](entry["name"], handle)
        except Exception as e:  # a network drop must not look like "no stream"
            print(f"  ! {entry['name']} {platform}: no se pudo listar ({type(e).__name__}: {e})",
                  file=sys.stderr)
            continue
    cands = [c for c in cands if _matches(game, c.title, c.game) and c.duration_s >= min_hours * 3600]
    # YouTube flat listings often lack exact dates: prefer longer, then newer
    cands.sort(key=lambda c: (c.started_at.date(), c.duration_s), reverse=True)
    if days <= 0:
        return cands[:1]
    since = datetime.now(UTC) - timedelta(days=days)
    out, seen = [], set()
    for i, c in enumerate(cands):
        key = (c.started_at.date(), round(c.duration_s / 600))  # same stream on two platforms
        if key in seen or not c.dated or (i and c.started_at < since):
            continue
        seen.add(key)
        out.append(c)
    return out
