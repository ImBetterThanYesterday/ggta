"""Clip cutter: ffmpeg trim → 9:16 reframe → burned ASS captions."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess  # nosec B403
import sys
import unicodedata
from pathlib import Path

from ..models.transcript import Transcript
from ..selection.selector import PickResult
from ..settings import AppConfig, ReframeConfig
from .captions import _write_ass

log = logging.getLogger(__name__)


def _find_ffmpeg() -> str:
    """Return path to ffmpeg: bundled copy next to exe, or fall back to PATH."""
    bundled = Path(sys.executable).parent / "ffmpeg.exe"
    if bundled.exists():
        return str(bundled)
    path = shutil.which("ffmpeg")
    if path is None:
        raise RuntimeError("ffmpeg not found on PATH")
    return path


def cut_all_clips(
    mp4_path: Path,
    transcript: Transcript,
    picks: list[PickResult],
    out_dir: Path,
    config: AppConfig,
) -> list[Path]:
    """Cut, reframe, and caption every accepted pick. Returns paths of created files."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []
    for i, pr in enumerate(picks, 1):
        slug = _title_slug(pr.pick.title_es)
        out_path = out_dir / f"clip_{i:02d}_{slug}.mp4"
        results.append(_cut_one(mp4_path, transcript, pr, i, out_path, config))
    return results


def _cut_one(
    mp4_path: Path,
    transcript: Transcript,
    pr: PickResult,
    index: int,
    out_path: Path,
    config: AppConfig,
) -> Path:
    start = pr.pick.start_offset_s
    end = pr.pick.end_offset_s

    if config.reframe.mode == "smart":
        return _cut_smart(mp4_path, transcript, start, end, index, out_path, config, pr)

    ass_path: Path | None = None
    if config.caption.enabled:
        ass_path = out_path.with_suffix(".ass")
        _write_ass(transcript, start, end, config.caption, ass_path)

    cmd = _build_ffmpeg_cmd(mp4_path, start, end, ass_path, config.reframe, out_path)
    log.info("clip %d  [%.1f-%.1fs]  ->  %s", index, start, end, out_path.name)
    log.debug("ffmpeg: %s", " ".join(cmd))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)  # nosec B603 — cmd built internally by _build_ffmpeg_cmd
    except FileNotFoundError:
        raise RuntimeError(
            "ffmpeg not found. Place ffmpeg.exe next to clipsmith.exe or add it to PATH."
        )
    if result.returncode != 0:
        log.error("ffmpeg stderr:\n%s", result.stderr[-2000:])
        raise RuntimeError(f"ffmpeg failed for clip {index} ({out_path.name})")
    return out_path


def _load_or_make_hook(mp4_path: Path, transcript: Transcript, start: float, end: float,
                       index: int, config: AppConfig, pr: PickResult | None) -> dict | None:
    """hook_<index>.json (or hook.json) next to the source; generated with the LLM if missing."""
    import json

    from .hook import generate_hook, save_hook

    d = mp4_path.parent
    for name in (f"hook_{index:02d}.json", "hook.json"):
        if (d / name).exists():
            return json.loads((d / name).read_text(encoding="utf-8"))
    if not config.hook.enabled or pr is None:
        return None
    ctx = {}
    if (d / "context.json").exists():
        ctx = json.loads((d / "context.json").read_text(encoding="utf-8"))
    text = "\n".join(
        f"[{s.start - start:.0f}s] {s.text.strip()}"
        for s in transcript.segments if s.end >= start and s.start <= end
    )
    chat = ""
    if (d / "chat.json").exists():
        msgs = json.loads((d / "chat.json").read_text(encoding="utf-8")).get("messages", [])
        chat = " | ".join(m["message"] for m in msgs if start <= m["time_in_seconds"] <= end + 20)
    try:
        hook = generate_hook(
            streamer=ctx.get("streamer", ""), stream_title=ctx.get("title", ""),
            reason=f"{pr.pick.title_es}. {pr.pick.reason}", transcript=text, chat=chat,
            lang={"en": "English", "es": "Spanish", "pt": "Portuguese"}.get(
                ctx.get("lang", ""), config.hook.language),
            provider=config.hook.provider,
        )
    except Exception as exc:
        log.warning("hook generation failed: %s", exc)
        return None
    save_hook(hook, d / f"hook_{index:02d}.json")
    return hook


