"""Spread the backlog over time without posting the same clip everywhere at once.

Rules (all editable below):
  * each network has its own posting hours (America/Bogota);
  * the same clip never lands on two networks within GAP_HOURS of each other, so each
    network gets different content on a given day and the clip reaches the next one later;
  * a streamer doesn't repeat on the same network within STREAMER_GAP_HOURS, nor on any network
    within STREAMER_ANY_GAP_HOURS, and has at most STREAMER_DAILY_CAP posts a day;
  * audiences prefer different hours (Spain, LATAM, US) -> soft bonus/penalty;
  * fresh clips first: a clip from a stream that aired in the last FRESH_HOURS gets up to
    FRESH_BONUS extra points (fading to 0), and clips older than STALE_DAYS lose STALE_PENALTY;
  * the best clips go first, and the network that gets them first rotates every day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PLATFORMS, Post, Video

TZ = ZoneInfo("America/Bogota")

PLATFORM_SLOTS: dict[str, list[str]] = {
    "tiktok": ["11:00", "13:00", "17:30", "21:00"],
    "youtube": ["09:00", "11:00", "13:00", "17:30", "21:00"],
    "instagram": ["09:00", "11:00", "13:00", "17:30", "20:00"],
    "facebook": ["09:00", "11:00", "13:00", "17:30", "20:00"],
}
GAP_HOURS = 30.0          # same clip, different network
STREAMER_GAP_HOURS = 8.0  # same streamer, same network
STREAMER_ANY_GAP_HOURS = 3.0   # same streamer on ANY network: keep them apart
STREAMER_DAILY_CAP = 3         # max posts of one streamer per day, all networks together
SLOT_CLEARANCE_MIN = 75   # don't plan within this many minutes of another post on the network

FRESH_BONUS = 40.0        # GTA 6 clips are news: yesterday's stream beats last week's
FRESH_HOURS = 72.0        # the bonus fades linearly to 0 over this many hours after the stream
STALE_DAYS = 7.0          # clips older than this only fill gaps
STALE_PENALTY = 15.0

# Bogota hours when each audience is awake (Spain = Bogota + 6h, US east = Bogota + 1h)
AUDIENCE_WINDOWS = {"spain": (9.0, 14.0), "latam": (11.0, 22.0), "us": (17.0, 22.0)}
SPAIN_BASED = {"auronplay", "rubius", "thegrefg", "vegetta777", "robleis", "illojuan"}


def audience_for(streamer: str, lang: str) -> str:
    if lang == "en":
        return "us"
    return "spain" if streamer.lower() in SPAIN_BASED else "latam"


def _at(day: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=TZ)


def _when(p: Post) -> datetime | None:
    """When a post occupies the calendar. Skipped/failed posts occupy nothing."""
    if p.status in ("skipped", "failed"):
        return None
    when = p.published_at or p.scheduled_at
    if when is not None and when.tzinfo is None:  # SQLite returns naive datetimes
        when = when.replace(tzinfo=UTC)
    return when


def _fit(video: Video, slot: datetime) -> float:
    lo, hi = AUDIENCE_WINDOWS.get(video.audience, (9.0, 22.0))
    h = slot.hour + slot.minute / 60
    return 15.0 if lo <= h < hi else -10.0


def _freshness(video: Video, slot: datetime) -> float:
    """Bonus for clips of recent streams, judged at the moment the post would go out."""
    aired = video.stream_at
    if aired is None:
        return 0.0
    if aired.tzinfo is None:  # SQLite returns naive datetimes
        aired = aired.replace(tzinfo=UTC)
    age_h = max(0.0, (slot - aired).total_seconds() / 3600)
    if age_h < FRESH_HOURS:
        return FRESH_BONUS * (1 - age_h / FRESH_HOURS)
    return -STALE_PENALTY if age_h > STALE_DAYS * 24 else 0.0


@dataclass
class Planned:
    video: Video
    platform: str
    at: datetime


def plan(db: Session, start: datetime, days: int, platforms: list[str] | None = None) -> list[Planned]:
    platforms = [p for p in (platforms or PLATFORMS) if p in PLATFORM_SLOTS]
    videos = list(db.scalars(select(Video).order_by(Video.priority.desc(), Video.id)))
    posts: list[Post] = list(db.scalars(select(Post)))
    by_video: dict[int, list[Post]] = {}
    by_platform: dict[str, list[Post]] = {p: [] for p in PLATFORMS}
    for p in posts:
        by_video.setdefault(p.video_id, []).append(p)
        by_platform.setdefault(p.platform, []).append(p)
    taken = {(p.video_id, p.platform) for p in posts}
    streamer_of = {v.id: v.streamer.lower() for v in videos}
    by_streamer: dict[str, list[Post]] = {}
    for p in posts:
        by_streamer.setdefault(streamer_of.get(p.video_id, ""), []).append(p)

    # chronological slots; platform order rotates daily so the best clips alternate networks
    slots: list[tuple[datetime, str]] = []
    first_day = start.astimezone(TZ).date()
    for d in range(days + 1):
        day = first_day + timedelta(days=d)
        rot = d % len(platforms)
        order = platforms[rot:] + platforms[:rot]
        for hhmm in sorted({s for p in platforms for s in PLATFORM_SLOTS[p]}, key=lambda s: _at(day, s)):
            for p in order:
                if hhmm in PLATFORM_SLOTS[p]:
                    t = _at(day, hhmm)
                    if t > start:
                        slots.append((t, p))
    slots.sort(key=lambda x: x[0])

    out: list[Planned] = []
    for t, platform in slots:
        near = [
            q for q in by_platform[platform]
            if _when(q) and abs((_when(q) - t).total_seconds()) < SLOT_CLEARANCE_MIN * 60
        ]
        if near:
            continue
        best: tuple[float, Video] | None = None
        for v in videos:
            if (v.id, platform) in taken:
                continue
            if any(
                _when(q) and q.platform != platform
                and abs((_when(q) - t).total_seconds()) < GAP_HOURS * 3600
                for q in by_video.get(v.id, [])
            ):
                continue
            if any(
                streamer_of.get(q.video_id) == streamer_of[v.id] and _when(q)
                and abs((_when(q) - t).total_seconds()) < STREAMER_GAP_HOURS * 3600
                for q in by_platform[platform]
            ):
                continue
            mine = by_streamer.get(streamer_of[v.id], [])
            if any(_when(q) and abs((_when(q) - t).total_seconds()) < STREAMER_ANY_GAP_HOURS * 3600
                   for q in mine):
                continue
            if sum(1 for q in mine if _when(q) and _when(q).astimezone(TZ).date() == t.date()) \
                    >= STREAMER_DAILY_CAP:
                continue
            score = v.priority + _fit(v, t) + _freshness(v, t)
            if best is None or score > best[0]:
                best = (score, v)
        if best is None:
            continue
        v = best[1]
        post = Post(video_id=v.id, platform=platform, status="planned", scheduled_at=t)
        db.add(post)
        taken.add((v.id, platform))
        by_video.setdefault(v.id, []).append(post)
        by_platform[platform].append(post)
        by_streamer.setdefault(streamer_of[v.id], []).append(post)
        out.append(Planned(v, platform, t))
    db.flush()
    return out
