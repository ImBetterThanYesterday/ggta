"""Engine/session for the ledger. Reads GGTA_DATABASE_URL (env or clipsmith/.env)."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import Base


def _env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    env = Path(".env")
    if not val and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
    return val


def database_url() -> str:
    return _env("GGTA_DATABASE_URL") or f"sqlite:///{Path('work/ledger.db').resolve()}"


def make_engine() -> Engine:
    url = database_url()
    engine = create_engine(url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Tiny forward-only migration: add columns introduced after the table was created."""
    have = {c["name"] for c in inspect(engine).get_columns("video")}
    ts = "TIMESTAMP WITH TIME ZONE" if engine.dialect.name == "postgresql" else "DATETIME"
    with engine.begin() as conn:
        if "stream_at" not in have:
            conn.execute(text(f"ALTER TABLE video ADD COLUMN stream_at {ts}"))


@contextmanager
def session():
    engine = make_engine()
    with Session(engine, expire_on_commit=False) as s:
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