def _cut_smart(
    mp4_path: Path,
    transcript: Transcript,
    start: float,
    end: float,
    index: int,
    out_path: Path,
    config: AppConfig,
    pr: PickResult | None = None,
) -> Path:
    """Facecam-aware vertical layout + AI hook (text + voice) + PNG captions."""
    import json
    import re

    from .captions_png import write_caption_track
    from .hook import render_hook_png, synth_voice
    from .smart import smart_filter

    tmp = (out_path.parent / f".work_{out_path.stem}").resolve()
    tmp.mkdir(parents=True, exist_ok=True)

    hook = _load_or_make_hook(mp4_path, transcript, start, end, index, config, pr)
    hook_dur, vo_path, hook_png = 0.0, None, None
    if hook:
        ctx_path = mp4_path.parent / "context.json"
        streamer = json.loads(ctx_path.read_text())["streamer"] if ctx_path.exists() else ""
        text = hook["hook_text"]
        if streamer:  # LLMs misspell names ("DJMARIIIO"): force the exact spelling
            pat = re.sub(r"(.)", lambda m: re.escape(m.group(1)) + "+", streamer.lower())
            text = re.sub(pat, streamer.upper(), text, flags=re.IGNORECASE)
        hook_png = tmp / "hook.png"
        render_hook_png(text, hook_png)
        if hook.get("hook_style", "voice_text") == "text_only":
            # Text only: long enough to read (~4 words/s), streamer's audio untouched.
            hook_dur = min(3.5, max(2.2, len(text.split()) / 4 + 1.0))
        else:
            vo = hook["voiceover"]
            for name, spoken in config.hook.pronounce.items():
                vo = re.sub(re.escape(name), spoken, vo, flags=re.IGNORECASE)
            vo_path = tmp / "vo.mp3"
            lang = json.loads(ctx_path.read_text()).get("lang", "") if ctx_path.exists() else ""
            voice = config.hook.voices.get(lang, config.hook.voice)
            hook_dur = synth_voice(vo, vo_path, voice, config.hook.rate) + 0.35

    track = None
    if config.caption.enabled:
        track = write_caption_track(transcript, start, end, tmp / "caps", skip_until=hook_dur)

    cmd = [_find_ffmpeg(), "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
           "-i", str(mp4_path)]
    idx = 1
    cap_i = hook_i = vo_i = None
    if track:
        cmd += ["-f", "concat", "-safe", "0", "-i", str(track)]
        cap_i, idx = idx, idx + 1
    if hook_png:
        cmd += ["-loop", "1", "-t", f"{hook_dur:.2f}", "-i", str(hook_png)]
        hook_i, idx = idx, idx + 1
        if vo_path:
            cmd += ["-i", str(vo_path)]
            vo_i, idx = idx, idx + 1
    fc = smart_filter(mp4_path, mp4_path.parent, captions_input=cap_i,
                      hook_input=hook_i, hook_dur=hook_dur)
    if vo_i is not None:
        # Duck the game under the voice-over, then mix.
        fc += (f";[0:a]volume='if(lt(t,{hook_dur:.2f}),0.18,1)':eval=frame[ga]"
               f";[{vo_i}:a]adelay=120|120,volume=1.8[vo]"
               f";[ga][vo]amix=inputs=2:duration=first:normalize=0[aout]")
        amap = "[aout]"
    else:
        amap = "0:a?"
    cmd += [
        "-filter_complex", fc, "-map", "[out]", "-map", amap,
        "-c:v", "libx264", "-preset", "faster", "-crf", "19", "-profile:v", "high",
        "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "160k",
        "-t", f"{end - start:.3f}", "-movflags", "+faststart", str(out_path),
    ]
    log.info("clip %d  [%.1f-%.1fs]  ->  %s (smart%s)", index, start, end, out_path.name,
             f", hook {hook.get('hook_style', 'voice_text')} {hook_dur:.1f}s" if hook else "")
    result = subprocess.run(cmd, capture_output=True, text=True)  # nosec B603
    if result.returncode != 0:
        log.error("ffmpeg stderr:\n%s", result.stderr[-2000:])
        raise RuntimeError(f"ffmpeg failed for clip {index} ({out_path.name})")
    if hook:  # posting kit next to the video
        out_path.with_suffix(".txt").write_text(
            f"{hook.get('post_title', '')}\n\n{hook.get('post_caption', '')}\n\n"
            + " ".join(hook.get("hashtags", [])) + "\n",
            encoding="utf-8",
        )
    shutil.rmtree(tmp, ignore_errors=True)
    return out_path


