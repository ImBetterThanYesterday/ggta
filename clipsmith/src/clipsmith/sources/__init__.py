"""Multi-platform sources: Twitch, Kick and YouTube URLs -> local MP4 + chat.json.

Everything is normalized to the same on-disk layout the pipeline already uses:

    work/<video_id>/<video_id>.mp4
    work/<video_id>/chat.json        (ChatLog format)

plus a list of "signal" Clips (e.g. YouTube "most replayed" peaks) that are fed
into candidate scoring the same way existing Twitch clips are.
"""

from __future__ import annotations

from .fetch import FetchResult, detect_platform, fetch_source

__all__ = ["FetchResult", "detect_platform", "fetch_source"]
