"""`clipsmith vod <URL>` — best moments of a FINISHED stream, chat-first."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ..logging import configure_logging
from ..models.twitch import Clip
from ..pipeline import process_vod
from ..settings import load_config, load_secrets
from ..sources import fetch_source
from ..sources.vod import (
    Peak,
    fetch_audio,
    fetch_full_chat,
    find_loud_peaks,
    find_peaks,
    find_song_peaks,
    loudness_series,
    merge_heatmap,
    merge_peaks,
)
from .utils import _resolve_config

console = Console()


def _hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def vod_cmd(
    url: str = typer.Argument(..., help="Link de un directo TERMINADO (YouTube, Kick o Twitch)"),
    top: int = typer.Option(8, "--top", help="Cuántos momentos procesar"),
    pre: int = typer.Option(75, "--pre", help="Segundos antes del pico a bajar"),
    post: int = typer.Option(45, "--post", help="Segundos después del pico a bajar"),
    min_score: float = typer.Option(3.0, "--min-score", help="Umbral del pico de chat"),
    only_peaks: bool = typer.Option(False, "--only-peaks", help="Solo listar picos, no cortar"),
    bulla: bool = typer.Option(True, "--bulla/--no-bulla", help="Detectar gritos/bulla"),
    canto: bool = typer.Option(True, "--canto/--no-canto", help="Detectar cuando canta"),
    exclude: list[str] = typer.Option([], "--exclude", help="Saltar picos cerca de H:MM:SS"),
    streamer: str = typer.Option("", "--streamer", help="Nombre a usar en ganchos/créditos"),
    lang: str = typer.Option("", "--lang", help="es | en (idioma del gancho y la voz)"),
    max_height: int = typer.Option(1080, "--max-height"),
    config_path: Path = typer.Option(Path("config.yaml"), "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Find where chat exploded / people screamed / sang, then clip only those moments."""
    configure_logging(verbose=verbose)
    run_vod(url, top=top, pre=pre, post=post, min_score=min_score, only_peaks=only_peaks,
            bulla=bulla, canto=canto, exclude=exclude, streamer=streamer, lang=lang,
            max_height=max_height, config_path=config_path)


def run_vod(
    url: str, *, top: int = 8, pre: int = 75, post: int = 45, min_score: float = 3.0,
    only_peaks: bool = False, bulla: bool = True, canto: bool = True,
    exclude: list[str] | None = None, streamer: str = "", lang: str = "",
    max_height: int = 1080, config_path: Path = Path("config.yaml"),
) -> list[Path]:
    cfg = load_config(_resolve_config(config_path))
    secrets = load_secrets()
    work = cfg.work_dir.expanduser()

    vc = fetch_full_chat(url, work)
    name = streamer or vc.channel
    console.print(f"[green]{vc.platform}[/green] {name}: {vc.title[:80]!r} — "
                  f"{vc.duration_s / 3600:.1f} h, {len(vc.messages):,} mensajes de chat")

    if vc.duration_s:
        # YouTube only archives the first ~12 h of longer streams, but the chat replay
        # covers everything: drop chat after the end of the saved video.
        vc.messages = [m for m in vc.messages if m.time_in_seconds < vc.duration_s - 30]
    peaks = find_peaks(vc.messages, top=top, min_score=min_score)
    if bulla:
        try:
            db = loudness_series(fetch_audio(url, vc.platform, work))
            loud = find_loud_peaks(db, top=top)
            console.print(f"[cyan]bulla:[/cyan] {len(loud)} picos de volumen")
            peaks = merge_peaks(peaks, loud, top=top)
        except Exception as exc:
            console.print(f"[yellow]bulla: no se pudo analizar el audio ({exc})[/yellow]")
    if canto:
        songs = find_song_peaks(vc.messages)
        console.print(f"[magenta]canto:[/magenta] {len(songs)} momentos con canción")
        peaks = songs + [p for p in peaks if all(abs(p.t - q.t) > 120 for q in songs)]
    peaks = merge_heatmap(peaks, vc.signal_clips, top=top + 6)
    peaks = peaks[:top]  # --top is a hard cap (songs first, then chat/loudness/heatmap)
    if exclude:
        from .utils import _parse_start_at

        skip = [_parse_start_at(e) for e in exclude]
        peaks = [p for p in peaks if all(abs(p.t - x) > 120 for x in skip)]
    if not peaks:
        console.print("[yellow]No encontré momentos claros.[/yellow]")
        return []

    t = Table(title=f"Momentos top — {name}")
    for c in ("#", "Hora", "Score", "Msgs", "Reacc.", "Señal / lo que decía el chat"):
        t.add_column(c, overflow="fold")
    for i, p in enumerate(peaks, 1):
        t.add_row(str(i), _hms(p.t), str(p.score), str(p.msgs), str(p.reactions),
                  " | ".join(p.samples[:5])[:120])
    console.print(t)
    if only_peaks:
        return []

    slug = re.sub(r"[^\w]+", "_", f"{name}_{vc.title}".lower())[:50].strip("_")
    final_dir = cfg.out_dir.expanduser() / slug
    final_dir.mkdir(parents=True, exist_ok=True)
    (final_dir / "peaks.json").write_text(
        json.dumps([vars(p) for p in peaks], ensure_ascii=False, indent=2), encoding="utf-8"
    )

    made: list[Path] = []
    for i, p in enumerate(peaks, 1):
        if list(work.glob(f"*_p{int(p.t)}/picks.json")):
            console.print(f"[dim]{_hms(p.t)} ya procesado antes — lo salto[/dim]")
            continue
        is_song = bool(p.samples) and p.samples[0].startswith("(CANTANDO")
        p_pre, p_post = (40, 80) if is_song else (pre, post)  # songs: chat reacts early
        start = max(0.0, p.t - p_pre)
        end = min(vc.duration_s or p.t + p_post, p.t + p_post)
        console.rule(f"[bold]{name} {i}/{len(peaks)}  {_hms(p.t)}[/bold]")
        try:
            res = fetch_source(url, work, start_s=start, end_s=end, max_height=max_height,
                               id_suffix=f"_p{int(p.t)}", chat_messages=vc.messages)
            wdir = work / res.video.id
            (wdir / "context.json").write_text(json.dumps(
                {"streamer": name, "title": vc.title, "lang": lang, "url": url,
                 "platform": vc.platform, "window_start": start}, ensure_ascii=False),
                encoding="utf-8")
            chat_txt = " | ".join(p.samples[:8])
            kind = "STREAMER SINGING ALONG? chat talks about the song" if is_song else "CHAT/AUDIO PEAK"
            hint = Clip(
                id=f"peak_{int(p.t)}", url="",
                title=(f"{kind} (score {p.score}, {p.msgs} msgs, {p.reactions} reactions). "
                       f"Chat said: {chat_txt}"),
                creator_name="chat", video_id=res.video.id,
                vod_offset=int(p.t - start), duration=10, view_count=0, created_at="",
            )
            res.video.user_login = name
            process_vod(res.video, cfg, secrets, skip_download=True, skip_chat=True,
                        skip_transcribe=True, extra_clips=[hint, *res.signal_clips],
                        max_candidates=1)
            made += _package(cfg.out_dir.expanduser() / res.video.id, wdir, final_dir, p,
                             start, name, vc.platform, url, vc.title, vc.started_at)
        except Exception as exc:
            console.print(f"[red]momento {i} falló: {exc}[/red]")

    console.print(f"\n[green]Listo:[/green] {len(made)} clip(s) nuevos en {final_dir}")
    for c in made:
        console.print(f"  • {c.parent.name}/")
    return made


