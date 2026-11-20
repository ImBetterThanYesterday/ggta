"""`clipsmith radar` (who is live) and `clipsmith live` (clip live streams as they happen)."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ..logging import configure_logging
from ..pipeline import process_vod
from ..settings import load_config, load_secrets
from ..sources import fetch_source
from ..sources.live import ChannelStatus, check_roster, load_roster
from .utils import _resolve_config

console = Console()
_DEFAULT_ROSTER = Path("../streamers.yaml")


def _matches(st: ChannelStatus, game: str) -> bool:
    if not game:
        return True
    g = game.lower()
    hay = f"{st.game} {st.title}".lower()
    # "gta" also matches "Grand Theft Auto" and "GTA VI" titles
    return g in hay or (g == "gta" and "grand theft auto" in hay)


def _live_now(roster_path: Path, game: str, tier: int, lang: str) -> list[ChannelStatus]:
    roster = [
        s for s in load_roster(roster_path)
        if s.get("tier", 1) <= tier and (not lang or s.get("lang") == lang)
    ]
    res = [r for r in check_roster(roster) if r.live and _matches(r, game)]
    # One row per streamer: keep the platform with most viewers (simulcasts).
    best: dict[str, ChannelStatus] = {}
    for r in res:
        if r.streamer not in best or r.viewers > best[r.streamer].viewers:
            best[r.streamer] = r
    return sorted(best.values(), key=lambda r: r.viewers, reverse=True)


def radar_cmd(
    roster: Path = typer.Option(_DEFAULT_ROSTER, "--roster", help="streamers.yaml"),
    game: str = typer.Option("gta", "--game", help="Filtra por juego/título ('' = todos)"),
    tier: int = typer.Option(2, "--tier", help="1 = solo los top, 2 = todos"),
    lang: str = typer.Option("", "--lang", help="en | es | pt ... ('' = todos)"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show which streamers from the roster are live right now."""
    configure_logging(verbose=False)
    live = _live_now(roster, game, tier, lang)
    if as_json:
        print(json.dumps([r.to_dict() for r in live], ensure_ascii=False, indent=2))
        return
    t = Table(title=f"En vivo ahora ({datetime.now():%H:%M}) — filtro: {game or 'todo'}")
    for col in ("Streamer", "Plataforma", "Viewers", "Lleva", "Juego", "Título", "URL"):
        t.add_column(col, overflow="fold")
    for r in live:
        h, m = divmod(int(r.elapsed_s) // 60, 60)
        t.add_row(r.streamer, r.platform, f"{r.viewers:,}", f"{h}h{m:02d}", r.game[:20],
                  r.title[:40], r.url)
    console.print(t)
    if not live:
        console.print("[dim]Nadie del roster está en vivo con ese filtro.[/dim]")


def live_cmd(
    roster: Path = typer.Option(_DEFAULT_ROSTER, "--roster"),
    game: str = typer.Option("gta", "--game"),
    tier: int = typer.Option(1, "--tier"),
    lang: str = typer.Option("", "--lang"),
    only: list[str] = typer.Option([], "--only", help="Solo estos streamers (repetible)"),
    window_min: int = typer.Option(20, "--window", help="Minutos a procesar por pasada"),
    every_min: int = typer.Option(20, "--every", help="Cada cuántos minutos revisar"),
    top: int = typer.Option(3, "--top", help="Máx. streams por pasada (los de más viewers)"),
    once: bool = typer.Option(False, "--once", help="Una sola pasada y salir"),
    max_height: int = typer.Option(720, "--max-height"),
    config_path: Path = typer.Option(Path("config.yaml"), "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Clip LIVE streams: every N minutes, process the last N minutes of each live stream."""
    configure_logging(verbose=verbose)
    cfg = load_config(_resolve_config(config_path))
    secrets = load_secrets()
    state_path = cfg.work_dir.expanduser() / "live_state.json"
    state: dict = json.loads(state_path.read_text()) if state_path.exists() else {}
    lag_s = 90  # live VODs/DVR lag behind real time a bit

    while True:
        live = _live_now(roster, game, tier, lang)
        if only:
            wanted = {o.lower() for o in only}
            live = [r for r in live if r.streamer.lower() in wanted]
        console.print(f"[cyan]{datetime.now():%H:%M}[/cyan] en vivo: "
                      + ", ".join(f"{r.streamer} ({r.viewers:,})" for r in live) or "nadie")
        for r in live[:top]:
            if not r.url:
                console.print(f"[yellow]{r.streamer}: no encontré URL del VOD en curso[/yellow]")
                continue
            key = f"{r.platform}:{r.url}"
            done_until = float(state.get(key, 0))
            end_s = max(0.0, r.elapsed_s - lag_s)
            start_s = max(done_until, end_s - window_min * 60)
            if end_s - start_s < 120:
                continue  # not enough new footage yet
            suffix = f"_w{int(start_s)}"
            console.print(f"[green]▶ {r.streamer}[/green] {r.platform} "
                          f"{int(start_s)//60}–{int(end_s)//60} min  {r.url}")
            try:
                res = fetch_source(r.url, cfg.work_dir, start_s=start_s, end_s=end_s,
                                   max_height=max_height, id_suffix=suffix)
                process_vod(res.video, cfg, secrets, skip_download=True, skip_chat=True,
                            extra_clips=res.signal_clips)
                state[key] = end_s
                state_path.write_text(json.dumps(state, indent=2))
            except Exception as exc:
                console.print(f"[red]{r.streamer}: {exc}[/red]")
        if once:
            break
        time.sleep(every_min * 60)
