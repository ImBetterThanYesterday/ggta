"""Edit plan: one editorial decision per clip instead of one template for all.

Claude (or the fallback heuristic) writes a small JSON plan:

    {
      "payoff_t": 23.4,                       # the moment the clip is about (clip seconds)
      "cold_open": true,                      # show the payoff first, then rewind
      "teaser": {"from": 22.2, "to": 24.4},   # what plays before the rewind
      "segments": [{"from": 0, "layout": "stack"}, {"from": 21.5, "layout": "cam_big"}],
      "punch_ins": [{"t": 23.4, "dur": 1.2, "target": "face", "zoom": 1.2}],
      "sfx": [{"t": 23.4, "name": "boom"}],
      "caption_preset": "boxed", "emphasis": ["PERRO"],
      "hook_type": "watch_face", "hook_look": "black_box",
      "hook_text": "...", "voiceover": "...", ...post fields
    }

All times are relative to the start of the clip window (before the cold open is
prepended). `validate` clamps, enforces effect budgets and variety against recent clips,
and returns the structured errors it fixed, which are sent back to the model once.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- catalogue

LAYOUTS: dict[str, dict] = {
    "stack": {"cam": True, "desc": "facecam top (700px), gameplay bottom. Neutral default."},
    "stack_50": {"cam": True, "desc": "facecam and gameplay half and half. Talky moments."},
    "cam_big": {"cam": True, "desc": "big facecam (47%), gameplay below. Reactions, screaming, laughing."},
    "game_top": {"cam": True, "desc": "gameplay top, facecam bottom. When the game action leads."},
    "square_game": {"cam": True, "desc": "facecam top, sharp square gameplay. Driving, chases, aiming."},
    "game_pip": {"cam": True, "desc": "full-screen gameplay with a small floating facecam. Big game action."},
    "game_full": {"cam": False, "desc": "full-screen gameplay crop, no facecam. Pure game spectacle."},
    "fit_blur": {"cam": False, "desc": "whole 16:9 frame over a blurred copy. When context/UI matters."},
}
HOOK_TYPES: dict[str, str] = {
    "headline": "breaking-news statement of the absurd thing (\"SPEED ROBA UN AVIÓN LLENO DE GENTE\")",
    "stakes": "numbers / what is at stake (\"1 VIDA, 5 PATRULLAS Y CERO PLAN\")",
    "curiosity": "specific curiosity gap, never vague (\"NADIE ESPERABA LO QUE HIZO CON EL PERRO\")",
    "question": "provocative question that makes people comment (\"¿ESTO ES NORMAL EN GTA 6?\")",
    "watch_face": "point at the reaction (\"MIRA SU CARA CUANDO…\"); best with cold_open",
    "pov": "meme POV framing (\"POV: le prestas el carro a tu primo\")",
}
HOOK_LOOKS = ("white_box", "black_box", "yellow_box", "outline", "top_bar")
CAPTION_PRESETS = ("yellow_pop", "green_pop", "boxed", "single_word", "clean", "din_caps")
SFX = ("whoosh", "boom", "riser", "pop")
TARGETS = ("face", "game", "frame")

# budgets (from the research report; tune with real numbers from the ledger)
MIN_SEGMENT_S = 1.5
SWITCHES_PER_30S = 3
PUNCHES_PER_40S = 4
MAX_SFX = 3
TEASER_MIN, TEASER_MAX = 2.5, 5.0
COLD_OPEN_MIN_PAYOFF = 8.0  # a payoff in the first seconds needs no rewind

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "payoff_t": {"type": "number"},
        "cold_open": {"type": "boolean"},
        "teaser": {"type": "object", "properties": {"from": {"type": "number"}, "to": {"type": "number"}},
                   "required": ["from", "to"], "additionalProperties": False},
        "segments": {"type": "array", "items": {
            "type": "object",
            "properties": {"from": {"type": "number"}, "layout": {"type": "string", "enum": list(LAYOUTS)}},
            "required": ["from", "layout"], "additionalProperties": False}},
        "punch_ins": {"type": "array", "items": {
            "type": "object",
            "properties": {"t": {"type": "number"}, "dur": {"type": "number"},
                           "target": {"type": "string", "enum": list(TARGETS)}, "zoom": {"type": "number"}},
            "required": ["t", "dur", "target", "zoom"], "additionalProperties": False}},
        "sfx": {"type": "array", "items": {
            "type": "object",
            "properties": {"t": {"type": "number"}, "name": {"type": "string", "enum": list(SFX)}},
            "required": ["t", "name"], "additionalProperties": False}},
        "caption_preset": {"type": "string", "enum": list(CAPTION_PRESETS)},
        "emphasis": {"type": "array", "items": {"type": "string"}},
        "hook_type": {"type": "string", "enum": list(HOOK_TYPES)},
        "hook_look": {"type": "string", "enum": list(HOOK_LOOKS)},
        "hook_style": {"type": "string", "enum": ["voice_text", "text_only"]},
        "hook_text": {"type": "string"},
        "voiceover": {"type": "string"},
        "post_title": {"type": "string"},
        "post_caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "why": {"type": "string"},
    },
    "required": ["payoff_t", "cold_open", "teaser", "segments", "punch_ins", "sfx", "caption_preset",
                 "emphasis", "hook_type", "hook_look", "hook_style", "hook_text", "voiceover",
                 "post_title", "post_caption", "hashtags", "why"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- history


def recent_history(out_dir: Path, n: int = 8) -> list[dict]:
    """Most recent edit plans (newest first) from out/<stream>/<clip>/edit.json."""
    files = sorted(out_dir.glob("*/*/edit.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    hist = []
    for f in files[:n]:
        try:
            hist.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return hist


def _least_recent(options: tuple[str, ...] | list[str], used: list[str], seed: int = 0) -> str:
    """Option not used for the longest time; ties broken by `seed` so batches differ."""
    def age(o: str) -> int:
        return used.index(o) if o in used else len(used) + ((sum(map(ord, o)) + seed) % 7)
    return max(options, key=age)


# --------------------------------------------------------------------------- validation


def validate(plan: dict, dur: float, has_cam: bool, history: list[dict] | None = None,
             seed: int = 0) -> tuple[dict, list[str]]:
    """Return (fixed plan, errors found). The plan is always renderable afterwards."""
    errors: list[str] = []
    history = history or []
    p = json.loads(json.dumps(plan))  # deep copy
    dur = max(1.0, float(dur))

    # payoff + cold open
    payoff = float(p.get("payoff_t", dur * 0.6))
    if not 0 <= payoff <= dur:
        errors.append(f"payoff_t {payoff:.1f} outside the clip (0–{dur:.1f})")
        payoff = min(max(payoff, 0.0), dur)
    p["payoff_t"] = round(payoff, 2)
    if p.get("cold_open") and payoff < COLD_OPEN_MIN_PAYOFF:
        errors.append(f"cold_open with payoff at {payoff:.1f}s: the payoff already opens the clip")
        p["cold_open"] = False
    tz = p.get("teaser") or {}
    a, b = float(tz.get("from", payoff - 1.4)), float(tz.get("to", payoff + 0.6))
    if p.get("cold_open"):
        if b - a < TEASER_MIN or b - a > TEASER_MAX:
            errors.append(f"teaser lasts {b - a:.1f}s; must be {TEASER_MIN}–{TEASER_MAX}s")
        mid = min(max(payoff, (a + b) / 2), dur)
        ln = min(max(b - a, TEASER_MIN), TEASER_MAX)
        a = max(0.0, min(mid - ln * 0.6, dur - ln))
        b = min(dur, a + ln)
    p["teaser"] = {"from": round(a, 2), "to": round(b, 2)}

    # segments
    segs = sorted(p.get("segments") or [], key=lambda s: float(s.get("from", 0)))
    fixed: list[dict] = []
    for s in segs:
        lay = s.get("layout", "stack")
        if lay not in LAYOUTS:
            errors.append(f"unknown layout {lay!r}")
            lay = "stack" if has_cam else "fit_blur"
        if LAYOUTS[lay]["cam"] and not has_cam:
            errors.append(f"layout {lay!r} needs a facecam and this stream has none")
            lay = "game_full" if lay in ("game_pip", "square_game", "game_top") else "fit_blur"
        t = min(max(float(s.get("from", 0)), 0.0), dur)
        if fixed and t - fixed[-1]["from"] < MIN_SEGMENT_S:
            errors.append(f"segment at {t:.1f}s is less than {MIN_SEGMENT_S}s after the previous one")
            continue
        if fixed and fixed[-1]["layout"] == lay:
            continue  # merge identical neighbours
        if dur - t < MIN_SEGMENT_S and fixed:
            errors.append(f"segment at {t:.1f}s leaves less than {MIN_SEGMENT_S}s before the end")
            continue
        fixed.append({"from": round(t, 2), "layout": lay})
    if not fixed:
        fixed = [{"from": 0.0, "layout": "stack" if has_cam else "fit_blur"}]
    fixed[0]["from"] = 0.0
    max_sw = max(1, round(dur / 30 * SWITCHES_PER_30S))
    if len(fixed) - 1 > max_sw:
        errors.append(f"{len(fixed) - 1} layout switches; at most {max_sw} for a {dur:.0f}s clip")
        fixed = fixed[: max_sw + 1]
    p["segments"] = fixed

    # punch-ins
    pins: list[dict] = []
    max_p = max(1, math.ceil(dur / 40 * PUNCHES_PER_40S))
    for pi in sorted(p.get("punch_ins") or [], key=lambda x: float(x.get("t", 0))):
        t, d = float(pi.get("t", 0)), float(pi.get("dur", 1.2))
        z = float(pi.get("zoom", 1.2))
        tgt = pi.get("target", "face")
        if tgt not in TARGETS or (tgt == "face" and not has_cam):
            tgt = "frame" if not has_cam else "face"
        if not 1.1 <= z <= 1.35:
            errors.append(f"punch-in zoom {z:.2f} at {t:.1f}s; use 1.15–1.3")
            z = min(max(z, 1.12), 1.3)
        d = min(max(d, 0.6), 2.5)
        if t < 0.5 or t + 0.3 > dur:
            errors.append(f"punch-in at {t:.1f}s is outside 0.5s–end")
            continue
        if pins and t < pins[-1]["t"] + pins[-1]["dur"] + 1.0:
            errors.append(f"punch-in at {t:.1f}s overlaps/too close to the previous one")
            continue
        if len(pins) >= max_p:
            errors.append(f"more than {max_p} punch-ins for a {dur:.0f}s clip")
            break
        pins.append({"t": round(t, 2), "dur": round(min(d, dur - t), 2), "target": tgt, "zoom": round(z, 2)})
    p["punch_ins"] = pins

    # sfx
    sfx: list[dict] = []
    for s in sorted(p.get("sfx") or [], key=lambda x: float(x.get("t", 0))):
        if s.get("name") not in SFX:
            errors.append(f"unknown sfx {s.get('name')!r}")
            continue
        t = float(s.get("t", 0))
        if not 0 <= t <= dur or (sfx and t - sfx[-1]["t"] < 2.0) or len(sfx) >= MAX_SFX:
            errors.append(f"sfx at {t:.1f}s dropped (outside clip, <2s apart or over {MAX_SFX})")
            continue
        sfx.append({"t": round(t, 2), "name": s["name"]})
    p["sfx"] = sfx

    # variety vs. the last clips
    used_caps = [h.get("caption_preset", "") for h in history]
    used_hooks = [h.get("hook_type", "") for h in history]
    used_looks = [h.get("hook_look", "") for h in history]
    if p.get("caption_preset") not in CAPTION_PRESETS or p["caption_preset"] in used_caps[:2]:
        if p.get("caption_preset") in used_caps[:2]:
            errors.append(f"caption_preset {p['caption_preset']!r} used in the last 2 clips")
        p["caption_preset"] = _least_recent(CAPTION_PRESETS, used_caps, seed)
    if p.get("hook_type") not in HOOK_TYPES:
        p["hook_type"] = _least_recent(tuple(HOOK_TYPES), used_hooks, seed)
    elif p["hook_type"] in used_hooks[:2]:
        errors.append(f"hook_type {p['hook_type']!r} used in the last 2 clips; pick another")
    if p.get("hook_look") not in HOOK_LOOKS or p["hook_look"] in used_looks[:1]:
        p["hook_look"] = _least_recent(HOOK_LOOKS, used_looks, seed)
    if p.get("cold_open"):
        p["hook_style"] = "text_only"  # never talk over the payoff
    p["emphasis"] = [w.strip().upper() for w in p.get("emphasis") or [] if w.strip()][:4]
    return p, errors


# --------------------------------------------------------------------------- fallback


def fallback_plan(dur: float, has_cam: bool, peaks: list[tuple[float, float]],
                  history: list[dict] | None = None, seed: int = 0) -> dict:
    """Heuristic plan when the LLM is off or fails: payoff = loudest late peak."""
    history = history or []
    late = [pk for pk in peaks if pk[0] >= dur * 0.25] or peaks or [(dur * 0.6, 0.0)]
    payoff = max(late, key=lambda x: x[1])[0]
    first_layouts = [h.get("segments", [{}])[0].get("layout", "") for h in history]
    opener = _least_recent(("stack", "square_game", "stack_50", "game_top") if has_cam
                           else ("fit_blur", "game_full"), first_layouts, seed)
    react = "cam_big" if has_cam else "game_full"
    segs = [{"from": 0.0, "layout": opener}]
    if payoff - 1.5 > MIN_SEGMENT_S:
        segs.append({"from": payoff - 1.5, "layout": react})
        if dur - (payoff + 2.5) > MIN_SEGMENT_S:
            segs.append({"from": payoff + 2.5, "layout": opener})
    return {
        "payoff_t": payoff,
        "cold_open": bool(payoff >= 10),
        "teaser": {"from": payoff - 2.4, "to": payoff + 1.6},
        "segments": segs,
        "punch_ins": [{"t": payoff, "dur": 1.2, "target": "face" if has_cam else "frame", "zoom": 1.2}],
        "sfx": [{"t": payoff, "name": "boom"}],
        "caption_preset": _least_recent(CAPTION_PRESETS, [h.get("caption_preset", "") for h in history], seed),
        "emphasis": [],
        "hook_type": _least_recent(tuple(HOOK_TYPES), [h.get("hook_type", "") for h in history], seed),
        "hook_look": _least_recent(HOOK_LOOKS, [h.get("hook_look", "") for h in history], seed),
        "hook_style": "text_only",
        "why": "fallback heuristic",
    }


# --------------------------------------------------------------------------- LLM director

DIRECTOR_PROMPT = """\
You are the editor of a viral gaming-clips page (GTA 6 streamer clips for TikTok, Reels, Shorts).
Decide how THIS clip is edited, so it doesn't look like every other clip page, and write its hook.

