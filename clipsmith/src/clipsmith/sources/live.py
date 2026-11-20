"""Live status for Kick / Twitch / YouTube channels (no API keys needed)."""

from __future__ import annotations

import json
import subprocess  # nosec B404
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from .fetch import _UA, _YTDLP_BASE

_TWITCH_GQL = "https://gql.twitch.tv/gql"
_TWITCH_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"  # public web client id (same as twitch/chat.py)


@dataclass
class ChannelStatus:
    streamer: str
    platform: str
    handle: str
    exists: bool = True
    live: bool = False
    title: str = ""
    game: str = ""
    viewers: int = 0
    followers: int = 0
    started_at: str = ""  # ISO-8601 UTC
    url: str = ""  # URL to pass to `clipsmith url` / live mode
    error: str = ""

    @property
    def elapsed_s(self) -> float:
        if not self.started_at:
            return 0.0
        start = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        return (datetime.now(UTC) - start).total_seconds()

    def to_dict(self) -> dict:
        return asdict(self) | {"elapsed_s": round(self.elapsed_s)}


# --------------------------------------------------------------------------- Kick


def kick_status(streamer: str, slug: str) -> ChannelStatus:
    st = ChannelStatus(streamer, "kick", slug)
    try:
        r = httpx.get(f"https://kick.com/api/v2/channels/{slug}", headers=_UA, timeout=20)
        if r.status_code == 404:
            st.exists = False
            return st
        r.raise_for_status()
        d = r.json()
        st.followers = d.get("followers_count") or 0
        ls = d.get("livestream")
        if ls and ls.get("is_live", True):
            st.live = True
            st.title = ls.get("session_title", "")
            st.viewers = ls.get("viewer_count") or 0
            cats = ls.get("categories") or []
            st.game = cats[0]["name"] if cats else ""
            start = ls.get("start_time") or ls.get("created_at") or ""
            if start:
                st.started_at = (
                    datetime.fromisoformat(start.replace(" ", "T")).replace(tzinfo=UTC).isoformat()
                )
            # The in-progress VOD (grows while live) is what we clip from.
            vids = httpx.get(
                f"https://kick.com/api/v2/channels/{slug}/videos", headers=_UA, timeout=20
            ).json()
            for v in vids:
                if v.get("id") == ls.get("id") or v.get("is_live"):
                    st.url = f"https://kick.com/{slug}/videos/{v['video']['uuid']}"
                    break
            if not st.url and vids:
                st.url = f"https://kick.com/{slug}/videos/{vids[0]['video']['uuid']}"
    except Exception as exc:
        st.error = str(exc)[:200]
    return st


# --------------------------------------------------------------------------- Twitch


def twitch_status_many(pairs: list[tuple[str, str]]) -> list[ChannelStatus]:
    """One GQL call for all Twitch logins. pairs = [(streamer, login)]."""
    logins = [login.lower() for _, login in pairs]
    query = """query($logins:[String!]) { users(logins:$logins) {
        login followers { totalCount }
        stream { id title viewersCount createdAt game { name } archiveVideo { id } }
        videos(first:1, type:ARCHIVE) { edges { node { id } } } } }"""
    out = {login: ChannelStatus(s, "twitch", login) for s, login in pairs}
    try:
        r = httpx.post(
            _TWITCH_GQL,
            json={"query": query, "variables": {"logins": logins}},
            headers={"Client-ID": _TWITCH_CLIENT_ID},
            timeout=30,
        )
        r.raise_for_status()
        users = (r.json().get("data") or {}).get("users") or []
        found = set()
        for u in users:
            if not u:
                continue
            st = out[u["login"].lower()]
            found.add(u["login"].lower())
            st.followers = (u.get("followers") or {}).get("totalCount") or 0
            s = u.get("stream")
            if s:
                st.live = True
                st.title = s.get("title") or ""
                st.viewers = s.get("viewersCount") or 0
                st.game = (s.get("game") or {}).get("name", "")
                st.started_at = s.get("createdAt") or ""
                vod = (s.get("archiveVideo") or {}).get("id")
                if vod:
                    st.url = f"https://www.twitch.tv/videos/{vod}"
            if not st.url:
                edges = (u.get("videos") or {}).get("edges") or []
                if edges:
                    st.url = f"https://www.twitch.tv/videos/{edges[0]['node']['id']}"
        for login, st in out.items():
            if login not in found:
                st.exists = False
    except Exception as exc:
        for st in out.values():
            st.error = str(exc)[:200]
    return list(out.values())


# --------------------------------------------------------------------------- YouTube


def youtube_status(streamer: str, handle: str) -> ChannelStatus:
    h = handle if handle.startswith("@") else f"@{handle}"
    st = ChannelStatus(streamer, "youtube", h)
    cmd = [sys.executable, "-m", "yt_dlp", *_YTDLP_BASE, "-J", "--no-playlist",
           f"https://www.youtube.com/{h}/live"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)  # nosec B603
        if p.returncode != 0:
            err = p.stderr.lower()
            if "does not exist" in err or "404" in err:
                st.exists = False
            elif "not currently live" not in err and "will begin" not in err:
                st.error = p.stderr.strip().splitlines()[-1][:200] if p.stderr.strip() else "error"
            return st
        d = json.loads(p.stdout)
        st.followers = d.get("channel_follower_count") or 0
        if d.get("live_status") == "is_live":
            st.live = True
            st.title = d.get("title", "")
            st.viewers = d.get("concurrent_view_count") or 0
            st.game = ""  # YouTube doesn't expose the game reliably; check title
            ts = d.get("release_timestamp") or d.get("timestamp")
            if ts:
                st.started_at = datetime.fromtimestamp(ts, UTC).isoformat()
            st.url = f"https://www.youtube.com/watch?v={d['id']}"
    except Exception as exc:
        st.error = str(exc)[:200]
    return st


# --------------------------------------------------------------------------- roster


def load_roster(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("streamers", [])


def check_roster(roster: list[dict], *, workers: int = 8) -> list[ChannelStatus]:
    jobs, twitch_pairs = [], []
    for s in roster:
        name = s["name"]
        for platform, handle in (s.get("channels") or {}).items():
            if not handle:
                continue
            if platform == "twitch":
                twitch_pairs.append((name, handle))
            elif platform == "kick":
                jobs.append((kick_status, name, handle))
            elif platform == "youtube":
                jobs.append((youtube_status, name, handle))
    results: list[ChannelStatus] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fn, n, h) for fn, n, h in jobs]
        for i in range(0, len(twitch_pairs), 35):  # GQL batch size
            futs.append(ex.submit(twitch_status_many, twitch_pairs[i : i + 35]))
        for f in futs:
            r = f.result()
            results.extend(r if isinstance(r, list) else [r])
    return results
