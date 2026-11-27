"""`clipsmith batch` — latest finished stream of each streamer in the roster -> clips."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from ..logging import configure_logging
from ..settings import load_config
from ..sources.latest import recent_finished
from ..sources.live import load_roster
from .vod import run_vod

console = Console()


def batch_cmd(
    only: list[str] = typer.Option([], "--only", help="Streamers a procesar (repetible). Vacío = todos"),
    roster: Path = typer.Option(Path("../streamers.yaml"), "--roster"),
    game: str = typer.Option("gta", "--game", help="Solo este juego; '!gta' = todo menos GTA; '' = cualquiera"),
    top: int = typer.Option(12, "--top", help="Momentos a revisar por directo"),
    tier: int = typer.Option(2, "--tier"),
    days: float = typer.Option(7, "--days", help="Directos de los últimos N días aún sin cortar (0 = solo el último)"),
    max_streams: int = typer.Option(3, "--max-streams", help="Máximo de directos por streamer"),
    config_path: Path = typer.Option(Path("config.yaml"), "--config", "-c"),
    out: Path = typer.Option(None, "--out", help="Carpeta de salida (default: out_dir del config)"),
    keep_source: bool = typer.Option(True, "--keep-source/--drop-source",
                                     help="Borrar el video fuente de work/ tras cortar el clip"),
) -> None:
    """For each streamer: every finished stream of the last `--days` days not cut yet -> `vod`."""
    configure_logging(verbose=False)
    entries = [s for s in load_roster(roster) if s.get("tier", 1) <= tier]
    if only:
        wanted = [o.lower() for o in only]
        entries = sorted((s for s in entries if s["name"].lower() in wanted),
                         key=lambda s: wanted.index(s["name"].lower()))
    done = _processed_urls(config_path)
    summary = []
    for s in entries:
        console.rule(f"[bold magenta]{s['name']}[/bold magenta]")
        streams = recent_finished(s, game=game, days=days)
        if not streams:
            console.print(f"[yellow]{s['name']}: sin directo terminado de '{game}'[/yellow]")
            summary.append((s["name"], "sin directo", 0))
            continue
        todo = [ps for ps in streams if ps.url not in done][:max_streams]
        if not todo:
            console.print(f"[dim]{s['name']}: sus directos recientes ya están cortados[/dim]")
            summary.append((s["name"], "nada nuevo", 0))
            continue
        for ps in todo:
            console.print(f"{ps.platform} · {ps.started_at:%Y-%m-%d} · {ps.duration_s / 3600:.1f} h · "
                          f"{ps.title[:70]!r}\n{ps.url}")
            try:
                made = run_vod(ps.url, top=top, streamer=s["name"], lang=s.get("lang", "es"),
                               config_path=config_path, out_dir=out, keep_source=keep_source)
                summary.append((s["name"], ps.url, len(made)))
            except Exception as exc:
                console.print(f"[red]{s['name']} falló: {exc}[/red]")
                summary.append((s["name"], f"error: {exc}"[:80], 0))
    console.rule("Resumen")
    for name, src, n in summary:
        console.print(f"  {name:16} {n:2d} clip(s)  {src}")


def _processed_urls(config_path: Path) -> set[str]:
    """Stream URLs that already have cut moments (each work/<clip>/context.json names its source)."""
    try:
        work = load_config(config_path).work_dir.expanduser()
    except Exception:
        work = Path("work")
    urls = set()
    for f in work.glob("*/context.json"):
        try:
            urls.add(json.loads(f.read_text(encoding="utf-8")).get("url", ""))
        except ValueError:
            continue
    return urls
