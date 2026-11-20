"""`clipsmith batch` — latest finished stream of each streamer in the roster -> clips."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from ..logging import configure_logging
from ..sources.latest import latest_finished
from ..sources.live import load_roster
from .vod import run_vod

console = Console()


def batch_cmd(
    only: list[str] = typer.Option([], "--only", help="Streamers a procesar (repetible). Vacío = todos"),
    roster: Path = typer.Option(Path("../streamers.yaml"), "--roster"),
    game: str = typer.Option("gta", "--game", help="Solo directos de este juego ('' = cualquiera)"),
    top: int = typer.Option(12, "--top", help="Momentos a revisar por directo"),
    tier: int = typer.Option(2, "--tier"),
    config_path: Path = typer.Option(Path("config.yaml"), "--config", "-c"),
) -> None:
    """For each streamer: find the latest FINISHED stream and run `vod` on it."""
    configure_logging(verbose=False)
    entries = [s for s in load_roster(roster) if s.get("tier", 1) <= tier]
    if only:
        wanted = [o.lower() for o in only]
        entries = sorted((s for s in entries if s["name"].lower() in wanted),
                         key=lambda s: wanted.index(s["name"].lower()))
    summary = []
    for s in entries:
        console.rule(f"[bold magenta]{s['name']}[/bold magenta]")
        ps = latest_finished(s, game=game)
        if not ps:
            console.print(f"[yellow]{s['name']}: sin directo terminado de '{game}'[/yellow]")
            summary.append((s["name"], "sin directo", 0))
            continue
        console.print(f"{ps.platform} · {ps.duration_s / 3600:.1f} h · {ps.title[:70]!r}\n{ps.url}")
        try:
            made = run_vod(ps.url, top=top, streamer=s["name"], lang=s.get("lang", "es"),
                           config_path=config_path)
            summary.append((s["name"], ps.url, len(made)))
        except Exception as exc:
            console.print(f"[red]{s['name']} falló: {exc}[/red]")
            summary.append((s["name"], f"error: {exc}"[:80], 0))
    console.rule("Resumen")
    for name, src, n in summary:
        console.print(f"  {name:16} {n:2d} clip(s)  {src}")