def _source_link(platform: str, url: str, t: float) -> str:
    s = int(t)
    if platform == "youtube":
        return f"{url.split('&')[0]}&t={s}s"
    if platform == "twitch":
        return f"{url}?t={s // 3600}h{s % 3600 // 60}m{s % 60}s"
    return url


def _package(out_dir: Path, wdir: Path, final_dir: Path, p: Peak, window_start: float,
             streamer: str, platform: str, url: str, stream_title: str,
             started_at: datetime | None = None) -> list[Path]:
    """One folder per clip: video.mp4 + publicar.txt with everything needed to post."""
    made = []
    picks = json.loads((wdir / "picks.json").read_text()) if (wdir / "picks.json").exists() else []
    for n, mp4 in enumerate(sorted(out_dir.glob("*.mp4")), 1):
        hook = {}
        for hp in (wdir / f"hook_{n:02d}.json", wdir / "hook.json"):
            if hp.exists():
                hook = json.loads(hp.read_text(encoding="utf-8"))
                break
        pick = picks[n - 1]["pick"] if len(picks) >= n else {}
        abs_t = window_start + float(pick.get("start_offset_s", p.t - window_start))
        dur = float(pick.get("end_offset_s", 0)) - float(pick.get("start_offset_s", 0))
        folder = final_dir / f"{_hms(p.t).replace(':', '-')}_{mp4.stem.replace('clip_01_', '')}"
        folder.mkdir(parents=True, exist_ok=True)
        shutil.move(str(mp4), folder / "video.mp4")
        for extra in out_dir.glob(f"{mp4.stem}.*"):
            extra.unlink(missing_ok=True)
        tags = " ".join(hook.get("hashtags", []))
        title = hook.get("post_title") or pick.get("title_es", "")
        caption = hook.get("post_caption", "")
        credit = f"🎥 {streamer}"
        txt = f"""=== TÍTULO (YouTube Shorts, ≤100 caracteres) ===
{title}

=== DESCRIPCIÓN YouTube ===
{caption}

{credit} — stream original: {_source_link(platform, url, abs_t)}
{tags}

=== TikTok / Instagram Reels / Facebook Reels (texto del post) ===
{caption}

{credit}
{tags}

=== HASHTAGS ===
{tags}

=== GANCHO USADO ===
Estilo: {hook.get('hook_style', '-')}
Texto en pantalla: {hook.get('hook_text', '-')}
Voz en off: {hook.get('voiceover', '-') if hook.get('hook_style') != 'text_only' else '(sin voz)'}

=== DATOS DEL CLIP ===
Streamer: {streamer}
Plataforma: {platform}
Directo: {stream_title}
Fecha del directo: {started_at.strftime('%Y-%m-%d %H:%M UTC') if started_at else '-'}
Minuto en el directo: {_hms(abs_t)}  (duración del clip: {dur:.0f} s)
Link al momento exacto: {_source_link(platform, url, abs_t)}
Por qué se eligió: {pick.get('reason', '')}

=== CHECKLIST ANTES DE PUBLICAR ===
[ ] Revisé el video completo con sonido
[ ] El streamer da permiso / tiene campaña de clipping
[ ] Sin spoilers fuertes de la historia
[ ] Título y hashtags revisados (nombre bien escrito)
"""
        (folder / "publicar.txt").write_text(txt, encoding="utf-8")
        made.append(folder / "video.mp4")
    return made
