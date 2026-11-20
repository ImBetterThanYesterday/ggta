"""clipsmith CLI entry point."""

from __future__ import annotations

import typer

from .batch import batch_cmd
from .clip import clip_cmd, reframe_cmd
from .clip import publish_cmd as youtube_publish_cmd
from .cloud import cloud_app
from .live import live_cmd, radar_cmd
from .publish import publish_cmd, status_cmd
from .run import process, run_vod, serve, watch, whoami
from .setup import check_ollama, detect_webcam_cmd, setup
from .url import url_cmd
from .vod import vod_cmd

app = typer.Typer(add_completion=False, help="Twitch / Kick / YouTube -> AI clip pipeline")

app.add_typer(cloud_app, name="cloud")

app.command()(process)
app.command()(watch)
app.command("run-vod")(run_vod)
app.command()(whoami)
app.command()(serve)
app.command("url")(url_cmd)
app.command("vod")(vod_cmd)
app.command("batch")(batch_cmd)
app.command("publish")(publish_cmd)
app.command("publish-status")(status_cmd)
app.command("radar")(radar_cmd)
app.command("live")(live_cmd)
app.command("clip")(clip_cmd)
app.command("reframe")(reframe_cmd)
app.command("publish-youtube")(youtube_publish_cmd)  # upstream: direct YouTube OAuth
app.command()(setup)
app.command("check-ollama")(check_ollama)
app.command("detect-webcam")(detect_webcam_cmd)

if __name__ == "__main__":
    app()
