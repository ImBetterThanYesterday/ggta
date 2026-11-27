"""Render a clip from its edit plan (see editplan.py).

Output timeline = [teaser (cold open)] + clip. The clip is cut into pieces wherever the
layout or a punch-in changes; every piece is composed at 1080x1920 and the pieces are
concatenated. Captions, hook and SFX are laid over the joined timeline; audio is mixed
and normalized to -14 LUFS / -1.5 dBTP (works on TikTok, Shorts, Reels and FB).
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import shutil
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path

from ..models.transcript import Transcript
from . import editplan
from .smart import OUT_H, OUT_W, detect_facecam, probe_size

log = logging.getLogger(__name__)

FPS = 30
CAP_H = 360  # caption PNG height (captions_png.H)


@dataclass
class Piece:
    src: int      # ffmpeg input index
    a: float      # start, seconds in that input
    b: float      # end
    layout: str
    zoom: float = 1.0
    target: str = "face"


# --------------------------------------------------------------------------- geometry


def _even(v: float) -> int:
    return max(2, int(v) & ~1)


def _fit(box_w: float, box_h: float, aspect: float) -> tuple[float, float]:
    """Largest (w, h) with `aspect` inside box_w x box_h."""
    return (box_h * aspect, box_h) if box_w / box_h > aspect else (box_w, box_w / aspect)


def cam_crop(cam: list[int], aspect: float, zoom: float = 1.0) -> tuple[int, int, int, int]:
    """Crop of `aspect` inside the facecam overlay, framed on the face, optionally zoomed."""
    x, y, w, h = cam
    cw, ch = _fit(w, h, aspect)
    cw, ch = cw / zoom, ch / zoom
    fx, fy = x + w / 2, y + h * 0.45  # face sits in the upper half of the cam crop
    cx = min(max(fx - cw / 2, x), x + w - cw)
    cy = min(max(fy - ch / 2, y), y + h - ch)
    return _even(cw), _even(ch), int(cx), int(cy)


def game_crop(W: int, H: int, cam: list[int] | None, aspect: float,
              zoom: float = 1.0) -> tuple[int, int, int, int]:
    """Biggest crop of `aspect` from the frame, centered, sliding away from the facecam."""
    gw, gh = _fit(W, H, aspect)
    gx, gy = (W - gw) / 2, (H - gh) / 2
    best, moved = None, False
    if cam:
        m = W * 0.03  # the overlay is usually a bit bigger than the detected crop
        x, y, w, h = cam[0] - m, cam[1] - m, cam[2] + 2 * m, cam[3] + 2 * m
        if gx < x + w and x < gx + gw and gy < y + h and y < gy + gh:  # crop shows the cam
            # wide crops can't slide past it: use the biggest region beside/above/below it
            for rx, ry, rw, rh in ((0, 0, x, H), (x + w, 0, W - x - w, H),
                                   (0, 0, W, y), (0, y + h, W, H - y - h)):
                if rw > 0 and rh > 0:
                    fw, fh = _fit(rw, rh, aspect)
                    if best is None or fw * fh > best[0]:
                        best = (fw * fh, rx + (rw - fw) / 2, ry + (rh - fh) / 2, fw, fh)
        if best and best[0] >= 0.55 * gw * gh:
            _, gx, gy, gw, gh = best
            gx, gy, moved = max(0.0, gx), max(0.0, gy), True
    if cam and not moved:
        x, _, w, _ = cam
        if x + w / 2 < W / 2 and gx < x + w and W - (x + w) >= gw:  # cam on the left
            gx = max(gx, x + w - gw / 6)
        elif x + w / 2 >= W / 2 and gx + gw > x >= gw:      # cam on the right
            gx = min(gx, x - gw + gw / 6)
    gx = min(max(0, gx), W - gw)
    if zoom > 1:
        nw, nh = gw / zoom, gh / zoom
        gx, gy = gx + (gw - nw) / 2, gy + (gh - nh) / 2
        gw, gh = nw, nh
    return _even(gw), _even(gh), int(gx), int(gy)


# (cam panel height, game first?) for the stacked layouts
_STACKS = {"stack": (700, False), "stack_50": (960, False), "cam_big": (1150, False),
           "square_game": (840, False), "game_top": (700, True)}


def layout_filter(src: str, out: str, layout: str, W: int, H: int, cam: list[int] | None,
                  zoom: float = 1.0, target: str = "face", tag: str = "") -> str:
    """Filter chain taking [src] (already trimmed) and producing [out] at 1080x1920."""
    zc = zoom if target in ("face", "frame") else 1.0
    zg = zoom if target in ("game", "frame") else 1.0
    end = f"fps={FPS},format=yuv420p,setsar=1[{out}]"
    if layout in _STACKS and cam:
        cam_h, game_first = _STACKS[layout]
        game_h = OUT_H - cam_h
        cw, ch, cx, cy = cam_crop(cam, OUT_W / cam_h, zc)
        gw, gh, gx, gy = game_crop(W, H, cam, OUT_W / game_h, zg)
        a, b = (f"g{tag}", f"c{tag}") if game_first else (f"c{tag}", f"g{tag}")
        return (f"[{src}]split[c{tag}i][g{tag}i];"
                f"[c{tag}i]crop={cw}:{ch}:{cx}:{cy},scale={OUT_W}:{cam_h}:flags=lanczos,"
                f"unsharp=5:5:0.6:5:5:0.0,setsar=1[c{tag}];"
                f"[g{tag}i]crop={gw}:{gh}:{gx}:{gy},scale={OUT_W}:{game_h}:flags=lanczos,setsar=1[g{tag}];"
                f"[{a}][{b}]vstack,{end}")
    if layout == "game_pip" and cam:
        gw, gh, gx, gy = game_crop(W, H, cam, OUT_W / OUT_H, zg)
        pw = 470
        cw, ch, cx, cy = cam_crop(cam, 1.35, zc)
        ph = _even(pw / 1.35)
        return (f"[{src}]split[g{tag}i][c{tag}i];"
                f"[g{tag}i]crop={gw}:{gh}:{gx}:{gy},scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1[g{tag}];"
                f"[c{tag}i]crop={cw}:{ch}:{cx}:{cy},scale={pw}:{ph}:flags=lanczos,"
                f"pad={pw + 12}:{ph + 12}:6:6:white,setsar=1[c{tag}];"
                f"[g{tag}][c{tag}]overlay={OUT_W - pw - 12 - 36}:310,{end}")
    if layout == "game_full" or (layout in _STACKS or layout == "game_pip"):
        gw, gh, gx, gy = game_crop(W, H, cam, OUT_W / OUT_H, max(zg, zc))
        return f"[{src}]crop={gw}:{gh}:{gx}:{gy},scale={OUT_W}:{OUT_H}:flags=lanczos,{end}"
    # fit_blur: whole frame 1.25x wide over a blurred copy of itself
    fg_w = _even(OUT_W * 1.25 * max(zg, zc))
    return (f"[{src}]split[a{tag}][b{tag}];"
            f"[a{tag}]scale=-2:{OUT_H},crop={OUT_W}:{OUT_H},boxblur=24:2,eq=brightness=-0.08[bg{tag}];"
            f"[b{tag}]scale={fg_w}:-2:flags=lanczos,crop={OUT_W}:ih[fg{tag}];"
            f"[bg{tag}][fg{tag}]overlay=(W-w)/2:(H-h)/2,{end}")


def positions(layout: str, has_cam: bool) -> tuple[int, int]:
    """(caption PNG top y, hook centre y) for a layout. Inside the 290–1440 safe zone."""
    if layout in _STACKS and has_cam:
        cam_h, game_first = _STACKS[layout]
        seam = OUT_H - cam_h if game_first else cam_h
        return min(seam - 60, 1440 - CAP_H + 60), seam
    if layout == "fit_blur":
        return 1180, 520
    return 1050, 640


# --------------------------------------------------------------------------- timeline


def main_pieces(plan: dict, dur: float, src: int = 0) -> list[Piece]:
    """Cut [0, dur] wherever the layout or a punch-in changes."""
    cuts = {0.0, dur}
    for s in plan["segments"]:
        cuts.add(min(max(s["from"], 0.0), dur))
    for p in plan["punch_ins"]:
        cuts.add(p["t"])
        cuts.add(min(dur, p["t"] + p["dur"]))
    pts = sorted(cuts)
    pieces: list[Piece] = []
    for a, b in itertools.pairwise(pts):
        if b - a < 0.25 and pieces:  # sliver: extend the previous piece
            pieces[-1].b = b
            continue
        lay = [s for s in plan["segments"] if s["from"] <= a + 1e-6][-1]["layout"]
        pin = next((p for p in plan["punch_ins"] if p["t"] - 1e-6 <= a < p["t"] + p["dur"] - 1e-6), None)
        pieces.append(Piece(src, a, b, lay, pin["zoom"] if pin else 1.0, pin["target"] if pin else "face"))
    # merge neighbours that look the same
    merged: list[Piece] = []
    for p in pieces:
        if merged and (merged[-1].layout, merged[-1].zoom, merged[-1].target) == (p.layout, p.zoom, p.target):
            merged[-1].b = p.b
        else:
            merged.append(p)
    return merged


def teaser_layout(plan: dict, has_cam: bool) -> str:
    if plan.get("hook_type") == "watch_face" and has_cam:
        return "cam_big"
    lay = [s for s in plan["segments"] if s["from"] <= plan["payoff_t"] + 1e-6]
    return lay[-1]["layout"] if lay else plan["segments"][0]["layout"]


def _piecewise(points: list[tuple[float, int]]) -> str:
    """ffmpeg expression of t: value changes at each (t_start, value)."""
    expr = str(points[-1][1])
    for (_, v), (t_next, _) in zip(reversed(points[:-1]), reversed(points[1:])):
        expr = f"if(lt(t,{t_next:.3f}),{v},{expr})"
    return expr


# --------------------------------------------------------------------------- signals


def audio_peaks(mp4: Path, start: float, dur: float, top: int = 10) -> list[tuple[float, float]]:
    """Loud moments of the clip: (seconds from clip start, dB above the clip's median)."""
    import numpy as np

    raw = subprocess.run(  # nosec B603
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(mp4),
         "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True, check=False,
    ).stdout
    if len(raw) < 16000:
        return []
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    hop = 2000  # 0.25 s
    n = len(x) // hop
    db = 20 * np.log10(np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1)) + 1e-6)
    med = float(np.median(db))
    order = np.argsort(-db)
    peaks: list[tuple[float, float]] = []
    for i in order:
        t = i * 0.25 + 0.125
        if db[i] - med < 4:
            break
        if all(abs(t - p[0]) >= 1.5 for p in peaks):
            peaks.append((round(float(t), 2), round(float(db[i] - med), 1)))
        if len(peaks) >= top:
            break
    return peaks


