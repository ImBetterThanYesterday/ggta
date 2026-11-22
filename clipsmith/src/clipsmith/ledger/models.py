"""Tables of the publishing ledger (Postgres in production, SQLite as a fallback)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

PLATFORMS = ("tiktok", "youtube", "instagram", "facebook")
# planned -> scheduled -> published ; skipped / failed are terminal
STATUSES = ("planned", "scheduled", "published", "skipped", "failed")


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Video(Base):
    __tablename__ = "video"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    folder: Mapped[str] = mapped_column(String(300), unique=True)  # relative to out/
    streamer: Mapped[str] = mapped_column(String(80), default="")
    stream: Mapped[str] = mapped_column(String(200), default="")
    lang: Mapped[str] = mapped_column(String(8), default="es")
    audience: Mapped[str] = mapped_column(String(12), default="latam")  # spain | latam | us
    title: Mapped[str] = mapped_column(String(200), default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    priority: Mapped[float] = mapped_column(Float, default=50.0)  # higher = post sooner
    tags: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    posts: Mapped[list[Post]] = relationship(back_populates="video", cascade="all, delete-orphan")


class Post(Base):
    """One clip on one network. A clip has at most one post per network."""

    __tablename__ = "post"
    __table_args__ = (UniqueConstraint("video_id", "platform", name="uq_video_platform"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("video.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(12), default="planned")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    method: Mapped[str] = mapped_column(String(24), default="")  # chrome | upload-post | meta-suite
    url: Mapped[str] = mapped_column(Text, default="")
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    video: Mapped[Video] = relationship(back_populates="posts")
