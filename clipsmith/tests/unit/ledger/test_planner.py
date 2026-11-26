"""The planner must spread clips over time without repeating them everywhere at once."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from clipsmith.ledger.models import Base, Post, Video
from clipsmith.ledger.planner import TZ, plan

pytestmark = pytest.mark.unit


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _video(db, n: int, streamer: str, prio: float = 50.0, audience: str = "latam") -> Video:
    v = Video(folder=f"s/{n}_clip", streamer=streamer, stream="s", title=f"clip {n}",
              audience=audience, priority=prio)
    db.add(v)
    db.flush()
    return v


START = datetime(2026, 11, 22, 0, 0, tzinfo=TZ)


def test_same_clip_never_on_two_networks_close_together(db):
    for i in range(8):
        _video(db, i, f"streamer{i}")
    rows = plan(db, START, 1)
    by_clip: dict[int, list[datetime]] = {}
    for r in rows:
        by_clip.setdefault(r.video.id, []).append(r.at)
    for times in by_clip.values():
        times.sort()
        assert all(b - a >= timedelta(hours=30) for a, b in itertools.pairwise(times))


def test_streamer_limited_per_day(db):
    for i in range(12):
        _video(db, i, "OnlyOne")
    rows = plan(db, START, 1)
    per_day: dict = {}
    for r in rows:
        per_day.setdefault(r.at.date(), 0)
        per_day[r.at.date()] += 1
    assert all(n <= 3 for n in per_day.values())


def test_best_clip_goes_first(db):
    _video(db, 1, "a", prio=50)
    best = _video(db, 2, "b", prio=95)
    first = plan(db, START, 1)[0]
    assert first.video.id == best.id


def test_skipped_posts_do_not_block_slots_or_clips(db):
    v = _video(db, 1, "a")
    t = START + timedelta(hours=9)
    db.add(Post(video_id=v.id, platform="youtube", status="skipped", scheduled_at=t))
    db.flush()
    rows = plan(db, START, 0)
    # the skipped row is "taken" for youtube, but occupies no time on other networks
    assert not any(r.video.id == v.id and r.platform == "youtube" for r in rows)
    assert any(r.video.id == v.id for r in rows)


def test_published_time_beats_old_planned_time(db):
    v = _video(db, 1, "a")
    now = START - timedelta(hours=2)
    db.add(Post(video_id=v.id, platform="youtube", status="published",
                scheduled_at=START + timedelta(days=5), published_at=now))
    db.flush()
    rows = plan(db, START, 0)
    # published 2h before START on youtube -> other networks must wait the 30h gap
    assert not any(r.video.id == v.id and r.at - now < timedelta(hours=30) for r in rows)


def test_fresh_stream_beats_old_backlog(db):
    old = _video(db, 1, "OldGuy", prio=70)
    old.stream_at = START - timedelta(days=10)
    new = _video(db, 2, "NewGuy", prio=50)
    new.stream_at = START - timedelta(hours=6)
    db.flush()
    rows = plan(db, START, 1, platforms=["youtube"])
    assert rows[0].video.id == new.id


def test_freshness_fades_and_unknown_dates_are_neutral():
    from clipsmith.ledger.planner import FRESH_BONUS, _freshness

    v = Video(folder="x/y", streamer="a", priority=50)
    assert _freshness(v, START) == 0.0
    v.stream_at = START
    assert _freshness(v, START) == FRESH_BONUS
    v.stream_at = START - timedelta(hours=36)
    assert 0 < _freshness(v, START) < FRESH_BONUS
    v.stream_at = START - timedelta(days=8)
    assert _freshness(v, START) < 0
