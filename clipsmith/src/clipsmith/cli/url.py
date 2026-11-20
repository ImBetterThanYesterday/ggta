"""`clipsmith url <URL>` — run the pipeline on a Twitch, Kick or YouTube link."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from ..logging import configure_logging
from ..pipeline import process_vod
from ..settings import load_config, load_secrets
from ..sources import fetch_source
from .utils import _parse_start_at, _resolve_config

console = Console()


def url_cmd(
    url: str = typer.Argument(..., help="Twitch VOD, Kick VOD or YouTube video/stream URL"),
    start: str | None = typer.Option(
        None, "--start", help="Only use the stream from this time (H:MM:SS / MM:SS / seconds)"
    ),
    end: str | None = typer.Option(None, "--end", help="Only use the stream up to this time"),
    max_height: int = typer.Option(1080, "--max-height", help="Max video height to download"),
    config_path: Path = typer.Option(Path("config.yaml"), "--config", "-c"),
    provider: str | None = typer.Option(None, "--provider", help="anthropic | openai | ollama"),
    captions: bool | None = typer.Option(None, "--captions/--no-captions"),
    only_fetch: bool = typer.Option(False, "--only-fetch", help="Just download video + chat"),
    skip_select: bool = typer.Option(
        False, "--skip-select", help="Stop after scoring candidates (no LLM, no render)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Download video + chat from Twitch/Kick/YouTube and turn it into vertical clips."""
    configure_logging(verbose=verbose)
    cfg = load_config(_resolve_config(config_path))
    if captions is not None:
        cfg.caption.enabled = captions
    secrets = load_secrets()

    start_s = _parse_start_at(start)
    end_s = _parse_start_at(end)

    res = fetch_source(url, cfg.work_dir, start_s=start_s, end_s=end_s, max_height=max_height)
    console.print(
        f"[green]{res.platform}[/green] {res.video.user_login}: {res.video.title[:80]!r}\n"
        f"  video → {res.mp4_path}\n"
        f"  chat  → {len(res.chat.messages)} mensajes"
        + (f"\n  'most replayed' peaks → {len(res.signal_clips)}" if res.signal_clips else "")
    )
    if only_fetch:
        return

    process_vod(
        res.video,
        cfg,
        secrets,
        skip_download=True,
        skip_chat=True,  # chat.json already written by fetch_source
        skip_select=skip_select,
        provider=provider,
        extra_clips=res.signal_clips,
    )