def _has_audio(mp4: Path) -> bool:
    out = subprocess.run(  # nosec B603
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", str(mp4)], capture_output=True, text=True,
    ).stdout.strip()
    return bool(out)


def _snap_punches(plan: dict, peaks: list[tuple[float, float]]) -> None:
    """Land each punch-in on the nearest loud moment (±0.6 s), a few frames early."""
    for p in plan["punch_ins"]:
        near = [t for t, _ in peaks if abs(t - p["t"]) <= 0.6]
        if near:
            p["t"] = round(max(0.5, min(near, key=lambda t: abs(t - p["t"])) - 0.08), 2)


# --------------------------------------------------------------------------- planning


def _context(mp4: Path) -> dict:
    ctx = mp4.parent / "context.json"
    return json.loads(ctx.read_text(encoding="utf-8")) if ctx.exists() else {}


def _texts(mp4: Path, transcript: Transcript, start: float, end: float) -> tuple[str, str]:
    text = "\n".join(
        f"[{max(0.0, s.start - start):.1f}] {s.text.strip()}"
        for s in transcript.segments if s.end >= start and s.start <= end
    )
    chat = ""
    cp = mp4.parent / "chat.json"
    if cp.exists():
        msgs = json.loads(cp.read_text(encoding="utf-8")).get("messages", [])
        chat = " | ".join(f"{m['time_in_seconds'] - start:.0f}s {m['message']}" for m in msgs
                          if start <= m["time_in_seconds"] <= end + 8)
    return text, chat