What retains viewers (use it):
- Payoff first: if the best moment is later than ~8 s, set cold_open=true and pick a 2.5–5 s
  `teaser` around the payoff: start ~2 s BEFORE the peak so the viewer understands what happens,
  end ~1.5 s after the reaction. It plays first, then the clip rewinds to the start. With
  cold_open the hook text sits over the teaser (no voice-over).
- Change the composition WITH the events, not on a timer: context layout first, the action
  layout while things happen, a reaction layout (cam_big) for screaming/laughing. 0–3 switches
  per 30 s, never shorter than 1.5 s.
- Punch-ins: 15–25 % zoom (zoom 1.15–1.25) landing ON the reaction peak / key word, 0.8–1.6 s,
  at most 4 per 40 s, none in the first 0.5 s. target "face" for reactions, "game" for action.
- One accent per real beat, never stacked: "boom" on the payoff hit, "whoosh" on a big switch,
  "riser" before a reveal, "pop" on a joke. At most 3 and ≥2 s apart; zero is fine.
- emphasis: 1–3 key words of the transcript to colour in the captions (the absurd noun/verb).
Variety: the last clips used these — avoid repeating hook_type, hook_look and caption_preset:
{history}

Layouts (cam=needs facecam; this stream {cam_note}):
{layouts}
Hook types:
{hook_types}
hook_look: one of {looks}. caption_preset: one of {captions}.

