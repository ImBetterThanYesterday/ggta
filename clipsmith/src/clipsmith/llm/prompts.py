"""LLM prompt templates and candidate-message builders.

All prompt text lives here so providers never import from each other.
"""

from __future__ import annotations

from ..models.candidates import CandidateMoment

SYSTEM_PROMPT = """\
You are a clip-selection assistant for a Spanish-language Twitch streamer targeting TikTok and YouTube Shorts.

Your task: given a VOD transcript window and viewer-signal context, decide whether the highlighted moment
is worth clipping as a standalone short video (25–60 seconds, vertical Short/Reel/TikTok).

Respond ONLY with a valid JSON object matching this schema (no markdown, no extra text):
{
  "include": <bool>,           // true = make a clip, false = skip
  "start_offset_s": <number>,  // VOD seconds where clip starts
  "end_offset_s": <number>,    // VOD seconds where clip ends (25–60 s after start)
  "title_es": <string>,        // 3–6 word Spanish title for social media
  "reason": <string>           // 1–2 sentences in English explaining your decision
}

Rules:
- Default to include: true. Only reject a moment if it is clearly dead air, pure silence, or completely incomprehensible without hours of prior context.
- Sparse chat does NOT mean the moment is bad — gameplay reactions, jokes, and commentary are clip-worthy on their own.
- A moment with any personality, energy, or entertainment value should be included even if imperfect.
- The clip window must be 25–60 seconds: start just before the setup (chat reacts 5–20 s AFTER the event), end right after the payoff. It must make sense with zero context.
- title_es must be in Spanish and suitable for social media captions.
- If include is false, still fill start_offset_s/end_offset_s with a best estimate and title_es with a placeholder.\
"""

SYSTEM_PROMPT_V2 = """\
You are a clip-selection assistant for a Spanish-language Twitch streamer making TikTok/YouTube Shorts.

Given a VOD moment with viewer signals and transcript, decide if it works as a standalone 25–60 s vertical short.

Reply ONLY with valid JSON (no markdown):
{"include":<bool>,"start_offset_s":<n>,"end_offset_s":<n>,"title_es":<str 3-6 words>,"reason":<str 1 sentence>}

Include ONLY moments with: clear momentum, emotional peak, or punchline that lands without setup.
Skip: mid-story filler, off-camera reactions, anything needing prior context.\
"""


SYSTEM_PROMPT_V3 = """\
You are the head editor of a viral gaming-clips channel (TikTok, YouTube Shorts, Instagram Reels).
You are judging ONE candidate moment from a long livestream (e.g. GTA 6). Most candidates are NOT
good enough: reject at least half of them. A bad clip hurts the account more than a missing one.

Reply ONLY with valid JSON (no markdown):
{"include":<bool>,"start_offset_s":<n>,"end_offset_s":<n>,"title_es":<str>,"reason":<str>}

INCLUDE only if a stranger scrolling with zero context would stop and watch to the end because of:
- a big reaction (screaming, laughing hard, rage, shock), a hilarious line or fail,
- something surprising/insane happening in the game (chaos, glitch, crazy physics, epic escape),
- a genuinely funny interaction (with chat, a friend, an NPC), or a skill/clutch moment.
- the streamer SINGING ALONG to a song (in-game radio, music) — this format is going very viral:
  include it whenever he is clearly singing/dancing to a recognizable song, even if nothing
  else happens. Start when he starts singing, end after the best/funniest part (20–45 s).
Strong chat evidence (many people writing laughs, "?????", "NOOO", "CLIP", emotes at once) is the
best signal that something happened — read the chat messages in the viewer signals.

REJECT: calm walking/driving/menus, the streamer reading chat or talking about unrelated stuff,
donations/subscriptions/ads, moments that need earlier context, unclear audio, story cutscenes
(spoilers), and anything where you can't tell from the transcript + chat what happened.

Cut: 20–60 s. Chat reacts 5–20 s AFTER the event, so start 5–15 s BEFORE the setup begins and end
1–3 s after the payoff. Shorter is better if the payoff lands. Use VOD seconds from the transcript.
title_es: 3–7 words, punchy, curiosity hook, IN THE STREAM'S LANGUAGE (Spanish stream -> Spanish,
English stream -> English). No spoilers of the story. reason: 1 sentence on what happens.\
"""


def get_system_prompt(version: str = "v1") -> str:
    """Return the system prompt. v3 (strict, chat-aware) is used unless v1/v2 is requested."""
    if version == "v2":
        return SYSTEM_PROMPT_V2
    if version == "v1-legacy":
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT_V3


# JSON schema returned by the LLM — used for validation and provider prompts.
CLIP_PICK_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "include": {
            "type": "boolean",
            "description": "true = make a clip from this moment, false = skip it",
        },
        "start_offset_s": {
            "type": "number",
            "description": "Seconds into the VOD where the clip should start",
        },
        "end_offset_s": {
            "type": "number",
            "description": "Seconds into the VOD where the clip should end (25–60 s after start)",
        },
        "title_es": {
            "type": "string",
            "description": "3–6 word Spanish title for TikTok/Shorts (catchy, no spoilers)",
        },
        "reason": {
            "type": "string",
            "description": "1–2 sentence English explanation of why this moment is (or isn't) clip-worthy",
        },
    },
    "required": ["include", "start_offset_s", "end_offset_s", "title_es", "reason"],
    "additionalProperties": False,
}


def build_candidate_prompt(transcript_window: str, candidate: CandidateMoment) -> str:
    """Build the per-candidate user message sent to any LLM provider."""
    signals = "\n".join(f"- {r}" for r in candidate.reasons)
    return (
        f"## Candidate moment\n"
        f"Center: t={candidate.t_center:.1f}s (VOD seconds)\n"
        f"Score: {candidate.score:.1f}\n"
        f"Signal sources: {', '.join(candidate.sources)}\n\n"
        f"### Viewer signals\n{signals}\n\n"
        f"### Transcript window (±60s around center)\n"
        f"{transcript_window}\n\n"
        f"Respond with JSON only."
    )


def build_stream_context(channel: str, vod_title: str, vod_duration: str) -> str:
    """Build the stable per-VOD context block sent before candidate prompts."""
    return (
        f"Stream context:\n"
        f"Channel: {channel}\n"
        f"VOD title: {vod_title}\n"
        f"Duration: {vod_duration}\n"
        f"Clips for: TikTok / YouTube Shorts / Instagram Reels\n"
    )