def make_plan(mp4: Path, transcript: Transcript, start: float, end: float, index: int,
              config, pr=None, *, has_cam: bool, history: list[dict],
              peaks: list[tuple[float, float]]) -> dict:
    """Cached edit_<index>.json next to the source, or a new plan from the LLM/heuristic."""
    cache = mp4.parent / f"edit_{index:02d}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    dur = end - start
    seed = int(start * 10) + index
    ctx = _context(mp4)
    if config.edit.llm and pr is not None:
        text, chat = _texts(mp4, transcript, start, end)
        plan, errors = editplan.generate_plan(
            dur=dur, has_cam=has_cam, streamer=ctx.get("streamer", ""),
            stream_title=ctx.get("title", ""), reason=f"{pr.pick.title_es}. {pr.pick.reason}",
            transcript=text, chat=chat, peaks=peaks,
            lang={"en": "English", "es": "Spanish", "pt": "Portuguese"}.get(ctx.get("lang", ""),
                                                                            config.hook.language),
            history=history, provider=config.edit.provider or config.hook.provider, seed=seed,
        )
    else:
        plan, errors = editplan.validate(editplan.fallback_plan(dur, has_cam, peaks, history, seed),
                                         dur, has_cam, history, seed)
        plan["source"] = "fallback"
    if not config.edit.cold_open:
        plan["cold_open"] = False
    if not plan.get("hook_text"):  # heuristic plans reuse the classic hook generator
        from .clipper import _load_or_make_hook

        hook = _load_or_make_hook(mp4, transcript, start, end, index, config, pr) or {}
        for k in ("hook_text", "voiceover", "post_title", "post_caption", "hashtags"):
            plan.setdefault(k, hook.get(k, "" if k != "hashtags" else []))
        if not plan.get("cold_open"):
            plan["hook_style"] = hook.get("hook_style", plan.get("hook_style", "text_only"))
    _snap_punches(plan, peaks)
    plan["validator_errors"] = errors
    cache.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    hook_file = mp4.parent / f"hook_{index:02d}.json"
    if not hook_file.exists() and plan.get("hook_text"):  # publicar.txt reads post fields from here
        hook_file.write_text(json.dumps({k: plan.get(k) for k in (
            "hook_style", "hook_text", "voiceover", "post_title", "post_caption", "hashtags")},
            ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("edit plan (%s): %s", plan.get("source"), editplan.summary(plan))
    return plan


# --------------------------------------------------------------------------- render


def render_directed(mp4_path: Path, transcript: Transcript, start: float, end: float, index: int,
                    out_path: Path, config, pr=None) -> Path:
    from ..io.media import video_duration
    from .captions_png import write_caption_track
    from .clipper import _find_ffmpeg
    from .hook import render_hook_png, synth_voice
    from .sfx import gain_db, sfx_path

    end = min(end, video_duration(mp4_path))
    dur = end - start
    W, H = probe_size(mp4_path)
    cam = detect_facecam(mp4_path, mp4_path.parent)
    has_cam = bool(cam)
    history = editplan.recent_history(config.out_dir.expanduser(), config.edit.history)
    peaks = audio_peaks(mp4_path, start, dur)
    plan = make_plan(mp4_path, transcript, start, end, index, config, pr,
                     has_cam=has_cam, history=history, peaks=peaks)
    plan, _ = editplan.validate(plan, dur, has_cam, [], 0)  # re-check cached plans (layouts vs cam)

    tmp = (out_path.parent / f".work_{out_path.stem}").resolve()
    tmp.mkdir(parents=True, exist_ok=True)
    ctx = _context(mp4_path)
    streamer = ctx.get("streamer", "")

    cold = bool(plan.get("cold_open"))
    tz = plan["teaser"]
    tl = (tz["to"] - tz["from"]) if cold else 0.0
    total = tl + dur

    # ---- inputs
    cmd = [_find_ffmpeg(), "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(mp4_path)]
    n_in = 1
    teaser_i = None
    if cold:
        cmd += ["-ss", f"{start + tz['from']:.3f}", "-t", f"{tl:.3f}", "-i", str(mp4_path)]
        teaser_i, n_in = n_in, n_in + 1

    # hook
    text = (plan.get("hook_text") or "").strip()
    if streamer and text:  # LLMs misspell names: force the exact spelling
        pat = re.sub(r"(.)", lambda m: re.escape(m.group(1)) + "+", streamer.lower())
        text = re.sub(pat, streamer.upper(), text, flags=re.IGNORECASE)
    look = plan.get("hook_look", "white_box")
    hook_dur, hook_i, vo_i = 0.0, None, None
    if text:
        png = tmp / "hook.png"
        render_hook_png(text, png, look)
        read = min(3.4, max(2.2, len(text.split()) / 4 + 0.9))
        if plan.get("hook_style") == "voice_text" and not cold and plan.get("voiceover"):
            vo = plan["voiceover"]
            for name, spoken in config.hook.pronounce.items():
                vo = re.sub(re.escape(name), spoken, vo, flags=re.IGNORECASE)
            vo_path = tmp / "vo.mp3"
            voice = config.hook.voices.get(ctx.get("lang", ""), config.hook.voice)
            hook_dur = synth_voice(vo, vo_path, voice, config.hook.rate) + 0.35
        else:
            hook_dur = max(tl, read) if cold else read
        hook_dur = min(hook_dur, total)
        shown = total if look == "top_bar" else hook_dur
        cmd += ["-loop", "1", "-t", f"{shown:.2f}", "-i", str(png)]
        hook_i, n_in = n_in, n_in + 1
        if plan.get("hook_style") == "voice_text" and not cold and (tmp / "vo.mp3").exists():
            cmd += ["-i", str(tmp / "vo.mp3")]
            vo_i, n_in = n_in, n_in + 1

    # captions (skip while the hook covers the middle of the screen)
    cap_i = None
    if config.caption.enabled:
        skip = 0.0 if look == "top_bar" else max(0.0, hook_dur - tl)
        track = write_caption_track(transcript, start, end, tmp / "caps", skip_until=skip,
                                    preset=plan.get("caption_preset", "yellow_pop"),
                                    emphasis=plan.get("emphasis", []), offset=tl)
        if track:
            cmd += ["-f", "concat", "-safe", "0", "-i", str(track)]
            cap_i, n_in = n_in, n_in + 1

    # sfx (plan times are clip-relative -> shift by the teaser) + whoosh on the rewind
    cues = [(s["t"] + tl, s["name"]) for s in plan.get("sfx", [])]
    if cold:
        cues.append((max(0.0, tl - 0.3), "whoosh"))
    sfx_inputs: list[tuple[int, float, str]] = []
    for t, name in sorted(cues):
        f = sfx_path(name, config.work_dir.expanduser() / "sfx")
        if f:
            cmd += ["-i", str(f)]
            sfx_inputs.append((n_in, t, name))
            n_in += 1

    # ---- video graph
    pieces = main_pieces(plan, dur)
    if cold:
        pieces = [Piece(teaser_i, 0.0, tl, teaser_layout(plan, has_cam))] + pieces
    chains: list[str] = []
    main_n = sum(1 for p in pieces if p.src == 0)
    chains.append(f"[0:v]fps={FPS},split={main_n}" + "".join(f"[m{i}]" for i in range(main_n)))
    labels, mi, offs, t_out = [], 0, [], 0.0
    for k, p in enumerate(pieces):
        if p.src == 0:
            src = f"m{mi}t"
            chains.append(f"[m{mi}]trim=start={p.a:.3f}:end={p.b:.3f},setpts=PTS-STARTPTS[{src}]")
            mi += 1
        else:
            src = f"t{k}"
            chains.append(f"[{p.src}:v]fps={FPS},setpts=PTS-STARTPTS[{src}]")
        chains.append(layout_filter(src, f"p{k}", p.layout, W, H, cam, p.zoom, p.target, tag=str(k)))
        labels.append(f"[p{k}]")
        offs.append((t_out, p.layout))
        t_out += p.b - p.a
    chains.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[base]")
    cur = "[base]"
    if cold:  # white flash on the rewind
        chains.append(f"{cur}eq=brightness=0.55:enable='between(t,{tl:.3f},{tl + 0.1:.3f})'[fl]")
        cur = "[fl]"
    if cap_i is not None:
        ys = [(t, positions(lay, has_cam)[0]) for t, lay in offs]
        chains.append(f"{cur}[{cap_i}:v]overlay=(W-w)/2:'{_piecewise(ys)}':eval=frame:"
                      f"eof_action=pass:format=auto[cp]")
        cur = "[cp]"
    if hook_i is not None:
        if look == "top_bar":
            chains.append(f"{cur}[{hook_i}:v]overlay=(W-w)/2:300:eof_action=pass:format=auto[hk]")
        else:
            hy = positions(pieces[0].layout, has_cam)[1]
            chains.append(f"{cur}[{hook_i}:v]overlay=(W-w)/2:{hy}-h/2:enable='lt(t,{hook_dur:.2f})':"
                          f"eof_action=pass:format=auto[hk]")
        cur = "[hk]"
    chains.append(f"{cur}null[out]")

    # ---- audio graph
    amap = None
    if _has_audio(mp4_path):
        if cold:
            chains.append(f"[{teaser_i}:a]asetpts=PTS-STARTPTS[ta];[0:a]asetpts=PTS-STARTPTS[ma];"
                          f"[ta][ma]concat=n=2:v=0:a=1[game]")
        else:
            chains.append("[0:a]asetpts=PTS-STARTPTS[game]")
        mix = ["[game]"]
        if vo_i is not None:
            chains.append(f"[game]volume='if(lt(t,{hook_dur:.2f}),0.18,1)':eval=frame[gd]")
            chains.append(f"[{vo_i}:a]adelay=120|120,volume=1.8[vo]")
            mix = ["[gd]", "[vo]"]
        for i, t, name in sfx_inputs:
            ms = int(t * 1000)
            chains.append(f"[{i}:a]aformat=channel_layouts=stereo,adelay={ms}|{ms},"
                          f"volume={gain_db(name)}dB[s{i}]")
            mix.append(f"[s{i}]")
        chains.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0,"
                      "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")
        amap = "[aout]"

    cmd += ["-filter_complex", ";".join(chains), "-map", "[out]"]
    if amap:
        cmd += ["-map", amap, "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "17", "-profile:v", "high",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-t", f"{total:.3f}",
            "-movflags", "+faststart", str(out_path)]
    log.info("clip %d [%.1f-%.1fs] -> %s (directed: %s)", index, start, end, out_path.name,
             editplan.summary(plan))
    result = subprocess.run(cmd, capture_output=True, text=True)  # nosec B603
    if result.returncode != 0:
        log.error("ffmpeg stderr:\n%s", result.stderr[-2500:])
        raise RuntimeError(f"ffmpeg failed for clip {index} ({out_path.name})")

    saved = dict(plan, summary=editplan.summary(plan), has_cam=has_cam, duration=round(total, 2))
    out_path.with_suffix(".edit.json").write_text(json.dumps(saved, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")
    if plan.get("hook_text"):  # posting kit next to the video
        out_path.with_suffix(".txt").write_text(
            f"{plan.get('post_title', '')}\n\n{plan.get('post_caption', '')}\n\n"
            + " ".join(plan.get("hashtags", [])) + "\n", encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    return out_path
