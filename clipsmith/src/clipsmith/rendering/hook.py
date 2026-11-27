"""Hooks: the first 2–3 seconds that make people stop scrolling.

- An LLM writes a disruptive, curiosity-gap ("open loop") line grounded in what really
  happens in the clip (+ title/caption/hashtags for posting).
- edge-tts (free Microsoft neural voices, no API key) reads it as a voice-over.
- The hook text is drawn as a big TikTok-style white box while the voice plays;
  game audio is ducked under the voice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess  # nosec B404
from pathlib import Path

log = logging.getLogger(__name__)

HOOK_SCHEMA = {
    "type": "object",
    "properties": {
        "hook_style": {"type": "string", "enum": ["voice_text", "text_only"]},
        "hook_text": {"type": "string"},
        "voiceover": {"type": "string"},
        "post_title": {"type": "string"},
        "post_caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["hook_style", "hook_text", "voiceover", "post_title", "post_caption", "hashtags"],
    "additionalProperties": False,
}

HOOK_PROMPT = """\
You write the HOOK for a vertical short (TikTok / Reels / Shorts) cut from a livestream.
The hook is the first 2–3 seconds: on-screen text + an AI voice-over. Its only job is to make
people stop scrolling and stay until the payoff (an "open loop": promise something wild,
don't reveal how it ends).

Style: disruptive, provocative, a bit controversial / exaggerated, meme-like, clickbait-y —
like the biggest gaming clip pages. Name the streamer. Use strong verbs ("ATROPELLA",
"DESTRUYE", "NO TUVO PIEDAD", "SE VOLVIÓ LOCO"). Mention GTA 6 when relevant.
It MUST be grounded in what really happens in the clip: exaggerate the framing, never invent
events, and never accuse the real person of real-life crimes or anything offensive.

Rules that make hooks work:
- Name the CONCRETE, absurd thing that happens (the animal, the object, the victim, the fail),
  in the most shocking framing. Vague hooks ("y pasa esto", "le sale mal") are BANNED.
- Lead with the streamer's name, spelled EXACTLY as given.
- Present tense, headline style, like breaking news of something outrageous.
- Prefer framing that makes people want to comment/argue (moral outrage, "¿esto es normal?").
Good examples of the tone (do not copy, adapt):
  "DjMaRiiO ATROPELLA UN PELÍCANO EN SU PRIMERA PARTIDA"
  "IBAI ROBA UN BUS ESCOLAR LLENO DE NIÑOS EN GTA 6"
  "SPEED LE PEGA A UNA ABUELA Y LA POLICÍA LO PERSIGUE"
  "WESTCOL MATA A SU PROPIO AMIGO SIN QUERER EN VIVO"
Choose hook_style:
- "voice_text": AI voice-over + on-screen text. Best when the first seconds are quiet/calm
  (flying, walking, menus) and need context to make sense.
- "text_only": just the on-screen text, no voice. Best when the streamer is already talking,
  screaming or reacting in the first seconds (a voice-over would talk over him), or when the
  moment is instantly obvious and should hit immediately.
If text_only, still fill voiceover (it won't be used).

First think of 5 different candidate hooks, then output only the single most disruptive one.

Language: {lang}. Voice-over must be SHORT: 6–11 words (~2.5–3.5 s spoken), punchy, natural.
hook_text: 3–7 words, UPPERCASE, can differ slightly from the voice-over.
post_title: ≤ 70 chars for YouTube Shorts. post_caption: 1–2 lines for TikTok/IG with a
question to drive comments. hashtags: 5–8 (include #gta6 #gtavi and the streamer).

Streamer: {streamer}
Stream title: {stream_title}
What happens in the clip: {reason}
Transcript of the clip:
{transcript}
What chat was writing at that moment:
{chat}
"""


def generate_hook(*, streamer: str, stream_title: str, reason: str, transcript: str,
                  chat: str, lang: str = "Spanish", provider: str = "codex_cli") -> dict:
    from ..llm.subscription_provider import ask_json

    prompt = HOOK_PROMPT.format(lang=lang, streamer=streamer, stream_title=stream_title,
                                reason=reason, transcript=transcript[:3000], chat=chat[:1500])
    hook = ask_json(prompt, HOOK_SCHEMA, provider=provider)
    log.info("hook: %s | VO: %s", hook.get("hook_text"), hook.get("voiceover"))
    return hook


def synth_voice(text: str, out_mp3: Path, voice: str = "es-MX-JorgeNeural",
                rate: str = "+12%") -> float:
    """Free neural TTS via edge-tts. Returns duration in seconds."""
    import edge_tts

    async def _run() -> None:
        await edge_tts.Communicate(text, voice, rate=rate).save(str(out_mp3))

    asyncio.run(_run())
    dur = subprocess.run(  # nosec B603
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(out_mp3)], capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(dur)


def render_hook_png(text: str, out_png: Path, look: str = "white_box") -> tuple[int, int]:
    """Hook text as a PNG. Looks: white_box (classic TikTok), black_box, yellow_box,
    outline (no box, heavy stroke) and top_bar (full-width header kept the whole clip)."""
    from PIL import Image, ImageDraw, ImageFont

    from .captions_png import _font

    if look == "outline":
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Impact.ttf", 112) \
            if Path("/System/Library/Fonts/Supplemental/Impact.ttf").exists() else _font()
    elif look == "top_bar":
        font = ImageFont.truetype("/System/Library/Fonts/Avenir Next.ttc", 66, index=8) \
            if Path("/System/Library/Fonts/Avenir Next.ttc").exists() else _font()
    else:
        font = _font()
    max_w = 1000 if look == "top_bar" else 940
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if probe.textlength(t, font=font) > max_w - 60 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = t
    lines.append(cur)
    asc, desc = font.getmetrics()
    line_h = asc + desc + (2 if look == "top_bar" else 10)
    pad = 26 if look == "top_bar" else 34
    text_w = int(max(probe.textlength(line, font=font) for line in lines))
    box_w = 1060 if look == "top_bar" else text_w + pad * 2
    box_h = line_h * len(lines) + pad * 2 - 10
    img = Image.new("RGBA", (box_w + 20, box_h + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill, ink, stroke = (255, 255, 255, 255), (0, 0, 0, 255), 0
    if look == "black_box":
        fill, ink = (12, 12, 12, 235), (255, 255, 255, 255)
    elif look == "yellow_box":
        fill, ink = (255, 221, 0, 255), (0, 0, 0, 255)
    elif look == "top_bar":
        fill, ink = (0, 0, 0, 215), (255, 255, 255, 255)
    elif look == "outline":
        fill, ink, stroke = None, (255, 255, 255, 255), 10
    if fill is not None:
        radius = 18 if look == "top_bar" else 28
        d.rounded_rectangle((10, 14, box_w + 10, box_h + 14), radius=radius, fill=(0, 0, 0, 90))  # shadow
        d.rounded_rectangle((6, 6, box_w + 6, box_h + 6), radius=radius, fill=fill)
    y = pad + 6
    for line in lines:
        lw = probe.textlength(line, font=font)
        d.text((6 + (box_w - lw) / 2, y), line, font=font, fill=ink,
               stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        y += line_h
    img.save(out_png)
    return img.size


def save_hook(hook: dict, path: Path) -> None:
    path.write_text(json.dumps(hook, ensure_ascii=False, indent=2), encoding="utf-8")
