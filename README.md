# GGTA 🎮✂️

**Turn GTA 6 livestreams into ready-to-post vertical clips, automatically.**

GGTA watches the chat, not the video. When chat explodes, when the streamer screams,
or when everybody starts singing along to the radio, that's a viral moment. GGTA
downloads only those minutes, has an AI editor decide what's worth posting, and
renders a 9:16 short with:

- **the streamer's face on top and the gameplay below**
- **TikTok-style captions**
- **a hook**: an AI voice-over and/or on-screen text ("SPREEN GETS REPORTED FOR KICKING A CHICKEN")
- **a folder per clip** with `video.mp4` and `publicar.txt`, which holds the title, description, hashtags, credit and a link to the exact minute

It works with **Kick, Twitch and YouTube**, in Spanish and English, and it can
publish straight to YouTube Shorts, Facebook/Instagram Reels and TikTok.

> 🇪🇸 Spanish user guide: **[COMO-USAR.md](COMO-USAR.md)**

---

## How it works

```
finished stream URL
   │
   ├─ 1. download the full chat (light: minutes, not hours)
   ├─ 2. find peaks ── chat z-score (msgs, unique chatters, laughs/reactions)
   │                 ├ loudness of the audio-only track ("bulla")
   │                 ├ sing-along moments ("temazo", 🎵, "song")
   │                 └ YouTube "most replayed" heatmap
   ├─ 3. download ONLY ~2 min around each peak, 1080p
   ├─ 4. transcribe (faster-whisper) → AI editor rejects weak moments, trims good ones to 25–60 s
   ├─ 5. AI hook: voice + text or text-only, in the streamer's language
   ├─ 6. render: facecam detection → face panel + gameplay + captions + hook (ffmpeg)
   └─ 7. out/<stream>/<time>_<title>/{video.mp4, publicar.txt} → publish
```

The AI uses your **Claude Code or ChatGPT (Codex) subscription** through their CLIs,
so you don't pay per-token API costs. You can also use API keys or a local Ollama model.

## Quick start

Requirements:

- Python **3.11–3.13**. `uv venv` picks 3.12 automatically from `clipsmith/.python-version`. 3.14 isn't supported yet because `onnxruntime` has no wheels for it.
- [uv](https://docs.astral.sh/uv/) (or pip), `ffmpeg` and Node.js. yt-dlp uses Node.js for YouTube.
- An AI CLI that is logged in. The default is the **Codex CLI** (`codex`, your ChatGPT plan). To use **Claude Code** (`claude`, then `/login`), set `llm.provider: claude_cli` and `hook.provider: claude_cli` in `clipsmith/config.yaml`.

The first run downloads the Whisper speech model, which is about 500 MB.

```bash
git clone https://github.com/ImBetterThanYesterday/ggta.git
cd ggta/clipsmith
uv venv && uv pip install -e ".[dev]" "yt-dlp[default]" edge-tts pillow "opencv-python-headless<5"
cp .env.example .env        # optional keys (Upload-Post, Twitch, APIs)
cd ..

./clip vod "https://www.youtube.com/watch?v=<id>" --top 8          # one finished stream
./clip batch --only Spreen --only IShowSpeed                       # latest finished GTA stream of each
./clip radar                                                       # who's live playing GTA right now
./clip publish --dry-run --n 5                                     # preview what would be posted
./clip ledger plan --days 7                                        # calendar of what goes where (needs docker compose up -d)
```

The streamer roster lives in [`streamers.yaml`](streamers.yaml): about 40 English and
Spanish streamers with their verified YouTube, Twitch and Kick channels. Each one's
`lang` field sets the language of the hook, the voice and the post text.

## Repository layout

| Path | What |
|---|---|
| [`clip`](clip) | Launcher script (`./clip <url>` or `./clip <command>`) |
| [`clipsmith/`](clipsmith/) | The pipeline: a Python package, a fork of [clipsmith](https://github.com/ricardogr07/clipsmith) |
| `clipsmith/src/clipsmith/sources/` | Kick, YouTube and Twitch fetchers, chat, peaks, loudness, live status |
| `clipsmith/src/clipsmith/rendering/` | Smart layout, captions, AI hooks + TTS |
| `clipsmith/src/clipsmith/cli/` | `vod`, `batch`, `radar`, `live`, `publish` commands |
| [`docker-compose.yml`](docker-compose.yml) | Local Postgres for the publishing ledger (`./clip ledger`) |
| `clipsmith/src/clipsmith/ledger/` | Ledger: which clip is on which network, and the scheduling rules |
| [`streamers.yaml`](streamers.yaml) | Streamer roster |
| [`research/`](research/) | Background research on automated clip channels |
| [`tools/chat_peaks.py`](tools/chat_peaks.py) | Original standalone chat-peak prototype |

## ⚠️ Use responsibly

This is a tool. **Stream footage belongs to the streamers and game publishers.**
Only clip streamers who allow it, for example through their permission or an
official clipping program (Whop / Content Rewards campaigns). Always credit them,
and review every clip before posting. See [NOTICE.md](NOTICE.md).

GGTA is not affiliated with Rockstar Games, Take-Two, or any streamer or platform.

## Contributing

PRs welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) and our [Code of Conduct](CODE_OF_CONDUCT.md).
Report security issues as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE). Includes code from clipsmith (MIT, © Ricardo García Ramírez); see [NOTICE.md](NOTICE.md).
