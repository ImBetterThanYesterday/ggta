"""`clip ledger` — control panel for every clip: what exists, where it went, what's next."""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from ..ledger.db import database_url, session
from ..ledger.models import PLATFORMS, STATUSES, Post, Video
from ..ledger.planner import TZ, audience_for, plan

console = Console()
ledger_app = typer.Typer(help="Control de videos y publicaciones (Postgres).", no_args_is_help=True)
_SONG = re.compile(r"temazo|canta|cantando|song|sing|banger|cancion|canción|musica", re.IGNORECASE)


def _fmt(dt: datetime | None) -> str:
    return dt.astimezone(TZ).strftime("%a %d %H:%M") if dt else "—"


def _parse_when(text: str) -> datetime:
    """'2026-11-22 13:00' (Bogota) -> aware datetime."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


def _roster_langs(path: Path) -> dict[str, str]:
    for p in (path, Path("streamers.yaml")):
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            return {s["name"].lower(): s.get("lang", "es") for s in data.get("streamers", [])}
    return {}


def _duration(mp4: Path) -> float:
    r = subprocess.run(  # nosec B603 B607
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp4)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _find(db, needle: str) -> Video:
    hits = [v for v in db.scalars(select(Video)) if needle.lower() in v.folder.lower()]
    if len(hits) != 1:
        raise typer.BadParameter(f"'{needle}' coincide con {len(hits)} clips: "
                                 + ", ".join(h.folder for h in hits[:5]))
    return hits[0]


def _sync_publicado(out: Path, video: Video) -> None:
    """Keep out/<clip>/publicado.json (used by `clip publish`) in step with the ledger."""
    f = out / video.folder / "publicado.json"
    d = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    live = {p.platform for p in video.posts if p.status in ("scheduled", "published")}
    d["ok"] = sorted(set(d.get("ok", [])) | live)
    f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


@ledger_app.command("sync")
def sync_cmd(
    out: Path = typer.Option(Path("out"), "--out"),
    roster: Path = typer.Option(Path("../streamers.yaml"), "--roster"),
) -> None:
    """Register every out/<stream>/<clip>/ and import what publicado.json already says."""
    langs = _roster_langs(roster)
    new = 0
    with session() as db:
        for mp4 in sorted(out.glob("*/*/video.mp4")):
            folder = mp4.parent
            rel = f"{folder.parent.name}/{folder.name}"
            txt = folder / "publicar.txt"
            if not txt.exists():
                continue
            v = db.scalar(select(Video).where(Video.folder == rel))
            body = txt.read_text(encoding="utf-8")
            m = re.search(r"🎥 (.+?) — stream original: (\S+)", body)
            streamer, url = (m.group(1), m.group(2)) if m else ("", "")
            title = body.splitlines()[1].strip() if len(body.splitlines()) > 1 else folder.name
            tags = [t.lstrip("#") for t in re.findall(r"#\w+", body.split("=== HASHTAGS ===")[-1])][:12]
            lang = langs.get(streamer.lower(), "es")
            if v is None:
                dur = _duration(mp4)
                pri = 50.0 + (20.0 if _SONG.search(folder.name) else 0.0) + (5.0 if 25 <= dur <= 45 else 0.0)
                v = Video(folder=rel, streamer=streamer, stream=folder.parent.name, lang=lang,
                          audience=audience_for(streamer, lang), title=title, source_url=url,
                          duration_s=dur, priority=pri, tags=tags)
                db.add(v)
                db.flush()
                new += 1
            done = set()
            pj = folder / "publicado.json"
            if pj.exists():
                data = json.loads(pj.read_text(encoding="utf-8"))
                done = set(data.get("ok") or data.get("platforms") or [])
            have = {p.platform for p in v.posts}
            for plat in done - have:
                if plat in PLATFORMS:
                    db.add(Post(video_id=v.id, platform=plat, status="published",
                                method="imported", notes="imported from publicado.json (time unknown)"))
        total = db.scalar(select(func.count(Video.id)))
    console.print(f"[green]{new}[/green] clip(s) nuevos · {total} en total · BD: "
                  f"{database_url().split('@')[-1]}")


@ledger_app.command("status")
def status_cmd(pending: bool = typer.Option(False, "--pending", help="Solo clips con redes sin cubrir")) -> None:
    """Which clip is on which network."""
    t = Table(title="Clips × redes  (P=publicado  S=programado  ·=nada)")
    for c in ("Clip", "Streamer", "Prio", *(p[:2].upper() for p in PLATFORMS)):
        t.add_column(c, no_wrap=True)
    counts = {p: {s: 0 for s in STATUSES} for p in PLATFORMS}
    with session() as db:
        for v in db.scalars(select(Video).order_by(Video.priority.desc(), Video.id)):
            cells, missing = [], False
            by = {p.platform: p for p in v.posts}
            for plat in PLATFORMS:
                p = by.get(plat)
                if p is None:
                    cells.append("·")
                    missing = True
                else:
                    counts[plat][p.status] += 1
                    cells.append({"published": "P", "scheduled": "S", "planned": "p"}.get(p.status, "x"))
            if pending and not missing:
                continue
            t.add_row(v.folder.split("/")[-1][:34], v.streamer, f"{v.priority:.0f}", *cells)
    console.print(t)
    for plat in PLATFORMS:
        c = counts[plat]
        console.print(f"  {plat:10} publicados {c['published']:>2} · programados {c['scheduled']:>2} "
                      f"· planeados {c['planned']:>2}")


@ledger_app.command("plan")
def plan_cmd(
    days: int = typer.Option(7, "--days"),
    start: str = typer.Option("", "--from", help="'2026-11-22 00:00' hora Bogotá (default: ahora)"),
    platform: list[str] = typer.Option(None, "--platform", "-p"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    keep: bool = typer.Option(False, "--keep", help="No borrar los 'planned' anteriores"),
) -> None:
    """Fill the next days with the best clip for each slot, never the same clip on two networks at once."""
    when = _parse_when(start) if start else datetime.now(TZ) + timedelta(minutes=30)
    with session() as db:
        if not keep:  # re-plan from scratch: only untouched 'planned' rows are dropped
            for old in db.scalars(select(Post).where(Post.status == "planned")):
                db.delete(old)
            db.flush()
        rows = plan(db, when, days, platform or None)
        t = Table(title=f"Plan ({len(rows)} publicaciones)")
        for c in ("Cuándo (Bogotá)", "Red", "Streamer", "Clip"):
            t.add_column(c)
        for r in rows:
            t.add_row(_fmt(r.at), r.platform, r.video.streamer, r.video.folder.split("/")[-1][:40])
        console.print(t)
        if dry_run:
            db.rollback()
            console.print("[yellow]dry-run: no se guardó nada[/yellow]")


@ledger_app.command("queue")
def queue_cmd(
    hours: float = typer.Option(48, "--hours"),
    platform: str = typer.Option("", "--platform", "-p"),
    status: str = typer.Option("planned", "--status"),
) -> None:
    """What still has to be uploaded in the next hours."""
    limit = datetime.now(UTC) + timedelta(hours=hours)
    with session() as db:
        q = select(Post).where(Post.status == status, Post.scheduled_at <= limit).order_by(Post.scheduled_at)
        if platform:
            q = q.where(Post.platform == platform)
        t = Table()
        for c in ("Cuándo", "Red", "Streamer", "Carpeta"):
            t.add_column(c)
        for p in db.scalars(q):
            t.add_row(_fmt(p.scheduled_at), p.platform, p.video.streamer, p.video.folder)
        console.print(t)


@ledger_app.command("mark")
def mark_cmd(
    clip: str = typer.Argument(..., help="Texto de la carpeta (único)"),
    platform: str = typer.Argument(...),
    status: str = typer.Option("scheduled", "--status"),
    at: str = typer.Option("", "--at", help="'2026-11-22 13:00' hora Bogotá"),
    method: str = typer.Option("", "--method"),
    url: str = typer.Option("", "--url"),
    notes: str = typer.Option("", "--notes"),
    out: Path = typer.Option(Path("out"), "--out"),
) -> None:
    """Record that a clip was posted/scheduled on a network."""
    if platform not in PLATFORMS or status not in STATUSES:
        raise typer.BadParameter(f"platform ∈ {PLATFORMS}, status ∈ {STATUSES}")
    with session() as db:
        v = _find(db, clip)
        p = next((x for x in v.posts if x.platform == platform), None) or Post(video_id=v.id, platform=platform)
        p.status = status
        when = _parse_when(at) if at else None
        if status == "published":
            p.published_at = when or datetime.now(UTC)
            p.scheduled_at = p.published_at  # a planned time no longer applies
        elif when:
            p.scheduled_at = when
        if status in ("skipped", "failed"):
            p.scheduled_at = None
        p.method = method or p.method
        p.url = url or p.url
        p.notes = notes or p.notes
        db.add(p)
        db.flush()
        db.refresh(v)
        if status in ("scheduled", "published"):
            _sync_publicado(out, v)
    console.print(f"[green]✓[/green] {clip} → {platform}: {status} {at}")


@ledger_app.command("boost")
def boost_cmd(clip: str, points: float = typer.Argument(10.0)) -> None:
    """Raise/lower a clip's priority (it posts sooner/later)."""
    with session() as db:
        v = _find(db, clip)
        v.priority += points
    console.print(f"[green]✓[/green] {clip}: prioridad {v.priority:.0f}")
