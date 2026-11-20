# Notices and attribution

## clipsmith (upstream)

The `clipsmith/` directory is a modified fork of
[ricardogr07/clipsmith](https://github.com/ricardogr07/clipsmith),
Copyright (c) 2026 Ricardo García Ramírez, released under the MIT License.
The original license text is kept in [`clipsmith/LICENSE`](clipsmith/LICENSE).

GGTA's main changes to it:

- Kick and YouTube sources (VOD and live), besides Twitch
- Chat-first processing of finished streams: download only the chat, find the peaks, then download only those windows
- Loudness ("bulla") and sing-along detection
- Facecam-aware "smart" 9:16 layout and PNG captions (no libass needed)
- AI hooks (voice-over and on-screen text) in Spanish or English
- LLM providers that use the Claude Code and Codex CLI subscriptions instead of API keys
- Packaging and publishing to social networks through Upload-Post

## Third-party tools

GGTA calls these tools; it does not bundle them. Each has its own license.

- [FFmpeg](https://ffmpeg.org/) (LGPL or GPL, depending on your build)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT)
- [edge-tts](https://github.com/rany2/edge-tts) (GPL-3.0). It runs as a separate dependency and uses Microsoft's online TTS service.
- [OpenCV](https://opencv.org/) (Apache-2.0)

## Trademarks and content

GGTA is not affiliated with, endorsed by, or sponsored by Rockstar Games,
Take-Two Interactive, Twitch, Kick, YouTube, TikTok, Meta, or any streamer named
in this repository. "Grand Theft Auto" and "GTA" are trademarks of Take-Two
Interactive. Streamer names in `streamers.yaml` refer only to their public
channels.

The software is MIT-licensed. **The videos it processes are not.** Stream footage
belongs to the streamers and game publishers. You are responsible for having the
right to clip and republish it, for example through the streamer's permission
or an official clipping program.
