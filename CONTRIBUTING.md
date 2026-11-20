# Contributing to GGTA

Thanks for helping! Contributions in **English or Spanish** are welcome.

## Ways to help

- **Add or fix streamers** in [`streamers.yaml`](streamers.yaml). Check that each channel is the real one and not an impostor (look at follower counts).
- **Tune detection.** Chat noise filters, reaction words in more languages, and song detection all live in `clipsmith/src/clipsmith/sources/vod.py`.
- **Improve hooks and the AI editor prompts** in `clipsmith/src/clipsmith/rendering/hook.py` and `clipsmith/src/clipsmith/llm/prompts.py`.
- **Add platforms or publishers** in `clipsmith/src/clipsmith/sources/` and `clipsmith/src/clipsmith/cli/publish.py`.
- **Report bugs** with the stream URL, the command you ran and the log.

## Development setup

```bash
cd clipsmith
uv venv && uv pip install -e ".[dev,server]" "yt-dlp[default]" edge-tts pillow "opencv-python-headless<5"
.venv/bin/ruff check src tests
.venv/bin/python -m pytest tests -q          # e2e tests are skipped by default
```

## Pull requests

1. Fork the repo and create a branch from `main`, for example `feat/kick-live-chat` or `fix/youtube-postlive`.
2. Keep each PR focused on one change, and add or adjust tests when behavior changes.
3. Make sure `ruff check` and `pytest` pass, since CI runs both.
4. Describe what changed and how you tested it. A before/after clip or screenshot helps a lot for rendering changes.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).

## Never commit

- `.env`, API keys, cookies or OAuth tokens
- Video, audio or transcript files (`work/` and `out/` are gitignored)
- Clips of streamers who haven't given permission

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