def _build_ffmpeg_cmd(
    mp4_path: Path,
    start: float,
    end: float,
    ass_path: Path | None,
    reframe: ReframeConfig,
    out_path: Path,
) -> list[str]:
    cmd = [
        _find_ffmpeg(),
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(mp4_path),
        "-t",
        f"{end - start:.3f}",
    ]
    if reframe.mode == "none" and ass_path is None:
        cmd += ["-c:v", "copy", "-c:a", "copy"]
    elif reframe.mode == "stacked":
        cmd += _stacked_encode_args(reframe, ass_path)
    else:
        vf = _video_filter(reframe, ass_path)
        cmd += [
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
        ]
    cmd += ["-movflags", "+faststart", str(out_path)]
    return cmd


def _video_filter(reframe: ReframeConfig, ass_path: Path | None) -> str:
    parts: list[str] = []

    if reframe.mode == "webcam" and reframe.webcam_rect:
        x, y, w, h = reframe.webcam_rect
        parts.append(f"crop={w}:{h}:{x}:{y},scale=1080:1920:flags=lanczos")
    elif reframe.mode != "none":
        parts.append("crop=ih*9/16:ih,scale=1080:1920:flags=lanczos")

    if ass_path is not None:
        ass_str = str(ass_path).replace("\\", "/").replace(":", "\\:")
        parts.append(f"subtitles='{ass_str}'")

    return ",".join(parts)


def _stacked_filter_complex(reframe: ReframeConfig, ass_path: Path | None) -> str:
    top_h = int(1920 * reframe.split_ratio)
    bot_h = 1920 - top_h

    if reframe.webcam_rect:
        x, y, w, h = reframe.webcam_rect
        top = f"[0:v]crop={w}:{h}:{x}:{y},scale=1080:{top_h}:flags=lanczos[top]"
    else:
        log.warning("reframe.webcam_rect not set — using center-crop for top panel")
        top = f"[0:v]crop=ih*9/16:ih,scale=1080:{top_h}:flags=lanczos[top]"

    if reframe.gameplay_rect:
        x, y, w, h = reframe.gameplay_rect
        bot = f"[0:v]crop={w}:{h}:{x}:{y},scale=1080:{bot_h}:flags=lanczos[bot]"
    else:
        bot = f"[0:v]crop=ih*9/16:ih,scale=1080:{bot_h}:flags=lanczos[bot]"

    if ass_path is not None:
        ass_str = str(ass_path).replace("\\", "/").replace(":", "\\:")
        stack = f"[top][bot]vstack,subtitles='{ass_str}'[out]"
    else:
        stack = "[top][bot]vstack[out]"

    return ";".join([top, bot, stack])


def _stacked_encode_args(reframe: ReframeConfig, ass_path: Path | None) -> list[str]:
    fc = _stacked_filter_complex(reframe, ass_path)
    return [
        "-filter_complex",
        fc,
        "-map",
        "[out]",
        "-map",
        "0:a",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
    ]


def _title_slug(title: str) -> str:
    """Filesystem-safe ASCII slug from a Spanish clip title."""
    normalized = unicodedata.normalize("NFKD", title)
    ascii_only = "".join(c for c in normalized if not unicodedata.combining(c))
    slug = ascii_only.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "_", slug)
    return slug.strip("_-")[:40] or "clip"