Hook text rules: {hook_rules}

Language for all text: {lang}. Times are seconds from the clip start; the clip lasts {dur:.1f} s.
Streamer: {streamer}. Stream: {stream_title}
Why this moment was picked: {reason}
Transcript (seconds from clip start):
{transcript}
Chat (seconds from clip start; chat reacts 2–5 s AFTER the moment):
{chat}
Loudest audio moments (s: dB above the clip's median): {peaks}
{errors}"""

HOOK_RULES = (
    "hook_text 3–7 words UPPERCASE, concrete and grounded in what really happens (exaggerate the "
    "framing, never invent events, never accuse the real person of real crimes). Lead with the "
    "streamer's exact name unless the hook type reads better without it. voiceover 6–11 words "
    "(used only when hook_style=voice_text: quiet openings that need context; it must add context "
    "or opinion, not describe what is on screen). post_title ≤70 chars; post_caption 1–2 lines with "
    "a question; hashtags 5–8 incl. #gta6 #gtavi and the streamer."
)


def _fmt_history(history: list[dict]) -> str:
    if not history:
        return "  (no previous clips)"
    rows = []
    for h in history[:6]:
        lays = ">".join(s.get("layout", "?") for s in h.get("segments", []))
        rows.append(f"  - hook_type={h.get('hook_type')} hook_look={h.get('hook_look')} "
                    f"captions={h.get('caption_preset')} cold_open={h.get('cold_open')} layouts={lays}")
    return "\n".join(rows)


def generate_plan(*, dur: float, has_cam: bool, streamer: str, stream_title: str, reason: str,
                  transcript: str, chat: str, peaks: list[tuple[float, float]], lang: str,
                  history: list[dict], provider: str, seed: int = 0) -> tuple[dict, list[str]]:
    """Ask the LLM for a plan; one retry with the validator's errors; fallback on failure."""
    from ..llm.subscription_provider import ask_json

    base = {
        "history": _fmt_history(history),
        "cam_note": "HAS a facecam" if has_cam else "has NO facecam: only cam=False layouts",
        "layouts": "\n".join(f"  - {k} (cam={v['cam']}): {v['desc']}" for k, v in LAYOUTS.items()),
        "hook_types": "\n".join(f"  - {k}: {v}" for k, v in HOOK_TYPES.items()),
        "looks": ", ".join(HOOK_LOOKS), "captions": ", ".join(CAPTION_PRESETS), "hook_rules": HOOK_RULES,
        "lang": lang, "dur": dur, "streamer": streamer, "stream_title": stream_title, "reason": reason,
        "transcript": transcript[:3500], "chat": chat[:1800],
        "peaks": ", ".join(f"{t:.1f}s:+{db:.0f}" for t, db in sorted(peaks, key=lambda x: -x[1])[:8]),
    }
    errors: list[str] = []
    for attempt in range(2):
        extra = ("\nYour previous plan had these problems; fix them:\n- " + "\n- ".join(errors)) if errors else ""
        try:
            raw = ask_json(DIRECTOR_PROMPT.format(**base, errors=extra), PLAN_SCHEMA, provider=provider)
        except Exception as exc:
            log.warning("edit plan: LLM failed (%s) -> heuristic plan", exc)
            break
        plan, errors = validate(raw, dur, has_cam, history, seed)
        if not errors or attempt == 1:
            plan["source"] = "llm"
            return plan, errors
        log.info("edit plan: retrying with %d validator errors", len(errors))
    plan, errs = validate(fallback_plan(dur, has_cam, peaks, history, seed), dur, has_cam, history, seed)
    plan["source"] = "fallback"
    return plan, errs


def summary(plan: dict) -> dict:
    """What gets logged in the ledger for later analysis."""
    return {
        "format": plan.get("format", "clip"),
        "layouts": [s["layout"] for s in plan.get("segments", [])],
        "switches": max(0, len(plan.get("segments", [])) - 1),
        "punch_ins": len(plan.get("punch_ins", [])),
        "cold_open": bool(plan.get("cold_open")),
        "hook_type": plan.get("hook_type"),
        "hook_look": plan.get("hook_look"),
        "hook_style": plan.get("hook_style"),
        "caption_preset": plan.get("caption_preset"),
        "sfx": [s["name"] for s in plan.get("sfx", [])],
        "source": plan.get("source", ""),
    }
