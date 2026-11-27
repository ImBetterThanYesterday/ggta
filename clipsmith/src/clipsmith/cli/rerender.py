"""`clip rerender`: re-render finished clips with the current layout/encoder.

Uses what's cached in work/ (video piece, transcript, picks, hook), so there's no
download and no AI call. Handy after improving facecam detection or quality.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import typer
from rich.console import Console

from ..llm.base import ClipPick
from ..models.candidates import CandidateMoment
from ..models.transcript import Transcript
from ..rendering.clipper import _cut_smart
from ..selection.selector import PickResult
from ..settings import load_config
from .utils import _resolve_config

console = Console()


def _work_dir_for(folder: Path, work: Path) -> Path | None:
    """out/<stream>/<h-mm-ss>_<title>/  ->  work/<source-id>_p<seconds>/"""
    m = re.match(r"(\d+)-(\d+)-(\d+)_", folder.name)
    pub = folder / "publicar.txt"
    if not m or not pub.exists():
        return None
    sec = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
    text = pub.read_text(encoding="utf-8")
    prefixes = []
    if k := re.search(r"kick\.com/[^/]+/videos/([0-9a-f]{8})", text):
        prefixes.append(f"kick_{k[1]}")
    if t := re.search(r"twitch\.tv/videos/(\d+)", text):
        prefixes.append(t[1])
    if y := re.search(r"(?:v=|youtu\.be/)([\w-]{11})", text):
        prefixes.append(f"yt_{y[1]}")
    for pre in prefixes:
        for cand in (work / f"{pre}_p{sec}", *work.glob(f"{pre}_p{sec}*")):
            if (cand / "picks.json").exists():
                return cand
    return None


def make_web_copy(src: Path, dst: Path, max_mb: float = 9.4) -> Path:
    """Best quality that fits in `max_mb`: 2-pass x264 'slow' at 1080x1920."""
    import subprocess  # nosec B404
    import tempfile

    from ..io.media import video_duration

    dur = max(1.0, video_duration(src))
    audio_k = 128
    video_k = int(max_mb * 8 * 1024 / dur - audio_k - 40)
    with tempfile.TemporaryDirectory() as tmp:
        log = str(Path(tmp) / "x264")
        base = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-c:v", "libx264",
                "-preset", "slow", "-b:v", f"{video_k}k", "-maxrate", f"{int(video_k * 1.5)}k",
                "-bufsize", f"{video_k * 2}k", "-pix_fmt", "yuv420p", "-passlogfile", log]
        subprocess.run([*base, "-pass", "1", "-an", "-f", "mp4", "/dev/null"], check=True)  # nosec B603
        subprocess.run([*base, "-pass", "2", "-c:a", "aac", "-b:a", f"{audio_k}k",  # nosec B603
                        "-movflags", "+faststart", str(dst)], check=True)
    return dst


def rerender_cmd(
    match: list[str] = typer.Argument(None, help="Only clip folders whose path contains this"),
    config_path: Path = typer.Option(Path("config.yaml"), "--config", "-c"),
    out: Path = typer.Option(None, "--out", help="Folder with the clip folders (default: out/)"),
    redetect: bool = typer.Option(True, "--redetect/--keep-cam", help="Detect the facecam again"),
    replan: bool = typer.Option(False, "--replan", help="Ask for a new edit plan (layouts, hook...)"),
    web: bool = typer.Option(True, "--web/--no-web",
                             help="Also write video_web.mp4 (<10 MB, for browser uploads)"),
) -> None:
    """Re-render finished clips (better facecam crop / quality) from the cache in work/."""
    cfg = load_config(_resolve_config(config_path))
    work = cfg.work_dir.expanduser()
    out = (out or cfg.out_dir).expanduser()
    folders = sorted(p.parent for p in out.glob("*/*/video.mp4"))
    if match:
        folders = [f for f in folders if any(s.lower() in str(f).lower() for s in match)]
    if not folders:
        console.print("[yellow]No clip folders matched.[/yellow]")
        raise typer.Exit(1)
    done = 0
    for folder in folders:
        wdir = _work_dir_for(folder, work)
        mp4 = (wdir / f"{wdir.name}.mp4") if wdir else None
        if not wdir or not mp4 or not mp4.exists() or not (wdir / "transcript.json").exists():
            console.print(f"[yellow]skip[/yellow] {folder.parent.name}/{folder.name}: cache not found")
            continue
        raw = json.loads((wdir / "picks.json").read_text(encoding="utf-8"))
        picks = [PickResult(candidate=CandidateMoment(**p["candidate"]),
                            pick=ClipPick.from_dict(p["pick"])) for p in raw]
        if not picks:
            continue
        transcript = Transcript.from_json((wdir / "transcript.json").read_text(encoding="utf-8"))
        if redetect:
            (wdir / "facecam.json").unlink(missing_ok=True)
        if replan:
            (wdir / "edit_01.json").unlink(missing_ok=True)
        tmp = folder / "video.new.mp4"
        pr = picks[0]
        try:
            _cut_smart(mp4, transcript, pr.pick.start_offset_s, pr.pick.end_offset_s, 1, tmp, cfg, pr)
        except Exception as exc:
            console.print(f"[red]failed[/red] {folder.name}: {exc}")
            tmp.unlink(missing_ok=True)
            continue
        shutil.move(str(tmp), folder / "video.mp4")
        plan = tmp.with_suffix(".edit.json")
        if plan.exists():
            shutil.move(str(plan), folder / "edit.json")
        tmp.with_suffix(".txt").unlink(missing_ok=True)
        if web:
            make_web_copy(folder / "video.mp4", folder / "video_web.mp4")
        else:
            (folder / "video_web.mp4").unlink(missing_ok=True)  # stale copy
        cam = json.loads((wdir / "facecam.json").read_text()) if (wdir / "facecam.json").exists() else []
        console.print(f"[green]✓[/green] {folder.parent.name}/{folder.name}  cam={cam or 'none'}")
        done += 1
    console.print(f"\n[green]{done}[/green] clip(s) re-rendered.")
